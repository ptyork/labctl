"""From what was collected to everything plan shows and apply needs: the planning pipeline.

    moves (moves.py) → the plan (planner.py) → restores (restore.py) → path and space checks
    (checks.py) → each node's work (apply.node_work) → what runs and what is in the way
    (runtime.py) → what blocks apply, and what it asks about first.

Nothing here prints or asks (restore.Ask is a callback): the commands show a ``Planned``
(cli/render.py) and ask its questions. Reading the nodes for the checks and for archived data is
the only I/O. apply runs this twice: once to show the plan, and again under the nodes' locks, where
``same_as`` must hold.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from labctl.apply import ApplyError, NodeWork, node_work
from labctl.checks import Finding, Level, PathInfo, check_paths
from labctl.collect import Collected
from labctl.links import split_links
from labctl.moves import MovePlan, plan_moves
from labctl.planner import Add, Plan, Remove, make_plan
from labctl.ports import PortMove, lab_clashes, moved_ports
from labctl.remote import node_remote
from labctl.restore import Ask, Restores, find_archives, plan_restores
from labctl.runtime import Answers, Runtime, analyse, resolve
from labctl.state import Dirs, NodeState


class PlanError(Exception):
    """An option names something the plan does not have."""


@dataclass(frozen=True)
class Options:
    """What plan and apply share: the scope, and the choices the plan is made with."""

    nodes: tuple[str, ...]
    replace_stale: bool
    no_migrate: tuple[str, ...]
    verbose: bool
    fresh: tuple[str, ...] = ()
    restore: tuple[str, ...] = ()  # SERVICE or SERVICE@STAMP
    ff: bool = False
    stop_unmanaged: bool = False
    defer_blocked: bool = False
    # Conflicts answered in a terminal, by key (runtime.Conflict).
    stop: tuple[str, ...] = ()
    defer: tuple[str, ...] = ()

    @property
    def answers(self) -> Answers:
        """How these options answer the conflicts."""
        return Answers(self.stop, self.defer, self.stop_unmanaged, self.defer_blocked)


@dataclass(frozen=True)
class Planned:
    """A plan and everything known about it."""

    plan: Plan = Plan()
    move_plan: MovePlan = MovePlan()
    findings: tuple[Finding, ...] = ()
    probes: Mapping[str, dict[str, PathInfo]] = field(default_factory=dict)
    runtime: Runtime = Runtime()
    # What apply does on each node; None: apply cannot do this plan (``work_error`` says why).
    works: tuple[NodeWork, ...] | None = ()
    work_error: str = ""
    restores: Restores = Restores()
    # The options with the restore choices made (and the conflicts answered): what apply plans
    # again with, under the lock.
    options: Options | None = None
    # Problems of the lab config itself: variables nothing gives a value, per node; two services
    # publishing one port; a service linked to a container on another node.
    placeholders: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    lab_problems: tuple[str, ...] = ()
    shared: tuple[str, ...] = ()  # remote storage newly mounted on more than one node
    trash: tuple[str, ...] = ()  # NODE/PROJECT taken down for good: its files are gone
    port_moves: tuple[PortMove, ...] = ()  # shown again once applied
    repo_notices: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()  # why apply is blocked
    confirm: tuple[str, ...] = ()  # what apply asks about first
    # The bundles and node files planned against: they must not change before applying.
    files: object = None
    tree: str | None = None  # the snapshot planned (None: plan, the working tree)
    base: str = ""  # HEAD when planned: the snapshot is committed on it

    @property
    def work_nodes(self) -> set[str]:
        return {work.node for work in self.works or ()}

    def same_as(self, other: Planned) -> bool:
        """Whether apply would do the same: the plan, the files it is made from and replaces, and
        what runs (a container or a port that got in the way since, a stack started or stopped by
        hand)."""
        return (self.plan, self.files, self.runtime) == (other.plan, other.files, other.runtime)


def make_planned(
    collected: Collected,
    options: Options,
    notices: Sequence[str] = (),
    ask: Ask | None = None,
) -> Planned:
    """
    Plan what was collected, and work out what blocks apply and what it asks about.

    Args:
        collected (Collected):
            Every node's desired and recorded state, and what was read on the nodes.
        options (Options):
            The choices the plan is made with.
        notices (Sequence[str]):
            What the caller found about the lab config's branch (default: nothing).
        ask (Ask | None):
            Asks which archived copy to restore (default: the newest is restored, and said so).

    Returns:
        Planned:
            The plan. With ``collected.errors`` there is none: only the reason it is blocked.

    Raises:
        PlanError:
            ``--no-migrate`` names a service the plan does not have.
    """
    if collected.errors:
        return Planned(reasons=("errors above",), tree=collected.tree, base=collected.base)
    scope = collected.scope
    desired_states = collected.desired_states
    move_plan = plan_moves(
        desired_states, collected.recorded, _dirs(collected), collected.recorded_dirs
    )
    plan = make_plan(
        desired_states,
        collected.recorded,
        scope,
        _known_services(collected, options.no_migrate),
        move_plan,
    )
    restores = _restores(collected, plan, options, ask)
    if restores.operations:
        plan = replace(plan, operations=(*plan.operations, *restores.operations))
    # The choices made (asked, or the defaults) are what apply plans again under the lock.
    options = replace(options, fresh=(*options.fresh, *restores.fresh), restore=restores.chosen)
    findings, probes = check_paths(
        plan.operations,
        collected.desired,
        move_plan.recorded,
        replace_stale=options.replace_stale,
        moves=plan.moves,
    )
    works: tuple[NodeWork, ...] | None
    try:
        works, work_error = tuple(node_work(plan, collected, scope)), ""
    except ApplyError as exc:
        works, work_error = None, str(exc)
    read = collected.read(scope)  # only the named nodes are analysed for apply
    runtime = analyse(
        collected.desired,
        collected.recorded,
        plan,
        containers={node: on_node.containers for node, on_node in read.items()},
        listeners={node: on_node.listeners for node, on_node in read.items()},
        missing={node: on_node.missing for node, on_node in read.items()},
        work={work.node for work in works or () if not work.record_only},
        answers=options.answers,
    )
    if works is not None:
        works = tuple(replace(work, runtime=runtime.for_node(work.node)) for work in works)
    planned = Planned(
        plan=plan,
        move_plan=move_plan,
        findings=tuple(findings),
        probes=probes,
        runtime=runtime,
        works=works,
        work_error=work_error,
        restores=restores,
        options=options,
        repo_notices=(*notices, *collected.repo_notices),
        files=_files(collected),
        tree=collected.tree,
        base=collected.base,
    )
    return _assessed(planned, collected)


def answered(planned: Planned, stop: Iterable[str], defer: Iterable[str]) -> Planned:
    """
    The plan with conflicts answered in a terminal.

    Args:
        planned (Planned):
            The plan (made with options).
        stop (Iterable[str]):
            The keys of the conflicts answered "stop it" / "take it down".
        defer (Iterable[str]):
            The keys of those answered "don't start".

    Returns:
        Planned:
            The plan with the answers in its options (apply plans again with them), its runtime
            and each node's work.
    """
    assert planned.options is not None
    options = replace(
        planned.options,
        stop=(*planned.options.stop, *stop),
        defer=(*planned.options.defer, *defer),
    )
    runtime = resolve(planned.runtime, options.answers)
    works = tuple(
        replace(work, runtime=runtime.for_node(work.node)) for work in planned.works or ()
    )
    return replace(planned, runtime=runtime, works=works, options=options)


#############################################################################

# What blocks apply, and what it asks about

#############################################################################


def _assessed(planned: Planned, collected: Collected) -> Planned:
    """The plan with the lab config's own problems, what is only pointed out, why apply is blocked
    and what it asks about first."""
    scope, plan = collected.scope, planned.plan
    placeholders = {
        node: wanted.env.placeholders
        for node, wanted in collected.desired.items()
        if wanted.env.placeholders
    }
    # Two of a node's services publishing one port, and a service linked to a container the lab
    # config runs on another node: `up` would fail on either.
    named = [node for node in sorted(collected.nodes) if scope.in_scope(node)]
    lab_problems = (
        *(clash for node in named for clash in lab_clashes(collected.nodes[node].desired)),
        *split_links(collected.desired, named),
    )
    shared = tuple(remote_sharing(collected.desired_states, collected.recorded))
    trash = tuple(f"{node}/{name}" for node, name, taken_over in gone(collected) if not taken_over)
    port_moves = moved_ports(collected.desired_states, collected.recorded, planned.work_nodes)

    reasons = []
    if plan.problems or placeholders or planned.restores.errors or lab_problems:
        reasons.append("problems above")
    blocking = sum(finding.level is Level.BLOCK for finding in planned.findings)
    if blocking:
        reasons.append(f"{blocking} check{'s' if blocking > 1 else ''} failed")
    if planned.runtime.blocked:
        reasons.append("conflicts apply cannot resolve")
    if planned.works is None:
        reasons.append("a migrate and a move on one node")

    confirm = []
    removed = sum(isinstance(op, Remove) for op in plan.operations)
    if removed:
        confirm.append(f"{removed} service{'s' if removed > 1 else ''} removed")
    if planned.findings:
        checks = len(planned.findings)
        confirm.append(f"{checks} check{'s' if checks > 1 else ''}")
    if plan.notices:
        confirm.append(f"{len(plan.notices)} notice{'s' if len(plan.notices) > 1 else ''}")
    if shared:
        confirm.append("remote storage newly shared between nodes")
    if any(view.drift for view in collected.nodes.values()):
        confirm.append("node edits to discard")
    if trash:
        confirm.append(f"{', '.join(trash)} taken down for good (files gone)")
    first = [node for node in sorted(planned.work_nodes) if collected.nodes[node].source == "first"]
    if first:
        confirm.append(f"a first apply on {', '.join(first)}")
    retiring = sorted(n for n, view in collected.nodes.items() if view.retiring and n in named)
    if retiring:
        confirm.append(f"{', '.join(retiring)} retired")
    if planned.repo_notices:
        count = len(planned.repo_notices)
        confirm.append(f"{count} repo notice{'s' if count > 1 else ''}")
    return replace(
        planned,
        placeholders=placeholders,
        lab_problems=lab_problems,
        shared=shared,
        trash=trash,
        port_moves=tuple(port_moves),
        reasons=tuple(reasons),
        confirm=tuple(confirm),
    )


def gone(collected: Collected) -> list[tuple[str, str, bool]]:
    """
    The projects running from DEPLOY_DIR whose files are gone, on the named nodes.

    Returns:
        list[tuple[str, str, bool]]:
            (node, project, taken over): a stack of the lab config with its name takes its
            containers over; any other is taken down for good.
    """
    return [
        (node, name, name in view.desired.state.stacks)
        for node, view in sorted(collected.nodes.items())
        if collected.scope.in_scope(node)
        for name in view.gone
    ]


def remote_sharing(
    desired: Mapping[str, NodeState], recorded: Mapping[str, NodeState | None]
) -> list[str]:
    """Remote storage this plan has mounted on more nodes than before (the services on each
    must be able to share it: a database on NFS used from two nodes is how it gets corrupted)."""

    def users(states: Iterable[NodeState | None]) -> dict[str, dict[str, list[str]]]:
        found: dict[str, dict[str, list[str]]] = {}
        for state in states:
            if state is None:
                continue
            for service in state.services.values():
                for _, where in service.remote:
                    found.setdefault(where, {}).setdefault(state.node, []).append(service.name)
        return found

    want, have = users(desired.values()), users(recorded.values())
    notes = []
    for where, nodes in sorted(want.items()):
        if len(nodes) > 1 and not set(nodes) <= set(have.get(where, {})):
            on = "; ".join(f"{node} ({', '.join(sorted(s))})" for node, s in sorted(nodes.items()))
            notes.append(
                f"{where} is mounted on {len(nodes)} nodes: {on}. Make sure what uses it can "
                "share it (one database on NFS written from two nodes is how it gets corrupted)"
            )
    return notes


#############################################################################

# Pieces

#############################################################################


def _dirs(collected: Collected) -> dict[str, Dirs]:
    return {node: view.desired.dirs for node, view in collected.nodes.items()}


def _files(collected: Collected) -> object:
    """What the plan is made from and what apply replaces. Of the nodes, only the named ones':
    the others are only looked at."""
    read = collected.read(collected.scope)
    return (
        {node: view.desired.files for node, view in collected.nodes.items()},
        {node: (on_node.files, on_node.names) for node, on_node in read.items() if on_node.exists},
        {node: frozenset(on_node.projects) for node, on_node in read.items() if on_node.exists},
    )


def _restores(collected: Collected, plan: Plan, options: Options, ask: Ask | None) -> Restores:
    """Archived data for the services added: found on every node read, named or not (only when
    one is stateful)."""
    states = collected.desired_states
    adds = [op for op in plan.operations if isinstance(op, Add)]
    if not any(states[op.node].services[op.service].stateful for op in adds):
        return Restores()
    roots = {}
    for node, view in collected.nodes.items():
        if view.on_node is None:  # not read
            continue
        # A node not named keeps its archive where its last apply put it.
        named = collected.scope.in_scope(node)
        dirs = view.desired.dirs if named or view.recorded_dirs is None else view.recorded_dirs
        roots[node] = dirs.appdata_dir
    archived = find_archives(roots, node_remote)
    return plan_restores(plan, states, archived, options.fresh, options.restore, ask)


def _known_services(collected: Collected, names: tuple[str, ...]) -> set[str]:
    """--no-migrate values must be service names the plan knows about."""
    known = {
        name
        for state in [*collected.desired_states.values(), *collected.recorded.values()]
        if state is not None
        for name in state.services
    }
    unknown = sorted(set(names) - known)
    if unknown:
        raise PlanError(f"--no-migrate: unknown service(s): {', '.join(unknown)}")
    return set(names)
