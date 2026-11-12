"""Path checks on the nodes for planned operations. Read-only.

- Bind sources outside the managed root on the target: must already exist (nothing creates them;
  Docker would make an empty root-owned directory).
- Migrate source data: should exist, with its size shown.
- Migrate target data under the managed root: existing data is moved aside (needs --replace-stale).
- Add of a stateful service: existing data on the target is reused.
- Moves (relocated roots, renamed data): the source should exist; the target must not hold data
  (a renamed directory's target may be archived with --replace-stale). Sizes and filesystems show
  whether a root move is a rename or a copy.
- Free space: what is copied onto a filesystem (migrates, restores from another node or into a
  volume, volume renames, moves across filesystems) must fit in what is free there. Copies are made
  in full before anything is archived or deleted, so the whole size counts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import PurePosixPath

from labctl.bundle import Desired
from labctl.compose import ARCHIVE_DIR
from labctl.envfile import PLACEHOLDER_PREFIX
from labctl.moves import DATA, VOLUME, Move, where_now
from labctl.planner import Add, Migrate, Operation, Remove, Restore, Update
from labctl.remote import Remote, RemoteError, node_remote
from labctl.state import NodeState, is_volume, volume_key, volume_name

# Arguments are "<mode>:<path>". Mode S: also measure size and newest mtime. Mode -: kind only.
_PROBE = """\
for arg in "$@"; do
  mode=${arg%%:*}; p=${arg#*:}
  if [ -L "$p" ] && [ ! -e "$p" ]; then kind=broken
  elif [ ! -e "$p" ]; then
    # [ -e ] is also false when a parent directory is not searchable by the ssh user (e.g.
    # /var/lib/docker, root only). Only call it missing if the nearest existing parent is.
    d=$(dirname "$p")
    while [ ! -e "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done
    if [ -x "$d" ]; then kind=missing; else kind=unreadable; fi
  elif [ -d "$p" ]; then
    if [ -z "$(ls -A "$p" 2>/dev/null)" ]; then kind=empty; else kind=dir; fi
  elif [ -f "$p" ]; then kind=file
  else kind=other
  fi
  size=; newest=; dev=
  if [ "$mode" = S ] && { [ "$kind" = dir ] || [ "$kind" = file ]; }; then
    size=$(du -sb "$p" 2>/dev/null | cut -f1)
    newest=$(find "$p" -printf '%T@\\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1)
  fi
  if [ "$mode" = S ]; then
    # Filesystem of the path, or of its nearest existing parent: tells a rename from a copy.
    d=$p; while [ ! -e "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done
    dev=$(stat -c %d "$d" 2>/dev/null)
  fi
  printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "$p" "$kind" "$size" "$newest" "$dev"
done
"""


# Free space: for each path, the filesystem holding it (or its nearest existing parent): bytes
# available and mount point. DOCKER stands for Docker's root dir, where volumes live.
DOCKER = "@docker"
_FREE = """\
for p in "$@"; do
  d=$p
  if [ "$p" = @docker ]; then d=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null); fi
  while [ -n "$d" ] && [ ! -e "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done
  [ -n "$d" ] || continue
  df -PB1 "$d" 2>/dev/null | awk -v p="$p" 'NR == 2 {printf "%s\\t%s\\t%s\\n", p, $4, $6}'
done
"""


class Level(Enum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"  # stops apply unless the named flag is given


@dataclass(frozen=True)
class Finding:
    level: Level
    node: str
    service: str
    message: str
    flag: str = ""  # flag that unblocks it


@dataclass(frozen=True)
class PathInfo:
    # missing, empty, dir, file, broken, other, unreadable (no permission to look)
    kind: str
    size: int | None = None
    newest: datetime | None = None
    device: str = ""  # filesystem (path or nearest existing parent), mode S only

    @property
    def has_data(self) -> bool:
        return self.kind in ("dir", "file")


def check_paths(
    operations: Sequence[Operation],
    desired: Mapping[str, Desired],
    recorded: Mapping[str, NodeState | None],
    *,
    replace_stale: bool = False,
    moves: Sequence[Move] = (),
) -> tuple[list[Finding], dict[str, dict[str, PathInfo]]]:
    """
    Probe the nodes for the data the operations and moves touch (read-only).

    Operations are planned in the post-move layout, but each path is probed where it is on the
    node now (``where_now``).

    Args:
        operations (Sequence[Operation]):
            The plan's operations.
        desired (Mapping[str, Desired]):
            Every node's bundle.
        recorded (Mapping[str, NodeState | None]):
            Every node's state after the moves.
        replace_stale (bool):
            Data already at a target is archived instead of blocking (default: False).
        moves (Sequence[Move]):
            The plan's moves (default: none).

    Returns:
        tuple[list[Finding], dict[str, dict[str, PathInfo]]]:
            The findings, and the raw probe results by node and current path (for sizes in the
            plan).
    """
    wanted: dict[str, dict[str, str]] = {}  # node -> current path -> mode
    now_of: dict[str, dict[str, str]] = {}  # node -> post-move path -> current path

    def probe_now(node: str, path: str, mode: str) -> None:
        if PLACEHOLDER_PREFIX not in path:
            modes = wanted.setdefault(node, {})
            if modes.get(path) != "S":
                modes[path] = mode

    def want(node: str, path: str, mode: str) -> None:
        now = where_now(path, [move for move in moves if move.node == node])
        now_of.setdefault(node, {})[path] = now
        probe_now(node, now, mode)

    volumes: dict[str, set[str]] = {}  # node -> volume keys

    def want_key(node: str, key: str) -> None:
        if is_volume(key):
            volumes.setdefault(node, set()).add(key)
        else:
            want(node, data_spec(desired[node].dirs.appdata_dir, key), "S")

    for move, source, target in move_paths(moves):
        if move.carried:
            continue
        if move.kind == VOLUME:
            want_key(move.node, move.old)
            want_key(move.node, move.new)
        else:
            probe_now(move.node, source, "S")
            probe_now(move.node, target, "S")

    for op in operations:
        match op:
            case Add(service, node) | Update(service, node):
                for path in _outside_root(desired[node], service):
                    want(node, path, "-")
                if isinstance(op, Add):
                    for path in _data_paths(desired[node], service):
                        want(node, path, "S")
                    for key in _volume_keys(desired[node], service):
                        want_key(node, key)
            case Migrate(_, source, target, _, added):
                for service in added:
                    for path in _outside_root(desired[target], service):
                        want(target, path, "-")
                    for path in _data_paths(desired[target], service):
                        want(target, path, "S")
                    for key in _volume_keys(desired[target], service):
                        want_key(target, key)
                for path in migrate_source_paths(op, desired, recorded):
                    want(source, path, "S")
                for key in op.source_data:
                    if is_volume(key):
                        want_key(source, key)
            case Restore(_, node, entries=entries):
                for _, key in entries:
                    want_key(node, key)
            case Remove():
                pass

    results: dict[str, dict[str, PathInfo]] = {}
    findings: list[Finding] = []
    for node in sorted(wanted.keys() | volumes.keys()):
        try:
            remote = node_remote(node)
            results[node] = probe(remote, sorted(wanted.get(node, {}).items()))
            results[node].update(probe_volumes(remote, sorted(volumes.get(node, ()))))
        except RemoteError as exc:
            findings.append(Finding(Level.BLOCK, node, "", f"path checks failed: {exc}"))

    findings.extend(_move_findings(moves, results, replace_stale, desired))
    # The per-operation findings think in the post-move layout (volumes do not move).
    planned = {
        node: {
            **{k: v for k, v in results[node].items() if is_volume(k)},
            **{
                post: results[node][now]
                for post, now in now_of.get(node, {}).items()
                if now in results[node]
            },
        }
        for node in results
    }
    for op in operations:
        findings.extend(_findings_for(op, desired, recorded, planned, replace_stale))
    needed = space_needed(operations, moves, desired, recorded, results, planned)
    for node, destinations in sorted(needed.items()):
        try:
            free = probe_free(node_remote(node), sorted(destinations))
        except RemoteError as exc:
            findings.append(Finding(Level.BLOCK, node, "", f"free space check failed: {exc}"))
            continue
        findings.extend(space_findings(node, destinations, free))
    return findings, results


##########################################################################

# Free space

##########################################################################

# node -> destination (a path, or DOCKER) -> (bytes copied there, what for)
Needed = dict[str, dict[str, tuple[int, tuple[str, ...]]]]


def space_needed(
    operations: Sequence[Operation],
    moves: Sequence[Move],
    desired: Mapping[str, Desired],
    recorded: Mapping[str, NodeState | None],
    results: Mapping[str, Mapping[str, PathInfo]],
    planned: Mapping[str, Mapping[str, PathInfo]],
) -> Needed:
    """
    What each copy the plan makes puts on each node, by destination: migrates (bind data into the
    target's APPDATA_DIR, volumes into Docker's root), restores from another node or into a volume,
    volume renames, and moves across filesystems. Renames on one filesystem need nothing. Sizes
    not known (unreadable, not measured) count as nothing.

    Args:
        operations (Sequence[Operation]):
            The plan's operations.
        moves (Sequence[Move]):
            The plan's moves.
        desired (Mapping[str, Desired]):
            Every node's bundle.
        recorded (Mapping[str, NodeState | None]):
            Every node's state after the moves.
        results (Mapping[str, Mapping[str, PathInfo]]):
            The probe, by current path.
        planned (Mapping[str, Mapping[str, PathInfo]]):
            The probe, by post-move path (volumes by key).

    Returns:
        Needed:
            Per node and destination: the bytes, and what they are for.
    """
    needed: dict[str, dict[str, tuple[int, tuple[str, ...]]]] = {}

    def add(node: str, destination: str, size: int | None, label: str) -> None:
        if not size:
            return
        total, labels = needed.setdefault(node, {}).get(destination, (0, ()))
        needed[node][destination] = (total + size, labels + (() if label in labels else (label,)))

    for op in operations:
        match op:
            case Migrate(_, source, target, _, added):
                label = ", ".join(added)
                root = desired[target].dirs.appdata_dir
                paths = migrate_source_paths(op, desired, recorded)
                for path in paths:
                    if not any(other != path and path.startswith(other + "/") for other in paths):
                        add(target, root, _size(planned, source, path), label)
                for key in op.source_data:
                    if is_volume(key):
                        add(target, DOCKER, _size(planned, source, key), label)
            case Restore(service, node, source, entries=entries, size=size):
                volume = any(is_volume(key) for _, key in entries)
                if source != node or volume:
                    add(node, DOCKER if volume else desired[node].dirs.appdata_dir, size, service)
            case _:
                pass
    for move, source, target in move_paths(moves):
        if move.carried:
            continue
        label = ", ".join(move.services) or f"{move.kind} {move.old} → {move.new}"
        if move.kind == VOLUME:
            root = desired[move.node].dirs.appdata_dir
            size = _size(results, move.node, data_spec(root, move.old))
            add(move.node, DOCKER if is_volume(move.new) else root, size, label)
            continue
        src = results.get(move.node, {}).get(source)
        dst = results.get(move.node, {}).get(target)
        if src and dst and src.device and dst.device and src.device != dst.device:
            add(move.node, target, src.size, label)
    return needed


def _size(results: Mapping[str, Mapping[str, PathInfo]], node: str, path: str) -> int | None:
    info = results.get(node, {}).get(path)
    return info.size if info and info.has_data else None


def probe_free(remote: Remote, destinations: Sequence[str]) -> dict[str, tuple[int, str]]:
    """Each destination's free bytes and mount point (DOCKER: Docker's root dir)."""
    return parse_free(remote.run_script(_FREE, *destinations))


def parse_free(output: str) -> dict[str, tuple[int, str]]:
    """_FREE's output: destination -> (bytes available, mount point)."""
    free: dict[str, tuple[int, str]] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[1].isdigit():
            free[fields[0]] = (int(fields[1]), fields[2])
    return free


def space_findings(
    node: str,
    destinations: Mapping[str, tuple[int, tuple[str, ...]]],
    free: Mapping[str, tuple[int, str]],
) -> list[Finding]:
    """A blocking finding for each filesystem the copies onto it do not fit on (destinations on
    one filesystem add up). A destination whose space could not be read is not judged."""
    by_mount: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    for destination, (size, labels) in destinations.items():
        if destination not in free:
            continue
        available, mount = free[destination]
        total, _, seen = by_mount.get(mount, (0, available, ()))
        by_mount[mount] = (
            total + size,
            available,
            seen + tuple(x for x in labels if x not in seen),
        )
    return [
        Finding(
            Level.BLOCK,
            node,
            ", ".join(labels),
            f"not enough free space on {mount}: the copies need {human_size(need)}, "
            f"{human_size(available)} is free",
        )
        for mount, (need, available, labels) in sorted(by_mount.items())
        if need > available
    ]


def move_paths(moves: Sequence[Move]) -> list[tuple[Move, str, str]]:
    """(move, source now, target now): each move's paths as they are before apply starts."""
    result: list[tuple[Move, str, str]] = []
    for i, move in enumerate(moves):
        before = [m for m in moves[:i] if m.node == move.node]
        result.append((move, where_now(move.source, before), where_now(move.target, before)))
    return result


def _move_findings(
    moves: Sequence[Move],
    results: Mapping[str, Mapping[str, PathInfo]],
    replace_stale: bool,
    desired: Mapping[str, Desired],
) -> list[Finding]:
    found: list[Finding] = []
    for move, source, target in move_paths(moves):
        if move.carried:
            continue
        if move.kind == VOLUME:
            root = desired[move.node].dirs.appdata_dir
            source, target = data_spec(root, move.old), data_spec(root, move.new)
            src = results.get(move.node, {}).get(source)
            dst = results.get(move.node, {}).get(target)
            label = ", ".join(move.services)
            if src is not None and not src.has_data:
                found.append(
                    Finding(
                        Level.WARN,
                        move.node,
                        label,
                        f"{key_label(move.old)} is {src.kind}: nothing to copy",
                    )
                )
            if dst is not None and dst.kind in ("broken", "other", "file") and is_volume(move.old):
                found.append(
                    Finding(Level.BLOCK, move.node, label, f"{target} already exists as {dst.kind}")
                )
            elif dst is not None and dst.has_data:
                found.append(
                    Finding(
                        Level.INFO if replace_stale else Level.BLOCK,
                        move.node,
                        label,
                        f"{key_label(move.new)} already has data ({describe(dst)}); it will be "
                        f"archived to {archive_path(root, move.new)}-<timestamp> before the copy",
                        flag="" if replace_stale else "--replace-stale",
                    )
                )
            continue
        src = results.get(move.node, {}).get(source)
        dst = results.get(move.node, {}).get(target)
        label = ", ".join(move.services) if move.kind == DATA else ""
        if src is not None and not src.has_data:
            what = "cannot be inspected" if src.kind == "unreadable" else f"is {src.kind}"
            found.append(Finding(Level.WARN, move.node, label, f"{source} {what}: nothing to move"))
        if dst is None:
            continue
        if dst.kind in ("broken", "other", "file"):
            found.append(
                Finding(Level.BLOCK, move.node, label, f"{target} already exists as {dst.kind}")
            )
        elif dst.has_data and move.kind == DATA:
            # Archived inside the (post-move) APPDATA_DIR the rename runs in.
            archived = archive_path(move.target.removesuffix("/" + move.new), move.new)
            found.append(
                Finding(
                    Level.INFO if replace_stale else Level.BLOCK,
                    move.node,
                    label,
                    f"{target} already exists ({describe(dst)}); it will be archived to "
                    f"{archived}-<timestamp> before the move",
                    flag="" if replace_stale else "--replace-stale",
                )
            )
        elif dst.has_data:
            found.append(
                Finding(
                    Level.BLOCK,
                    move.node,
                    "",
                    f"{move.kind} target {target} already exists ({describe(dst)}); move or "
                    "remove it by hand first",
                )
            )
    return found


def probe(remote: Remote, paths: Sequence[tuple[str, str]]) -> dict[str, PathInfo]:
    """paths: (path, mode) pairs; see _PROBE."""
    if not paths:
        return {}
    return parse_probe(remote.run_script(_PROBE, *(f"{mode}:{path}" for path, mode in paths)))


# The volumes that exist, then every volume's size. Read-only: no container is run (mounting a
# missing volume would create it).
_VOLUMES = 'docker volume ls -q && echo "--" && docker system df -v --format json'


def probe_volumes(remote: Remote, keys: Sequence[str]) -> dict[str, PathInfo]:
    """Volume keys -> missing, empty or dir (has data), with the size."""
    if not keys:
        return {}
    return parse_volumes(remote.run_script(_VOLUMES), keys)


def parse_volumes(output: str, keys: Sequence[str]) -> dict[str, PathInfo]:
    listed, _, usage = output.partition("--\n")
    existing = set(listed.split())
    sizes: dict[str, int | None] = {}
    try:
        for row in json.loads(usage or "{}").get("Volumes") or []:
            sizes[str(row.get("Name"))] = parse_size(str(row.get("Size", "")))
    except ValueError:
        pass
    result: dict[str, PathInfo] = {}
    for key in keys:
        name = volume_name(key)
        size = sizes.get(name)
        if name not in existing:
            result[key] = PathInfo("missing")
        else:
            result[key] = PathInfo("empty" if size == 0 else "dir", size=size)
    return result


def parse_size(text: str) -> int | None:
    """Docker's sizes: 0B, 3B, 12.3kB, 1.049MB, 2GB (powers of 1000)."""
    match = re.fullmatch(r"([\d.]+)\s*([kKMGT]?)B", text.strip())
    if not match:
        return None
    scale = {"": 1, "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    return int(float(match[1]) * scale[match[2]])


def parse_probe(output: str) -> dict[str, PathInfo]:
    result: dict[str, PathInfo] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        path, kind, size, newest, device = [*fields, "", ""][:5]
        result[path] = PathInfo(
            kind=kind,
            size=int(size) if size else None,
            newest=datetime.fromtimestamp(int(newest), UTC) if newest else None,
            device=device.strip(),
        )
    return result


def _findings_for(
    op: Operation,
    desired: Mapping[str, Desired],
    recorded: Mapping[str, NodeState | None],
    results: Mapping[str, Mapping[str, PathInfo]],
    replace_stale: bool,
) -> list[Finding]:
    found: list[Finding] = []

    def info(node: str, path: str) -> PathInfo | None:
        return results.get(node, {}).get(path)

    def missing_outside(node: str, service: str) -> None:
        for path in _outside_root(desired[node], service):
            p = info(node, path)
            if p and p.kind in ("missing", "broken"):
                found.append(
                    Finding(
                        Level.WARN,
                        node,
                        service,
                        f"{path} does not exist; nothing creates it (Docker would make an empty "
                        "root-owned directory)",
                    )
                )

    match op:
        case Add(service, node):
            missing_outside(node, service)
            for path in _data_paths(desired[node], service):
                p = info(node, path)
                if p and p.has_data:
                    found.append(
                        Finding(
                            Level.INFO,
                            node,
                            service,
                            f"{path} already has data ({describe(p)}); it will be used",
                        )
                    )
            for key in _volume_keys(desired[node], service):
                p = info(node, key)
                if p and p.has_data:
                    found.append(
                        Finding(
                            Level.INFO,
                            node,
                            service,
                            f"{key_label(key)} already has data ({describe(p)}); it will be used",
                        )
                    )
        case Update(service, node):
            missing_outside(node, service)
        case Migrate(_, source, target, removed, added):
            seen: set[str] = set()
            for service in added:
                missing_outside(target, service)
            for path in migrate_source_paths(op, desired, recorded):
                p = info(source, path)
                if path in seen or not p or p.has_data or p.kind == "unreadable":
                    continue
                seen.add(path)
                found.append(
                    Finding(
                        Level.WARN,
                        source,
                        ", ".join(removed),
                        f"{path} is {p.kind} on the source: nothing to copy",
                    )
                )
            for key in (key for key in op.source_data if is_volume(key)):
                p = info(source, key)
                if p and not p.has_data:
                    found.append(
                        Finding(
                            Level.WARN,
                            source,
                            ", ".join(removed),
                            f"{key_label(key)} is {p.kind} on the source: nothing to copy",
                        )
                    )
            for service in added:
                for key in _volume_keys(desired[target], service):
                    p = info(target, key)
                    if key in seen or not p or not p.has_data:
                        continue
                    seen.add(key)
                    found.append(
                        Finding(
                            Level.INFO if replace_stale else Level.BLOCK,
                            target,
                            service,
                            f"{key_label(key)} already exists on the target ({describe(p)}); it "
                            f"will be archived to "
                            f"{archive_path(desired[target].dirs.appdata_dir, key)}-<timestamp> "
                            "before the copy",
                            flag="" if replace_stale else "--replace-stale",
                        )
                    )
            targets = [
                (service, path)
                for service in added
                for path in _data_paths(desired[target], service)
            ]
            for service, path in targets:
                p = info(target, path)
                if p is None or path in seen:
                    continue
                seen.add(path)
                if p.kind in ("broken", "other"):
                    found.append(
                        Finding(
                            Level.BLOCK, target, service, f"{path} exists on the target as {p.kind}"
                        )
                    )
                elif p.has_data:
                    found.append(
                        Finding(
                            Level.INFO if replace_stale else Level.BLOCK,
                            target,
                            service,
                            f"{path} already exists on the target ({describe(p)}); it will be "
                            f"archived to {_archive_prefix(desired[target], path)}-<timestamp> "
                            "before the copy",
                            flag="" if replace_stale else "--replace-stale",
                        )
                    )
        case Restore(service, node, entries=entries):
            root = desired[node].dirs.appdata_dir
            for _, key in entries:
                p = info(node, data_spec(root, key))
                if p and p.has_data:
                    found.append(
                        Finding(
                            Level.INFO if replace_stale else Level.BLOCK,
                            node,
                            service,
                            f"{key_label(key)} already has data ({describe(p)}); it will be "
                            f"archived to {archive_path(root, key)}-<timestamp> before the "
                            f"restore (or --fresh {service} uses it as it is)",
                            flag="" if replace_stale else "--replace-stale",
                        )
                    )
        case Remove():
            pass
    return found


def describe(p: PathInfo) -> str:
    parts = [p.kind if p.kind != "dir" else "directory"]
    if p.size is not None:
        parts.append(human_size(p.size))
    if p.newest is not None:
        parts.append(f"last modified {p.newest:%Y-%m-%d}")
    return ", ".join(parts)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def archive_path(root: str, key: str) -> str:
    """Where data is archived: <key> in <root>'s archive, a volume as volume-<name> (the executor
    appends -<timestamp>)."""
    return str(PurePosixPath(root) / ARCHIVE_DIR / archive_name(key))


def archive_name(key: str) -> str:
    return f"volume-{volume_name(key)}" if is_volume(key) else key


def key_label(key: str) -> str:
    """A data key as the plan shows it: ``plex/`` or ``volume homelab-frodo_pgdata``."""
    return f"volume {volume_name(key)}" if is_volume(key) else f"{key}/"


def data_spec(root: str, key: str) -> str:
    """What a probe (and docker -v) takes for a data key: the absolute path, or the volume key."""
    return key if is_volume(key) else str(PurePosixPath(root) / key)


def _archive_prefix(desired: Desired, path: str) -> str:
    root = desired.dirs.appdata_dir
    return archive_path(root, str(PurePosixPath(path).relative_to(root)))


def _binds(desired: Desired, service: str) -> list[str]:
    definition = desired.service_config(service)
    sources: list[str] = []
    for mount in definition.get("volumes") or []:
        if mount.get("type") == "bind" and mount["source"] not in sources:
            sources.append(mount["source"])
    return sources


def _outside_root(desired: Desired, service: str) -> list[str]:
    root = desired.dirs.appdata_dir
    return [s for s in _binds(desired, service) if not PurePosixPath(s).is_relative_to(root)]


def _volume_keys(desired: Desired, service: str) -> list[str]:
    found = desired.state.services.get(service)
    return [volume_key(volume) for volume in found.volumes] if found else []


def _data_paths(desired: Desired, service: str) -> list[str]:
    root = PurePosixPath(desired.dirs.appdata_dir)
    found = desired.state.services.get(service)
    return [str(root / rel) for rel in found.data_paths] if found else []


def migrate_source_paths(
    op: Migrate, desired: Mapping[str, Desired], recorded: Mapping[str, NodeState | None]
) -> list[str]:
    """What a migrate copies: the leaving services' data paths, under the source's own names, in
    its post-move layout (the desired root)."""
    state = recorded.get(op.source)
    if state is None or op.source not in desired:
        return []
    names = dict(op.renamed)
    root = PurePosixPath(desired[op.source].dirs.appdata_dir)
    keys = dict.fromkeys(
        data_path
        for service in op.removed
        if service in state.services
        for data_path in state.services[service].data_paths
    )
    return [str(root / names.get(key, key)) for key in keys]
