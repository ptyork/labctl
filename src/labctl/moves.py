"""Moves on a node: relocated roots (DEPLOY_DIR, APPDATA_DIR) and renamed data directories.

Pure functions, like the planner. A move is detected by comparing what was applied with what the
repo wants on the *same node*:

- Roots: the recorded DEPLOY_DIR / APPDATA_DIR against the desired values. Either may sit inside
  the other (cirdan keeps APPDATA_DIR inside DEPLOY_DIR), so the moves are ordered so that no move
  carries away a directory an earlier one already put in place.
- Data: a service whose mount at the same container path now points at a different directory
  under APPDATA_DIR (``/config``: ``plexms`` -> ``plex``). The container path is the identity; a
  service that changes both is just new data.
- Data renamed while the service changes node: no move on the source; the migrate carries the
  mapping instead (``MovePlan.aliases``) and copies from the old name.
- Named volumes take part by their real names (``volume:homelab-frodo_pgdata``). A stack's own
  volumes carry its project name, so a service moving to another stack (or node) mounts another
  volume at the same container path: a rename. With a volume on either side a rename is a copy
  (kind VOLUME), the old side archived after it.

The recorded state is then rewritten as if the moves had happened, so the planner sees plain
updates (or migrates) instead of one data group disappearing and another appearing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from labctl.state import Dirs, NodeState, Service, is_volume, overlaps, under

HOME, APPDATA, DATA, VOLUME = "DEPLOY_DIR", "APPDATA_DIR", "data", "volume"


@dataclass(frozen=True)
class Move:
    """Move ``source`` to ``target`` on ``node``, in plan order.

    kind HOME / APPDATA: ``old``/``new`` are the configured values. kind DATA: ``old``/``new`` are
    keys under APPDATA_DIR and ``services`` the services whose mounts changed. kind VOLUME: data
    keys too, a volume on at least one side; copied, then the old side archived (``source`` and
    ``target`` are the keys). ``source`` is where
    the directory is when this move runs (earlier moves may have carried it), ``target`` where it
    ends up. ``carried`` means an earlier move already put it in place; nothing to do.
    """

    node: str
    kind: str
    old: str
    new: str
    source: str
    target: str
    services: tuple[str, ...] = ()
    carried: bool = False

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.node,)


@dataclass(frozen=True)
class MoveConflict:
    """A detected move labctl cannot do safely. Blocks apply."""

    node: str
    message: str


@dataclass(frozen=True)
class NotRenamed:
    """A service's mount now points elsewhere, but the old directory stays: other services still
    use it. The service starts with whatever is at the new path."""

    node: str
    old: str
    new: str
    services: tuple[str, ...]
    kept: tuple[str, ...]


@dataclass(frozen=True)
class UnevenRename:
    """Mounts that shared a top-level directory end up in different ones: plexms/config ->
    plex/config but plexms/icons stays. Done as asked, but usually a typo."""

    node: str
    service: str
    old: str  # top-level directory before
    new: tuple[str, ...]  # top-level directories its mounts end up in
    target: str = ""  # set when the service also changes node


@dataclass(frozen=True)
class FannedOut:
    """A service with volumes leaves a node for several: nothing to pick, so its volumes are not
    copied (it starts empty on each; the source's are archived)."""

    node: str
    service: str
    targets: tuple[str, ...]


MoveNotice = NotRenamed | UnevenRename | FannedOut


@dataclass(frozen=True)
class MovePlan:
    moves: tuple[Move, ...] = ()
    conflicts: tuple[MoveConflict, ...] = ()
    notices: tuple[MoveNotice, ...] = ()
    recorded: Mapping[str, NodeState | None] = field(default_factory=dict)
    # Source node -> data key as the target names it -> the key on the source (renamed migrates).
    aliases: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


def plan_moves(
    desired: Mapping[str, NodeState],
    recorded: Mapping[str, NodeState | None],
    desired_dirs: Mapping[str, Dirs],
    recorded_dirs: Mapping[str, Dirs],
) -> MovePlan:
    """
    Moves on each node: relocated roots (DEPLOY_DIR, APPDATA_DIR) and renamed data.

    Args:
        desired (Mapping[str, NodeState]):
            Every node the repo defines, as the repo wants it.
        recorded (Mapping[str, NodeState | None]):
            Each node's current state (None when unknown).
        desired_dirs (Mapping[str, Dirs]):
            Each node's DEPLOY_DIR and APPDATA_DIR as the repo has them.
        recorded_dirs (Mapping[str, Dirs]):
            Each node's DEPLOY_DIR and APPDATA_DIR as last applied (or found).

    Returns:
        MovePlan:
            The moves in the order they run, conflicts (which block apply), notices, the recorded
            state as it is after the moves, and the data keys renamed on the way by migrates.
    """
    moves: list[Move] = []
    conflicts: list[MoveConflict] = []
    notices: list[MoveNotice] = []
    rewritten = dict(recorded)

    for node in sorted(desired):
        have = recorded.get(node)
        if have is None:
            continue
        new_dirs = desired_dirs[node]
        if node in recorded_dirs:
            roots, conflict = root_moves(node, recorded_dirs[node], new_dirs)
            moves += roots
            conflicts += [conflict] if conflict else []
        renames, node_conflicts, node_notices = _renames(node, desired[node], have)
        conflicts += node_conflicts
        notices += node_notices
        for name in sorted(desired[node].services.keys() & have.services.keys()):
            notices += uneven_renames(node, name, have.services[name], desired[node].services[name])
        for old, new, services in renames:
            if is_volume(old) or is_volume(new):
                moves.append(Move(node, VOLUME, old, new, old, new, services))
                continue
            root = PurePosixPath(new_dirs.appdata_dir)
            moves.append(Move(node, DATA, old, new, str(root / old), str(root / new), services))
        if renames:
            rewritten[node] = _rewrite(have, {old: new for old, new, _ in renames})

    aliases, moved, cross_conflicts, cross_notices = _cross_node(desired, recorded)
    conflicts += cross_conflicts
    notices += cross_notices
    for source, leaving in moved.items():
        state = rewritten[source] or NodeState.of(source)
        rewritten[source] = replace(
            state, services={**state.services, **{svc.name: svc for svc in leaving}}
        )
    return MovePlan(tuple(moves), tuple(conflicts), tuple(notices), rewritten, aliases)


#############################################################################


# Roots


#############################################################################


def root_moves(node: str, old: Dirs, new: Dirs) -> tuple[list[Move], MoveConflict | None]:
    """Order the root moves so no move carries a directory already put in place.

    Both roots take part even when unchanged: an unchanged APPDATA_DIR inside a moving
    DEPLOY_DIR gets carried along and has to be moved back.
    """
    if old == new:
        return [], None
    wanted = [(HOME, old.deploy_dir, new.deploy_dir), (APPDATA, old.appdata_dir, new.appdata_dir)]
    for order in (wanted, wanted[::-1]):
        result = _simulate(node, order)
        if result is not None:
            return result, None
    return [], MoveConflict(
        node,
        f"DEPLOY_DIR {old.deploy_dir} → {new.deploy_dir} and APPDATA_DIR {old.appdata_dir} → "
        f"{new.appdata_dir} cannot be moved without one carrying the other; move one per apply",
    )


def _simulate(node: str, order: list[tuple[str, str, str]]) -> list[Move] | None:
    done: list[tuple[str, str]] = []  # (source, target) of moves made so far
    placed: list[str] = []  # directories already where they belong
    moves: list[Move] = []
    for kind, old, new in order:
        source = old
        for src, dst in done:
            source = rebase(source, src, dst)
        if source == new:
            if old != new:
                moves.append(Move(node, kind, old, new, source, new, carried=True))
            placed.append(new)
            continue
        if any(under(path, source) for path in placed) or under(new, source):
            return None
        moves.append(Move(node, kind, old, new, source, new))
        done.append((source, new))
        placed.append(new)
    return moves


#############################################################################


# Data renames


#############################################################################


def _renames(
    node: str, want: NodeState, have: NodeState
) -> tuple[list[tuple[str, str, tuple[str, ...]]], list[MoveConflict], list[NotRenamed]]:
    """(old, new, services) renames on one node, plus what could not be renamed."""
    candidates: dict[str, dict[str, set[str]]] = {}  # old -> new -> services
    for name in sorted(want.services.keys() & have.services.keys()):
        before = dict(have.services[name].mounts)
        after = dict(want.services[name].mounts)
        for target in sorted(before.keys() & after.keys()):
            if before[target] != after[target]:
                candidates.setdefault(before[target], {}).setdefault(after[target], set()).add(name)

    conflicts: list[MoveConflict] = []
    pairs: dict[str, tuple[str, set[str]]] = {}
    for old, choices in sorted(candidates.items()):
        if len(choices) > 1:
            conflicts.append(
                MoveConflict(
                    node,
                    f"{old}/ would be renamed to more than one directory: "
                    + ", ".join(
                        f"{n}/ ({', '.join(sorted(s))})" for n, s in sorted(choices.items())
                    ),
                )
            )
            continue
        ((new, names),) = choices.items()
        pairs[old] = (new, names)

    # A nested rename implied by its parent's (plexms -> plex covers plexms/x -> plex/x) is dropped;
    # one that contradicts it makes the whole rename impossible.
    nested: set[str] = set()
    for old, (new, names) in sorted(pairs.items()):
        parents = [pair for pair in pairs if under(old, pair)]
        if not parents:
            continue
        nested.add(old)
        parent = min(parents, key=len)  # the outermost rename is the one that runs
        if rebase(old, parent, pairs[parent][0]) != new:
            nested.add(parent)
            conflicts.append(
                MoveConflict(
                    node,
                    f"{old}/ → {new}/ contradicts {parent}/ → {pairs[parent][0]}/ "
                    f"(rename {parent}/ alone, or keep the layout inside it)",
                )
            )
        else:
            pairs[parent][1].update(names)
    renames = [
        (old, new, tuple(sorted(names)))
        for old, (new, names) in sorted(pairs.items())
        if old not in nested
    ]

    notices: list[NotRenamed] = []
    olds = {old for old, _, _ in renames}
    news = {new for _, new, _ in renames}
    kept_renames: list[tuple[str, str, tuple[str, ...]]] = []
    for old, new, services in renames:
        if under(new, old) or under(old, new):
            conflicts.append(
                MoveConflict(node, f"{old}/ → {new}/: a directory cannot move into itself")
            )
            continue
        if any(overlaps(new, o) for o in olds if o != old) or any(
            overlaps(old, n) for n in news if n != new
        ):
            conflicts.append(
                MoveConflict(
                    node,
                    f"{old}/ → {new}/ chains or swaps with another rename; do one per apply",
                )
            )
            continue
        # The old directory stays when a service that is not renamed still wants it.
        kept = tuple(
            sorted(
                name
                for name, svc in want.services.items()
                if name not in services
                and any(overlaps(data_key, old) for data_key in svc.data_keys)
            )
        )
        if kept:
            notices.append(NotRenamed(node, old, new, services, kept))
            continue
        # The new directory must not be another service's data already.
        users = sorted(
            name
            for name, svc in have.services.items()
            if name not in services and any(overlaps(data_key, new) for data_key in svc.data_keys)
        )
        if users:
            conflicts.append(
                MoveConflict(node, f"{old}/ → {new}/: {new}/ is already used by {', '.join(users)}")
            )
            continue
        kept_renames.append((old, new, services))
    return kept_renames, conflicts, notices


def _cross_node(
    desired: Mapping[str, NodeState], recorded: Mapping[str, NodeState | None]
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, list[Service]],
    list[MoveConflict],
    list[UnevenRename | FannedOut],
]:
    """Services leaving a node for another one whose data directories also change name.

    Nothing moves on the source. Instead the source's recorded service is rewritten to the target's
    names, so the planner pairs the data into a migrate as usual, and an alias (target name ->
    source name) tells the migrate what to copy from. Returns (aliases per source node, rewritten
    services per source node, conflicts, uneven-rename and fan-out notices).
    """
    aliases: dict[str, dict[str, str]] = {}
    rewritten: dict[str, list[Service]] = {}
    conflicts: list[MoveConflict] = []
    notices: list[UnevenRename | FannedOut] = []
    for source, have in sorted(recorded.items()):
        if have is None:
            continue
        staying = desired[source].services if source in desired else {}
        for name, before in sorted(have.services.items()):
            if name in staying:
                continue
            targets = sorted(
                n for n, want in desired.items() if n != source and name in want.services
            )
            changed = {
                target: (dict(before.mounts), dict(desired[target].services[name].mounts))
                for target in targets
            }
            renamed = {
                t: {m: (old[m], new[m]) for m in old.keys() & new.keys() if old[m] != new[m]}
                for t, (old, new) in changed.items()
            }
            renamed = {t: r for t, r in renamed.items() if r}
            if not renamed:
                continue
            if len(targets) > 1 and all(
                is_volume(old) or is_volume(new)
                for r in renamed.values()
                for old, new in r.values()
            ):
                # Volumes differ per node by project name: a fan-out, as with directories.
                notices.append(FannedOut(source, name, tuple(targets)))
                continue
            if len(targets) > 1:
                conflicts.append(
                    MoveConflict(
                        source,
                        f"{name} leaves {source} for {', '.join(targets)} and renames its data; "
                        "it cannot be migrated to more than one node",
                    )
                )
                continue
            (target,) = targets
            mapping: dict[str, str] = {}  # source name -> target name
            split = False
            for _, (old, new) in sorted(renamed[target].items()):
                if mapping.setdefault(old, new) != new:
                    split = True
                    conflicts.append(
                        MoveConflict(
                            source,
                            f"{name}: {old}/ on {source} would go to both {mapping[old]}/ and "
                            f"{new}/ on {target}",
                        )
                    )
            users = sorted(
                other
                for other, svc in [*have.services.items(), *staying.items()]
                if other != name
                and any(overlaps(data_key, old) for data_key in svc.data_keys for old in mapping)
            )
            if users and not split:
                conflicts.append(
                    MoveConflict(
                        source,
                        f"{name} moves to {target} with renamed data, but "
                        f"{', '.join(sorted(set(users)))} also "
                        f"{'uses' if len(set(users)) == 1 else 'use'} "
                        f"{', '.join(f'{o}/' for o in sorted(mapping))} on {source}; it cannot "
                        "take shared data under a new name",
                    )
                )
            if users or split:
                continue
            aliases.setdefault(source, {}).update({new: old for old, new in mapping.items()})
            rename = dict(mapping)
            leaving = before.with_keys(lambda k: rename.get(k, k))  # noqa: B023 (called at once)
            rewritten.setdefault(source, []).append(leaving)
            after = desired[target].services[name]
            notices += [
                replace(n, target=target) for n in uneven_renames(source, name, before, after)
            ]
    return aliases, rewritten, conflicts, notices


def uneven_renames(node: str, name: str, before: Service, after: Service) -> list[UnevenRename]:
    """A top-level directory whose mounts end up in more than one place (usually a typo)."""
    old, new = dict(before.mounts), dict(after.mounts)
    tops: dict[str, set[str]] = {}
    for m in old.keys() & new.keys():
        tops.setdefault(_top(old[m]), set()).add(_top(new[m]))
    return [
        UnevenRename(node, name, top, tuple(sorted(news)))
        for top, news in sorted(tops.items())
        if len(news) > 1
    ]


def _top(key: str) -> str:
    return PurePosixPath(key).parts[0]


def _rewrite(state: NodeState, renames: Mapping[str, str]) -> NodeState:
    """Recorded state as if the renames had already happened (fingerprints unchanged)."""

    def moved(key: str) -> str:
        for old, new in renames.items():
            if under(key, old) or key == old:
                return rebase(key, old, new)
        return key

    services = {name: svc.with_keys(moved) for name, svc in state.services.items()}
    return replace(state, services=services)


#############################################################################


# Paths


#############################################################################


def rebase(path: str, old: str, new: str) -> str:
    """``path`` after ``old`` moved to ``new`` (unchanged when not inside ``old``)."""
    if path == old:
        return new
    if under(path, old):
        return str(PurePosixPath(new) / PurePosixPath(path).relative_to(old))
    return path


def where_now(path: str, moves: list[Move] | tuple[Move, ...]) -> str:
    """Where a path in the final (post-move) layout is before any move runs."""
    for move in reversed(moves):
        if not move.carried:
            path = rebase(path, move.target, move.source)
    return path


def move_paths(moves: Sequence[Move]) -> list[tuple[Move, str, str]]:
    """(move, source now, target now): each move's paths as they are before apply starts."""
    result: list[tuple[Move, str, str]] = []
    for i, move in enumerate(moves):
        before = [m for m in moves[:i] if m.node == move.node]
        result.append((move, where_now(move.source, before), where_now(move.target, before)))
    return result
