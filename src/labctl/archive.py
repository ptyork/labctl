"""labctl's archives on a node: what is in them, and deleting what is no longer wanted.

A node has two: APPDATA_DIR/.labctl/archive (data, volumes, failed copies) and
DEPLOY_DIR/.labctl/archive (stray files, node edits). Only labctl writes there, so everything in
them is labctl's to delete. An entry is anything named ``<name>-YYYYMMDD-HHMMSS``, at any depth (a
data key can be nested: ``fileflows/data-<stamp>``). Data and sizes are often root's, so both
reading and deleting run in the helper container, with only the archive directory mounted.

Deciding what to delete is pure: entries are grouped, and each group keeps its newest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from labctl.envfile import preview_env
from labctl.layout import ARCHIVE_DIR, FILES_ARCHIVE, RECORD, SIDECAR
from labctl.node import Node, lock, unlock
from labctl.remote import Remote, execute
from labctl.settings import Settings, SettingsError
from labctl.stack import deploy_dir_on

STAMP_FORMAT = "%Y%m%d-%H%M%S"
_STAMP_LENGTH = len("YYYYMMDD-HHMMSS")
_STAMPED = "*-" + "[0-9]" * 8 + "-" + "[0-9]" * 6
_AGE = re.compile(r"(\d+)([dh])")

# Group kinds, in the order they are shown.
DATA = "data"
FAILED = "failed"
FILES = "files"

# In the helper, the archive on /x: "entry <path> <size> <sidecar>" and "orphan <path>", tab apart.
# Entries are not looked inside.
_LIST = f"""\
cd /x
find . -mindepth 1 \\( -name '{_STAMPED}' -prune -printf 'entry %P\\n' \\) \
  -o \\( -type f -name '*{SIDECAR}*' -printf 'sidecar %P\\n' \\) |
while read -r kind path; do
  if [ "$kind" = entry ]; then
    printf 'entry\\t%s\\t%s\\t' "$path" "$(du -sb -- "$path" | cut -f1)"
    if [ -f "$path{SIDECAR}" ]; then tr -d '\\n' < "$path{SIDECAR}"; fi
    echo
  else
    case "$path" in *{SIDECAR}) if [ -e "${{path%{SIDECAR}}}" ]; then continue; fi ;; esac
    printf 'orphan\\t%s\\n' "$path"
  fi
done
"""

# In the helper, the archive on /x: each path, its sidecar, and parents left empty. The entry goes
# first: interrupted, what is left is a sidecar that the next prune removes.
_DELETE = f"""\
set -e
cd /x
for path in "$@"; do
  rm -rf -- "$path"
  rm -f -- "$path{SIDECAR}"
  parent=$(dirname -- "$path")
  while [ "$parent" != . ]; do
    rmdir -- "$parent" 2>/dev/null || break
    parent=$(dirname -- "$parent")
  done
done
"""


@dataclass(frozen=True)
class Entry:
    """One thing in an archive."""

    folder: str  # the archive directory it is in
    path: str  # under the folder, ending in its stamp
    size: int | None = None
    files: bool = False  # in DEPLOY_DIR's archive
    sidecar: Mapping[str, object] = field(default_factory=dict, compare=False, hash=False)

    @property
    def stamp(self) -> str:
        return self.path[-_STAMP_LENGTH:]

    @property
    def name(self) -> str:
        """The path without its stamp."""
        return self.path[: -_STAMP_LENGTH - 1]

    @property
    def when(self) -> datetime:
        return datetime.strptime(self.stamp, STAMP_FORMAT)

    @property
    def reason(self) -> str:
        """Why it was archived, as its sidecar says ("" when it has none)."""
        return str(self.sidecar.get("reason", ""))

    @property
    def failed(self) -> bool:
        """A copy that did not stay: apply undid it, so nothing relies on it."""
        return not self.files and not self.sidecar and self.name.endswith("-failed")

    @property
    def mounts(self) -> tuple[tuple[str, str], ...]:
        """Each (service, container path) it held, as its sidecar says."""
        mounts = self.sidecar.get("mounts")
        return tuple(
            sorted(
                {
                    (str(mount.get("service", "")), str(mount.get("target", "")))
                    for mount in (mounts if isinstance(mounts, list) else [])
                    if isinstance(mount, dict)
                }
            )
        )


@dataclass(frozen=True)
class Group:
    """Entries that are copies of one thing, newest first."""

    kind: str  # DATA, FAILED or FILES
    title: str
    entries: tuple[Entry, ...]
    service: str = ""  # the service whose data this is


@dataclass(frozen=True)
class NodeArchive:
    """What a node's archives hold."""

    node: str
    entries: tuple[Entry, ...] = ()
    orphans: tuple[tuple[str, str], ...] = ()  # (folder, path) of sidecars with no entry


#############################################################################


# Deciding


#############################################################################


def group_entries(entries: Iterable[Entry]) -> list[Group]:
    """
    Group entries into copies of the same thing.

    Args:
        entries (Iterable[Entry]):
            The entries of one node.

    Returns:
        list[Group]:
            Data by service and container path (one entry for each mount it held), then the failed
            copies, then files by name.
    """
    data: dict[tuple[str, str], list[Entry]] = {}
    failed: list[Entry] = []
    files: dict[str, list[Entry]] = {}
    for entry in entries:
        if entry.failed:
            failed.append(entry)
        elif entry.files:
            files.setdefault(entry.name, []).append(entry)
        else:
            for mount in entry.mounts or (("", entry.name),):
                data.setdefault(mount, []).append(entry)
    groups = [
        Group(DATA, f"{service} {target}" if service else target, _newest_first(found), service)
        for (service, target), found in sorted(data.items())
    ]
    if failed:
        groups.append(Group(FAILED, "failed copies", _newest_first(failed)))
    groups += [Group(FILES, name, _newest_first(found)) for name, found in sorted(files.items())]
    return groups


def _newest_first(entries: Iterable[Entry]) -> tuple[Entry, ...]:
    return tuple(sorted(entries, key=lambda entry: (entry.stamp, entry.path), reverse=True))


def select(
    groups: Sequence[Group],
    *,
    keep: int = 1,
    older_than: timedelta | None = None,
    services: Collection[str] = (),
    now: datetime | None = None,
) -> frozenset[Entry]:
    """
    The entries to delete.

    Args:
        groups (Sequence[Group]):
            One node's groups.
        keep (int):
            The newest entries each group keeps (default: 1). Failed copies keep none.
        older_than (timedelta | None):
            Of those, delete only entries older than this (default: any age).
        services (Collection[str]):
            Only these services' groups (default: every group).
        now (datetime | None):
            The time ages are measured from (default: now).

    Returns:
        frozenset[Entry]:
            What to delete. An entry in several groups goes only when all of them let it go,
            including groups ``services`` left out.
    """
    cutoff = (now or datetime.now()) - older_than if older_than is not None else None
    doomed: set[Entry] = set()
    spared: set[Entry] = set()
    for group in groups:
        candidates: tuple[Entry, ...] = ()
        if not services or group.service in services:
            candidates = group.entries if group.kind == FAILED else group.entries[keep:]
        for entry in group.entries:
            if entry in candidates and (cutoff is None or entry.when < cutoff):
                doomed.add(entry)
            else:
                spared.add(entry)
    return frozenset(doomed - spared)


def parse_age(text: str) -> timedelta:
    """
    An age as given to --older-than.

    Args:
        text (str):
            A number and a unit: ``30d`` or ``12h``.

    Returns:
        timedelta:
            The age.

    Raises:
        ValueError:
            It is neither.
    """
    match = _AGE.fullmatch(text)
    if match is None:
        raise ValueError(f"'{text}' is not an age like 30d or 12h")
    return timedelta(**{"days" if match[2] == "d" else "hours": int(match[1])})


#############################################################################


# On the node


#############################################################################


def archive_dirs(settings: Settings, node: str, remote: Remote) -> tuple[str, str]:
    """
    Where a node's archives are: where its last apply put things, else where the repo says.

    Args:
        settings (Settings):
            The lab config.
        node (str):
            The node.
        remote (Remote):
            The node's shell.

    Returns:
        tuple[str, str]:
            The data archive and the files archive.

    Raises:
        SettingsError:
            Neither the node nor the repo says.
    """
    deploy = deploy_dir_on(settings, node, remote)
    if not deploy:
        raise SettingsError(f"{node}: DEPLOY_DIR is not set")
    record = execute(["cat", f"{deploy}/{RECORD}"], "/", remote)
    appdata = ""
    if record.code == 0:
        try:
            appdata = str(json.loads(record.stdout).get("appdata_dir", ""))
        except ValueError:
            pass
    appdata = appdata or preview_env(settings.nodes_dir, node).get("APPDATA_DIR", "")
    if not appdata:
        raise SettingsError(f"{node}: APPDATA_DIR is not set")
    return f"{appdata}/{ARCHIVE_DIR}", f"{deploy}/{FILES_ARCHIVE}"


def read_archive(name: str, node: Node, data_dir: str, files_dir: str) -> NodeArchive:
    """
    Read what a node's archives hold (read-only).

    Args:
        name (str):
            The node's name.
        node (Node):
            The node.
        data_dir (str):
            The data archive.
        files_dir (str):
            The files archive.

    Returns:
        NodeArchive:
            Its entries and orphaned sidecars. An archive that is not there is empty (it is checked
            first: mounting a missing directory would create it).
    """
    entries: list[Entry] = []
    orphans: list[tuple[str, str]] = []
    for folder, files in ((data_dir, False), (files_dir, True)):
        if node.exists(folder):
            found, lost = parse_listing(folder, files, node.helper(folder, _LIST, read_only=True))
            entries += found
            orphans += lost
    return NodeArchive(name, tuple(entries), tuple(orphans))


def parse_listing(
    folder: str, files: bool, output: str
) -> tuple[list[Entry], list[tuple[str, str]]]:
    entries: list[Entry] = []
    orphans: list[tuple[str, str]] = []
    for line in output.splitlines():
        kind, _, rest = line.partition("\t")
        if kind == "orphan":
            orphans.append((folder, rest))
        elif kind == "entry":
            path, _, rest = rest.partition("\t")
            size, _, text = rest.partition("\t")
            entries.append(
                Entry(folder, path, int(size) if size.isdigit() else None, files, _sidecar(text))
            )
    return entries, orphans


def _sidecar(text: str) -> Mapping[str, object]:
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def delete_entries(node: Node, archive: NodeArchive, doomed: Iterable[Entry], owner: str) -> None:
    """
    Delete entries and orphaned sidecars, under the node's apply lock.

    Args:
        node (Node):
            The node.
        archive (NodeArchive):
            What was read from it, for its orphaned sidecars.
        doomed (Iterable[Entry]):
            The entries to delete. One already gone is fine.
        owner (str):
            Who holds the lock, shown to an apply that finds it.

    Raises:
        NodeError:
            The lock is held, or a deletion failed.
    """
    paths: dict[str, list[str]] = {}
    for entry in doomed:
        paths.setdefault(entry.folder, []).append(entry.path)
    for folder, path in archive.orphans:
        paths.setdefault(folder, []).append(path)
    lock(node.remote, owner)
    try:
        for folder, found in sorted(paths.items()):
            node.helper(folder, _DELETE, *sorted(found))
    finally:
        unlock(node.remote)
