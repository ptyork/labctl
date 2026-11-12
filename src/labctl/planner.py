"""The planner: a pure function from (desired, recorded, scope) to a plan.

Nothing here touches the network or the filesystem. State collection builds the inputs; the
executor consumes the output.

Services are planned per node: add, remove, update or unchanged. Data is planned separately. A
piece of data is identified by its path under the managed root (or its named volume), not by the
service that mounts it; services whose data overlaps form one data group. A data group is
**migrated** only when it lives on exactly one node before and exactly one different node after.
The services carrying it may differ between the two sides (plex-nvidia on A, plex-intel on B). In
every other case no data moves, and the plan says so.

Data left behind with no service using it on that node (after a remove, or as a migrate's source)
is archived, never deleted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from labctl.moves import FannedOut, Move, MoveConflict, MoveNotice, MovePlan, UnevenRename
from labctl.state import NodeState, Scope, Service, is_volume, overlaps, under

##############################################################################

# Operations

##############################################################################


@dataclass(frozen=True)
class Add:
    service: str
    node: str

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.node,)


@dataclass(frozen=True)
class Remove:
    service: str
    node: str
    archive: tuple[str, ...] = ()  # data paths archived on the node (no other service uses them)

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.node,)


@dataclass(frozen=True)
class Update:
    """Same node, changed definition: redeploy in place."""

    service: str
    node: str

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.node,)


@dataclass(frozen=True)
class Migrate:
    """Move a data group from source to target; the source's copy is archived afterwards."""

    data: tuple[str, ...]  # data keys: "plexms" (a path under APPDATA_DIR) or "volume:<name>"
    source: str
    target: str
    removed: tuple[str, ...]  # services leaving the source
    added: tuple[str, ...]  # services starting on the target
    # (key, key on the source) where the source names the data differently (renamed on the way).
    renamed: tuple[tuple[str, str], ...] = ()

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.source, self.target)

    @property
    def source_data(self) -> tuple[str, ...]:
        """``data`` as the source names it: what is copied from there."""
        names = dict(self.renamed)
        return tuple(names.get(key, key) for key in self.data)

    @property
    def archive(self) -> tuple[str, ...]:
        return archived_keys(self.source_data)


@dataclass(frozen=True)
class Restore:
    """Archived data back for a service added to ``node``, before it starts: from ``source``'s
    archive (APPDATA_DIR ``root`` there), made at ``stamp``. Only ``node`` changes; the source's
    archive is only read (on the same node, a directory is moved back into place)."""

    service: str
    node: str
    source: str
    stamp: str
    root: str
    entries: tuple[tuple[str, str], ...]  # (entry under archive/, data key on the node)
    reason: str = ""  # why it was archived: removed, migrated, renamed, replaced
    size: int | None = None

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.node,)


Operation = Add | Remove | Update | Migrate | Restore


#############################################################################


# Notices: data that is deliberately not moved


#############################################################################


@dataclass(frozen=True)
class NotMigrated:
    """Services starting on another node without a data group's existing data.

    reason:
      "shared"     the data stays on the source because other services still use it there. The
                   services in ``moved`` used it with them and now start elsewhere without it.
                   Usually a mistake: apply asks for explicit confirmation.
      "duplicate"  another instance of a service that keeps running (with its data) on the source.
      "fanout"     the data's services leave the source for more than one node; nothing to pick.
      "no-migrate" a migrate turned into a fresh start by --no-migrate.
    """

    data: tuple[str, ...]
    source: str
    reason: str
    fresh: tuple[tuple[str, str], ...]  # (service, node) starting without the data
    kept: tuple[str, ...] = ()  # services still using the data on the source
    moved: tuple[str, ...] = ()  # services that left the source


#############################################################################


# Problems (any problem blocks apply)


#############################################################################


@dataclass(frozen=True)
class MissingBaseline:
    """A node in scope whose current state could not be read, so nothing can be diffed against
    it. (A skipped node without one is assumed to run what the repo says.)"""

    node: str


@dataclass(frozen=True)
class UnknownNode:
    """Recorded state exists for a node the repo no longer defines."""

    node: str


@dataclass(frozen=True)
class Assumed:
    """A skipped node never applied: planned as if it ran what the repo says. A service moving
    from it would show as a plain add (without its data)."""

    node: str


@dataclass(frozen=True)
class NotNamed:
    """A migrate between a named node and one that is not: it needs both, so nothing is applied
    (name the other node too, or --no-migrate the service)."""

    migrate: Migrate
    missing: str  # the node not named


Problem = MissingBaseline | UnknownNode | MoveConflict | NotNamed


#############################################################################


# Plan


#############################################################################


@dataclass(frozen=True)
class Plan:
    moves: tuple[Move, ...] = ()  # run first, in this order
    operations: tuple[Operation, ...] = ()
    notices: tuple[NotMigrated | MoveNotice | Assumed, ...] = ()
    problems: tuple[Problem, ...] = ()
    unchanged: tuple[tuple[str, str], ...] = ()  # (service, node)


def make_plan(
    desired: Mapping[str, NodeState],
    recorded: Mapping[str, NodeState | None],
    scope: Scope | None = None,
    no_migrate: Iterable[str] = (),
    move_plan: MovePlan | None = None,
) -> Plan:
    """
    Diff desired against recorded state across the whole homelab.

    Args:
        desired (Mapping[str, NodeState]):
            Every node the repo defines, as the repo wants it.
        recorded (Mapping[str, NodeState | None]):
            Every node labctl knows about: its current state if in scope, its latest apply tag's
            if skipped, or None when neither exists.
        scope (Scope | None):
            The nodes that may change (default: all). What touches only the others is left
            out; a migrate between one and another is a problem.
        no_migrate (Iterable[str]):
            Services whose data must not move: a migrate involving one becomes a fresh start
            (remove + add) (default: none).
        move_plan (MovePlan | None):
            From ``plan_moves``: replaces ``recorded`` with its post-move version and adds its
            moves (default: None).

    Returns:
        Plan:
            The moves, operations, notices and problems on the nodes in scope. Any problem
            blocks apply.
    """
    scope = scope or Scope()
    no_migrate = set(no_migrate)
    aliases: Mapping[str, Mapping[str, str]] = {}
    if move_plan is not None:
        recorded = move_plan.recorded
        aliases = move_plan.aliases
    assumed = [
        node for node in sorted(desired) if recorded.get(node) is None and not scope.in_scope(node)
    ]
    recorded = {**recorded, **{node: desired[node] for node in assumed}}

    baseline_problems: list[Problem] = [
        *(UnknownNode(node) for node in sorted(set(recorded) - set(desired))),
        *(MissingBaseline(node) for node in sorted(desired) if recorded.get(node) is None),
    ]
    if baseline_problems:
        # Without every node's baseline a migrate looks like a plain add; plan nothing.
        return Plan(problems=tuple(baseline_problems))

    want = {node: state.services for node, state in desired.items()}
    have = {node: state.services for node, state in recorded.items() if state is not None}

    operations, unchanged = _per_service(want, have)
    groups = _data_groups(want, have)
    notices: list[NotMigrated] = []
    migrated_from: set[tuple[int, str]] = set()  # (group index, source node)
    for index, group in enumerate(groups):
        before = group.nodes(HAVE)
        after = group.nodes(WANT)
        if len(before) != 1:
            continue  # new data, or data already on several nodes: nothing to move
        source = next(iter(before))
        removed = group.services_on(HAVE, source)
        targets = sorted(after - before)
        added = {node: group.services_on(WANT, node) for node in targets}
        movers = tuple(
            service for service in removed if service not in group.services_on(WANT, source)
        )

        if source not in after and len(targets) == 1:
            target = targets[0]
            if no_migrate & {*removed, *added[target]}:
                notices.append(
                    NotMigrated(group.keys, source, "no-migrate", _fresh(added), moved=movers)
                )
                continue
            names = aliases.get(source, {})
            renamed = tuple((key, names[key]) for key in group.keys if key in names)
            migrate = Migrate(group.keys, source, target, removed, added[target], renamed)
            absorbed = {(service, source) for service in removed} | {
                (service, target) for service in added[target]
            }
            operations = [
                op
                for op in operations
                if not (isinstance(op, Add | Remove) and (op.service, op.node) in absorbed)
            ] + [migrate]
            migrated_from.add((index, source))
        elif targets:
            kept = group.services_on(WANT, source)
            reason = "fanout" if not kept else ("shared" if movers else "duplicate")
            notices.append(NotMigrated(group.keys, source, reason, _fresh(added), kept, movers))

    operations = _with_archives(operations, groups, migrated_from, aliases)

    # Only the nodes named change. A migrate needs both of its nodes: with one left out it is a
    # problem, never half done.
    not_named = [
        NotNamed(op, node)
        for op in operations
        if isinstance(op, Migrate) and scope.in_scope(op.source) != scope.in_scope(op.target)
        for node in op.nodes
        if not scope.in_scope(node)
    ]
    return Plan(
        moves=tuple(
            move for move in (move_plan.moves if move_plan else ()) if scope.in_scope(move.node)
        ),
        operations=tuple(
            op
            for op in sorted(operations, key=_op_order)
            if all(scope.in_scope(node) for node in op.nodes)
        ),
        notices=(
            *(Assumed(node) for node in assumed),
            *(
                notice
                for notice in (*(move_plan.notices if move_plan else ()), *notices)
                if any(scope.in_scope(node) for node in _notice_nodes(notice))
            ),
        ),
        problems=(
            *(c for c in (move_plan.conflicts if move_plan else ()) if scope.in_scope(c.node)),
            *not_named,
        ),
        unchanged=tuple(unchanged),
    )


def _notice_nodes(notice: NotMigrated | MoveNotice) -> tuple[str, ...]:
    """The nodes a notice is about: it is shown when one of them is named."""
    match notice:
        case NotMigrated():
            return (notice.source, *(node for _, node in notice.fresh))
        case FannedOut():
            return (notice.node, *notice.targets)
        case UnevenRename(target=target) if target:
            return (notice.node, target)
        case _:
            return (notice.node,)


def top_level_paths(keys: Iterable[str]) -> tuple[str, ...]:
    """Path keys not nested inside another of the keys (volumes excluded): what a copy or an
    archive takes as directories."""
    paths = sorted(key for key in keys if not is_volume(key))
    return tuple(p for p in paths if not any(under(p, path) for path in paths))


def archived_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """What gets archived: the top-level paths, then the volumes."""
    keys = list(keys)
    return (*top_level_paths(keys), *sorted(dict.fromkeys(key for key in keys if is_volume(key))))


#############################################################################


# Per-service planning


#############################################################################


def _per_service(
    want: Mapping[str, Mapping[str, Service]], have: Mapping[str, Mapping[str, Service]]
) -> tuple[list[Operation], list[tuple[str, str]]]:
    operations: list[Operation] = []
    unchanged: list[tuple[str, str]] = []
    for node in sorted(want.keys() | have.keys()):
        wanted = want.get(node, {})
        current = have.get(node, {})
        for name in sorted(wanted.keys() | current.keys()):
            if name not in current:
                operations.append(Add(name, node))
            elif name not in wanted:
                operations.append(Remove(name, node))
            elif wanted[name].fingerprint != current[name].fingerprint:
                operations.append(Update(name, node))
            else:
                unchanged.append((name, node))
    return operations, unchanged


def _with_archives(
    operations: list[Operation],
    groups: list[_Group],
    migrated_from: set[tuple[int, str]],
    aliases: Mapping[str, Mapping[str, str]],
) -> list[Operation]:
    """Attach archiving to removes whose data no service on that node uses any more.

    One remove per (data group, node) carries the archive; data still used on the node, or moving
    away as part of a migrate, is left alone. Archived paths use the node's own names (``aliases``
    maps a renamed service's keys back to them).
    """
    claimed: set[tuple[int, str]] = set()
    result: list[Operation] = []
    for op in sorted(operations, key=_op_order):
        if isinstance(op, Remove):
            for index, group in enumerate(groups):
                if (HAVE, op.node, op.service) not in group.members:
                    continue
                key = (index, op.node)
                still_used = bool(group.services_on(WANT, op.node))
                if still_used or key in migrated_from or key in claimed:
                    continue
                claimed.add(key)
                names = aliases.get(op.node, {})
                paths = archived_keys(names.get(key, key) for key in group.keys_on(HAVE, op.node))
                op = Remove(op.service, op.node, (*op.archive, *paths))
        result.append(op)
    return result


def _op_order(op: Operation) -> tuple[str, str]:
    match op:
        case Migrate(data, source):
            return (data[0], source)
        case (
            Add(service, node) | Remove(service, node) | Update(service, node)
            | Restore(service, node)
        ):  # fmt: skip
            return (service, node)


#############################################################################


# Data groups


#############################################################################

WANT, HAVE = "want", "have"


@dataclass
class _Group:
    keys: tuple[str, ...]
    members: set[tuple[str, str, str]] = field(default_factory=set)  # (side, node, service)
    member_keys: dict[tuple[str, str, str], tuple[str, ...]] = field(default_factory=dict)

    def nodes(self, side: str) -> set[str]:
        return {node for s, node, _ in self.members if s == side}

    def services_on(self, side: str, node: str) -> tuple[str, ...]:
        return tuple(sorted(svc for s, n, svc in self.members if s == side and n == node))

    def keys_on(self, side: str, node: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    key
                    for m, keys in self.member_keys.items()
                    if m[:2] == (side, node)
                    for key in keys
                }
            )
        )


def _data_groups(
    want: Mapping[str, Mapping[str, Service]], have: Mapping[str, Mapping[str, Service]]
) -> list[_Group]:
    """Connected components of data keys: keys of one service, and overlapping paths, belong
    together.

    Keys are node-independent (paths relative to each node's managed root), so the same data on
    two nodes, or on both sides of the diff, lands in the same group.
    """
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    occurrences: list[tuple[tuple[str, str, str], tuple[str, ...]]] = []
    for tag, side in ((WANT, want), (HAVE, have)):
        for node, services in side.items():
            for name, service in services.items():
                keys = service.data_keys
                if keys:
                    occurrences.append(((tag, node, name), keys))
                    for key in keys:
                        union(key, keys[0])

    all_keys = sorted(parent)
    for i, a in enumerate(all_keys):
        for b in all_keys[i + 1 :]:
            if overlaps(a, b):
                union(a, b)

    groups: dict[str, _Group] = {}
    for key in all_keys:
        group = groups.setdefault(find(key), _Group(keys=()))
        group.keys = (*group.keys, key)
    for member, keys in occurrences:
        group = groups[find(keys[0])]
        group.members.add(member)
        group.member_keys[member] = keys
    return sorted(groups.values(), key=lambda g: g.keys)


def _fresh(added: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (service, node) for node, services in sorted(added.items()) for service in services
    )
