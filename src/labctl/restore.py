"""Restoring archived data to a service that is added to a node.

apply never deletes data: what no service uses any more is archived to APPDATA_DIR/.labctl/archive
(a volume as files), each entry with a sidecar, ``<entry>.labctl.json``: the node, the apply's
stamp, why it was archived, and every mount it held (service, stack, container path, data key).
That is what makes an archive restorable.

When a stateful service is added to a node and the nodes read hold archived copies of its data,
the newest copy is restored by default: the copies are matched by service and container path (the
same identity as renames), so ``volume:homelab-frodo_db`` restores into ``homelab-bilbo_db`` and
``plexms/`` into ``plex/``. In a terminal the plan asks; ``--fresh SERVICE`` starts empty and
``--restore SERVICE@STAMP`` picks a copy.

Discovery reads the nodes (read-only); the rest is pure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from labctl.layout import ARCHIVE_DIR
from labctl.planner import Add, Plan, Restore
from labctl.remote import Remote, RemoteError
from labctl.state import NodeState, is_volume, under
from labctl.text import human_size, plural

# $1: APPDATA_DIR. Each sidecar whose entry is still there: entry, size, then the JSON on one line.
_LIST = f"""\
cd "$1/{ARCHIVE_DIR}" 2>/dev/null || exit 0
find . -name '*.labctl.json' -type f | while IFS= read -r f; do
  e=${{f%.labctl.json}}
  [ -e "$e" ] || continue
  printf '%s\\t%s\\t' "${{e#./}}" "$(du -sb "$e" 2>/dev/null | cut -f1)"
  tr -d '\\n' < "$f"
  echo
done
"""


@dataclass(frozen=True)
class Archived:
    """One archive entry and what its sidecar says."""

    node: str
    root: str  # the node's APPDATA_DIR
    entry: str  # under archive/
    size: int | None
    data: Mapping[str, object]

    @property
    def stamp(self) -> str:
        return str(self.data.get("stamp", ""))

    @property
    def reason(self) -> str:
        return str(self.data.get("reason", ""))


@dataclass(frozen=True)
class Copy:
    """A restorable copy of one service's data: the entries one apply archived on one node."""

    source: str
    stamp: str
    reason: str
    root: str
    entries: tuple[tuple[str, str], ...]  # (entry, data key on the target)
    size: int | None

    def describe(self) -> str:
        at = f"{self.stamp[:4]}-{self.stamp[4:6]}-{self.stamp[6:8]} {self.stamp[9:11]}:"
        at += self.stamp[11:13]
        size = f", {human_size(self.size)}" if self.size is not None else ""
        return f"{self.source}'s archive {self.stamp} ({self.reason} {at}{size})"


def find_archives(roots: Mapping[str, str], remote: Callable[[str], Remote]) -> list[Archived]:
    """
    Every described archive entry on the nodes (read-only).

    Args:
        roots (Mapping[str, str]):
            Each node's APPDATA_DIR, by node.
        remote (Callable[[str], Remote]):
            The node's shell, by node name.

    Returns:
        list[Archived]:
            Each entry with a sidecar. A node that cannot be read is skipped: its archive is just
            not offered.
    """
    found: list[Archived] = []
    for node, root in sorted(roots.items()):
        try:
            output = remote(node).run_script(_LIST, root)
        except RemoteError:
            continue
        found += parse_archives(node, root, output)
    return found


def parse_archives(node: str, root: str, output: str) -> list[Archived]:
    found = []
    for line in output.splitlines():
        entry, _, rest = line.partition("\t")
        size, _, text = rest.partition("\t")
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and entry:
            found.append(Archived(node, root, entry, int(size) if size.isdigit() else None, data))
    return found


def copies_for(
    node: str, service: str, state: NodeState, archived: Iterable[Archived]
) -> list[Copy]:
    """
    The restorable copies of a service's data, matched by service and container path.

    Args:
        node (str):
            The node the service is added to.
        service (str):
            The service.
        state (NodeState):
            The node as the repo wants it.
        archived (Iterable[Archived]):
            The archive entries found (failed copies are left out).

    Returns:
        list[Copy]:
            One per (node, stamp) whose entries map onto the service's mounts, newest first.
    """
    wanted = state.services.get(service)
    if wanted is None or not wanted.stateful:
        return []
    want = dict(wanted.mounts)  # container path -> data key
    groups: dict[tuple[str, str], list[tuple[Archived, str]]] = {}
    for item in archived:
        if item.reason == "failed":
            continue
        mounts = item.data.get("mounts")
        for mount in mounts if isinstance(mounts, list) else []:
            if not isinstance(mount, dict) or mount.get("service") != service:
                continue
            target = str(mount.get("target", ""))
            if target not in want:
                continue
            key = restored_key(
                str(item.data.get("key", "")), str(mount.get("key", "")), want[target]
            )
            if key is not None:
                groups.setdefault((item.node, item.stamp), []).append((item, key))
    copies = []
    for (source, stamp), pairs in groups.items():
        entries = sorted({(item.entry, key) for item, key in pairs})
        if len({entry for entry, _ in entries}) != len(entries):
            continue  # one entry would go to two places
        items = {item.entry: item for item, _ in pairs}
        sizes = [item.size for item in items.values()]
        first = next(iter(items.values()))
        copies.append(
            Copy(
                source,
                stamp,
                first.reason,
                first.root,
                tuple(entries),
                sum(size for size in sizes if size is not None)
                if any(size is not None for size in sizes)
                else None,
            )
        )
    return sorted(copies, key=lambda c: (c.stamp, c.source != node), reverse=True)


def restored_key(entry_key: str, mount_key: str, wanted: str) -> str | None:
    """Where an archive entry goes: the entry archived ``entry_key``, the service mounted
    ``mount_key`` (the entry itself, or inside it) and now wants ``wanted`` at that path."""
    if mount_key == entry_key:
        return wanted
    if is_volume(entry_key) or is_volume(wanted) or not under(mount_key, entry_key):
        return None
    inner = str(PurePosixPath(mount_key).relative_to(entry_key))
    if wanted == inner or not wanted.endswith("/" + inner):
        return None  # the layout inside it changed: nothing to map it to
    return wanted.removesuffix("/" + inner)


# Asked in a terminal: the copy to restore, or None to start fresh.
Ask = Callable[[str, str, Sequence[Copy]], Copy | None]


@dataclass(frozen=True)
class Restores:
    operations: tuple[Restore, ...] = ()
    chosen: tuple[str, ...] = ()  # SERVICE@STAMP for each restore
    fresh: tuple[str, ...] = ()  # the services that start empty
    notes: tuple[str, ...] = ()  # said in the plan: what was decided, how to decide otherwise
    errors: tuple[str, ...] = ()  # a --restore that names no copy


def plan_restores(
    plan: Plan,
    desired: Mapping[str, NodeState],
    archived: Sequence[Archived],
    fresh: Iterable[str] = (),
    restore: Iterable[str] = (),
    ask: Ask | None = None,
) -> Restores:
    """
    Restores for the services the plan adds that have archived copies of their data.

    Args:
        plan (Plan):
            The plan; each Add of a stateful service is looked at.
        desired (Mapping[str, NodeState]):
            Every node as the repo wants it.
        archived (Sequence[Archived]):
            The archive entries found.
        fresh (Iterable[str]):
            ``--fresh``: services that start empty (default: none).
        restore (Iterable[str]):
            ``--restore``: SERVICE (the newest copy) or SERVICE@STAMP (default: none).
        ask (Ask | None):
            Asks which copy in a terminal; None takes the newest and says so (default: None).

    Returns:
        Restores:
            The Restore operations, the choices made (to plan again under the lock), notes for
            the plan, and errors for a ``--restore`` that names no copy.
    """
    fresh, picks = set(fresh), dict(_pick(pick) for pick in restore)
    operations: list[Restore] = []
    chosen: list[str] = []
    starting_fresh: list[str] = []
    notes: list[str] = []
    errors: list[str] = []
    for op in plan.operations:
        if not isinstance(op, Add):
            continue
        copies = copies_for(op.node, op.service, desired[op.node], archived)
        if not copies:
            if op.service in picks:
                errors.append(f"--restore {op.service}: no archived copy of its data")
            continue
        name = f"{op.service} on {op.node}"
        copy: Copy | None
        if op.service in fresh:
            copy = None
        elif picks.get(op.service):
            copy = next((copy for copy in copies if copy.stamp == picks[op.service]), None)
            if copy is None:
                stamps = ", ".join(copy.stamp for copy in copies)
                errors.append(f"--restore {op.service}@{picks[op.service]}: copies: {stamps}")
                continue
        elif ask is not None and op.service not in picks:
            copy = ask(op.service, op.node, copies)
        else:
            copy = copies[0]
            notes.append(
                f"{name}: restoring the newest copy, {copy.describe()}; --fresh {op.service} "
                "starts it empty"
            )
        if copy is None:
            starting_fresh.append(op.service)
            kept = plural(len(copies), "archived copy", "archived copies")
            notes.append(f"{name}: starts empty ({kept} kept)")
            continue
        chosen.append(f"{op.service}@{copy.stamp}")
        operations.append(
            Restore(
                op.service,
                op.node,
                copy.source,
                copy.stamp,
                copy.root,
                copy.entries,
                copy.reason,
                copy.size,
            )
        )
    return Restores(
        tuple(operations), tuple(chosen), tuple(starting_fresh), tuple(notes), tuple(errors)
    )


def _pick(text: str) -> tuple[str, str]:
    service, _, stamp = text.partition("@")
    return service, stamp
