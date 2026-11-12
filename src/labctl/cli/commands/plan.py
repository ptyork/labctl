"""labctl plan and labctl apply: one code path. plan is apply --dry-run."""

from __future__ import annotations

import getpass
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import click
from rich.markup import escape

from labctl.apply import (
    ApplyError,
    Executor,
    Node,
    NodeWork,
    RollbackFailed,
    lock,
    node_work,
    unlock,
)
from labctl.checks import (
    Finding,
    Level,
    PathInfo,
    check_paths,
)
from labctl.cli import menu
from labctl.cli.commands.settings import load_settings
from labctl.cli.git_sync import (
    branch_notices,
    catch_up,
    commit_applied,
    commit_message,
    publish_applied,
)
from labctl.cli.prompts import ask_restore, prompt_value
from labctl.cli.render import (
    choice_label,
    describe,
    remote_sharing,
    render_drift,
    render_files,
    render_findings,
    render_gone,
    render_moves,
    render_nodes,
    render_notices,
    render_port_moves,
    render_problems,
    render_repo_notices,
    render_runtime,
    render_stacks,
    render_summary,
)
from labctl.click import DebugOptions, Options, PlanOptions
from labctl.collect import Collected, collect
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import EnvError
from labctl.links import split_links
from labctl.moves import (
    MovePlan,
    plan_moves,
)
from labctl.planner import (
    Add,
    Plan,
    Remove,
    make_plan,
)
from labctl.ports import PortMove, lab_clashes, moved_ports
from labctl.progress import Progress
from labctl.remote import Remote, RemoteError, node_remote
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.restore import Ask, Restores, find_archives, plan_restores
from labctl.runtime import STOP, Answers, Runtime, analyse, resolve
from labctl.settings import Secrets, Settings, SettingsError
from labctl.state import Scope


@click.command(name="plan")
@PlanOptions(all_by_default=True).options
@click.option(
    "--detailed-exitcode",
    is_flag=True,
    help="Exit 0 when apply would change nothing, 1 on an error or a blocked plan, 2 when it "
    "would change something.",
)
@DebugOptions().options
def plan_cmd(options: Options, detailed_exitcode: bool = False, **kwargs: Any) -> None:
    """Show what apply would change on the nodes named, or 'all' (apply --dry-run).

    No node named means 'all'. Plans the lab config as it is now, uncommitted changes included.
    The nodes not named are read too, so a service moving between them is seen: a migrate needs
    both of its nodes named. Never changes a node.
    """
    changes = _run(options, dry_run=True)
    if detailed_exitcode and changes:
        sys.exit(2)


@click.command(name="apply")
@PlanOptions().options
@click.option("--dry-run", is_flag=True, help="Only show the plan (the same as `labctl plan`).")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Apply without asking: the plan is approved and other questions take their defaults "
    "(--ff to fast-forward). Conflicts need a terminal, or --stop-unmanaged/--defer-blocked.",
)
@click.option(
    "--wait",
    default=120,
    show_default=True,
    metavar="SECONDS",
    help="How long services may take to come up.",
)
@click.option(
    "--message",
    "-m",
    metavar="MESSAGE",
    help="Message for the commit made once applied (default: asked in a terminal, else generated).",
)
@DebugOptions().options
def apply_cmd(
    options: Options,
    dry_run: bool = False,
    yes: bool = False,
    wait: int = 120,
    message: str | None = None,
    **kwargs: Any,
) -> None:
    """Plan, then make the nodes named, or 'all', match the lab config. Rolls back if it fails.

    Applies the lab config as it is now, uncommitted changes included. Once everything is up, the
    changes are committed (the only time labctl commits), each node is tagged
    applied/<node>/<timestamp>, and the branch and tags are pushed.
    """
    _run(options, dry_run=dry_run, yes=yes, wait=wait, message=message)


def _run(
    options: Options,
    dry_run: bool,
    yes: bool = False,
    wait: int = 120,
    message: str | None = None,
) -> bool:
    """plan → (confirm) → lock → plan again → apply → commit → unlock → tag.

    Returns:
        bool:
            With ``dry_run``, whether apply would change anything (any node with work, a record
            to write included); otherwise False.
    """
    settings = load_settings()
    try:
        repo = ConfigRepo.open(settings.lab_config)
        defined = settings.node_names()
        # A node being retired (applied, its folder gone) can be named until its retiring apply.
        nodes = {*defined, *repo.retiring(defined)}
        scope = Scope.build(nodes, options.nodes)
    except (SettingsError, ValueError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc

    secrets = Secrets.load(settings.secrets_file)
    prompt = prompt_value if sys.stdin.isatty() else None
    try:
        fetched = repo.fetch()
        catch_up(repo, options, ask=sys.stdin.isatty() and not yes, applying=not dry_run)
        notices = branch_notices(repo, fetched)
        # apply pins the working tree as it is now: what it plans, ships, and commits once applied
        tree = None if dry_run else repo.snapshot()
        with collect(settings, scope, secrets, prompt, repo, tree) as collected:
            ask = ask_restore if sys.stdin.isatty() and not (dry_run or yes) else None
            planned = _plan_and_render(collected, options, notices, ask)
            if planned.reasons:
                sys.exit(1)
            if dry_run:
                return bool(planned.works)
            targets = [work.node for work in planned.works]
        if not targets:
            console.print("Nothing to apply.")
            return False
        planned, asked = _answer_conflicts(planned, options, sys.stdin.isatty())
        if (planned.confirm or asked) and not yes:
            _confirm(planned.confirm)
        _apply(
            settings,
            scope,
            secrets,
            planned.options or options,
            planned,
            targets,
            wait,
            repo,
            partial(
                commit_message, repo, planned.base, planned.tree, options.nodes, message, not yes
            ),
        )
        render_port_moves(planned.port_moves, "Published ports that changed")
        return False
    except (ComposeError, EnvError, ApplyError, RemoteError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        Remote.close_all(nodes)


def _answer_conflicts(planned: Planned, options: Options, terminal: bool) -> tuple[Planned, bool]:
    """
    Ask about each conflict the flags left unanswered (--yes answers none of them).

    Args:
        planned (Planned):
            The plan.
        options (Options):
            The options it was planned with (when the plan kept none).
        terminal (bool):
            Whether there is a terminal to ask in.

    Returns:
        tuple[Planned, bool]:
            The plan with the answers (in its options too: apply plans again with them), and
            whether anything was asked.

    Raises:
        click.ClickException:
            A conflict is left without an answer: no terminal, or the apply was cancelled.
    """
    unresolved = planned.runtime.unresolved
    if not unresolved:
        return planned, False
    if not terminal:
        n = len(unresolved)
        raise click.ClickException(
            f"The plan has {n} conflict{'s' if n > 1 else ''} with what labctl does not manage "
            "(listed above): answer in a terminal, or pass --stop-unmanaged or --defer-blocked"
        )
    options = planned.options or options
    stop, defer = list(options.stop), list(options.defer)
    console.print()
    for conflict in unresolved:
        choices = [*conflict.choices, ""]  # "": cancel
        labels = [choice_label(conflict, choice) for choice in choices]
        index = menu.choose(f"In the way on {conflict.node}", labels, header=describe(conflict))
        if index is None or not choices[index]:
            raise click.ClickException("Not applied")
        (stop if choices[index] == STOP else defer).append(conflict.key)
    options = replace(options, stop=tuple(stop), defer=tuple(defer))
    runtime = resolve(planned.runtime, _answers(options))
    works = tuple(replace(work, runtime=runtime.for_node(work.node)) for work in planned.works)
    return replace(planned, runtime=runtime, works=works, options=options), True


def _answers(options: Options) -> Answers:
    return Answers(options.stop, options.defer, options.stop_unmanaged, options.defer_blocked)


def _confirm(reasons: list[str]) -> None:
    """Ask in a terminal; without one, --yes is needed."""
    if not sys.stdin.isatty():
        raise click.ClickException(
            f"The plan has {', '.join(reasons)}; confirm in a terminal, or pass --yes"
        )
    console.print()
    if not menu.confirm("Apply plan?"):
        raise click.ClickException("Not applied")


def _apply(
    settings: Settings,
    scope: Scope,
    secrets: Secrets,
    options: Options,
    planned: Planned,
    targets: list[str],
    wait: int,
    repo: ConfigRepo,
    message: Callable[[], str] = lambda: "labctl apply",
) -> None:
    """Lock the nodes, plan again (the nodes and the lab config must not have changed), apply node
    by node; once everything is up, commit what was applied (``message`` gives the message) and
    record it; unlock; then move the branch to the commit, tag each node recorded and push the
    tags."""
    assert planned.tree is not None  # apply plans a snapshot, never the working tree itself
    remotes = {node: node_remote(node) for node in targets}
    by = f"{getpass.getuser()}@{socket.gethostname()}"
    owner = f"{by} {datetime.now().isoformat(timespec='seconds')}"
    locked: list[str] = []
    stuck = False  # the rollback failed: the locks stay
    executor: Executor | None = None
    tags: list[tuple[str, TagInfo]] = []
    try:
        for node in targets:
            lock(remotes[node], owner)
            locked.append(node)
        head = repo.head()
        if head != planned.base:
            raise ApplyError(
                f"HEAD moved from {planned.base[:7]} to {head[:7]} since the plan; run apply again"
            )
        if repo.snapshot() != planned.tree:
            raise ApplyError("The lab config changed since the plan; run apply again")
        with collect(settings, scope, secrets, None, repo, planned.tree, locked=True) as again:
            if again.errors:
                raise ApplyError(
                    "Cannot plan again under the lock:\n  " + "\n  ".join(again.errors)
                )
            computed = _compute(again, options)
            if computed.works is None:
                raise ApplyError(computed.work_error)
            # What runs counts too: a container or a port that got in the way since, or a stack
            # started or stopped by hand.
            now = (computed.plan, _files(again), computed.runtime)
            if now != (planned.plan, planned.files, planned.runtime):
                raise ApplyError("The homelab changed since it was planned; run apply again")
            tree, base = planned.tree, planned.base
            executor = Executor(
                settings,
                console.print,
                wait,
                options.replace_stale,
                tree=tree,
                commit_with=lambda: commit_applied(repo, tree, base, message),
                progress=Progress(console.print, console),
            )
            console.print()
            try:
                executor.run(
                    {node: Node(remote) for node, remote in remotes.items()},
                    computed.works,
                    computed.plan,
                )
            except RollbackFailed:
                stuck = True
                raise
            except ApplyError as exc:  # already shown, with the rollback
                raise click.ClickException("Not applied: everything was rolled back") from exc
            when = executor.when.isoformat(timespec="seconds")
            for work in computed.works:
                if work.node not in executor.recorded:
                    continue
                up = (
                    computed.runtime.running.get(work.node, frozenset())
                    if work.record_only
                    else work.runtime.start
                )
                info = TagInfo(
                    when,
                    by,
                    _version(),
                    tuple(sorted(up)),
                    work.desired.dirs,
                    work.desired.env.host_ip,
                    retired=work.node in again.retiring,
                )
                tags.append((work.node, info))
    finally:
        if not stuck:
            for node in locked:
                unlock(remotes[node])
    if executor is not None:
        advanced = repo.advance(executor.commit, planned.base)
        if not advanced:
            console.print(
                f"[yellow]! {repo.branch() or 'HEAD'} moved during the apply: the commit applied, "
                f"{executor.commit[:7]}, is not on it (its tags name it). Merge it:\n"
                f"  git -C {repo.root} merge {executor.commit[:12]}[/]"
            )
        committed = executor.commit != planned.base
        publish_applied(repo, executor.commit, executor.stamp, tags, advanced, committed)


def _version() -> str:
    try:
        return version("labctl")
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class Planned:
    plan: Plan
    reasons: list[str]  # why apply is blocked
    confirm: list[str]  # what apply asks about first
    files: object  # bundles and node files planned against: must not change before applying
    runtime: Runtime = Runtime()
    works: tuple[NodeWork, ...] = ()
    scope: Scope = Scope()
    tree: str | None = None  # the snapshot planned (None: plan, the working tree)
    base: str = ""  # HEAD when planned: the snapshot is committed on it
    options: Options | None = None  # with the restore choices made
    port_moves: tuple[PortMove, ...] = ()  # shown again once applied


def _files(collected: Collected) -> object:
    return (
        {node: d.files for node, d in collected.desired.items()},
        # Only the named nodes': the others are only looked at.
        _named(collected.node_files, collected.scope),
        _named(collected.node_names, collected.scope),
        _named(collected.projects, collected.scope),
    )


@dataclass(frozen=True)
class Computed:
    plan: Plan
    move_plan: MovePlan
    findings: list[Finding]
    probes: dict[str, dict[str, PathInfo]]
    runtime: Runtime
    works: tuple[NodeWork, ...] | None  # None: apply cannot do this plan (work_error says why)
    work_error: str = ""
    restores: Restores = Restores((), (), (), (), ())


def _restores(collected: Collected, plan: Plan, options: Options, ask: Ask | None) -> Restores:
    """Archived data for the services added: found on every node read, named or not (only when
    one is stateful)."""
    states = {node: d.state for node, d in collected.desired.items()}
    adds = [op for op in plan.operations if isinstance(op, Add)]
    if not any(states[op.node].services[op.service].stateful for op in adds):
        return Restores((), (), (), (), ())
    roots = {
        # A node not named keeps its archive where its last apply put it.
        node: (
            d.dirs if collected.scope.in_scope(node) else collected.recorded_dirs[node]
        ).appdata_dir
        for node, d in collected.desired.items()
        if node in collected.containers  # read
    }
    archived = find_archives(roots, node_remote)
    return plan_restores(plan, states, archived, options.fresh, options.restore, ask)


def _compute(collected: Collected, options: Options, ask: Ask | None = None) -> Computed:
    scope = collected.scope
    desired_states = {node: d.state for node, d in collected.desired.items()}
    move_plan = plan_moves(
        desired_states,
        collected.recorded,
        {node: d.dirs for node, d in collected.desired.items()},
        collected.recorded_dirs,
    )
    plan = make_plan(
        desired_states,
        collected.recorded,
        scope,
        _resolve_services(collected, options.no_migrate),
        move_plan,
    )
    restores = _restores(collected, plan, options, ask)
    if restores.operations:
        plan = replace(plan, operations=(*plan.operations, *restores.operations))
    findings, probes = check_paths(
        plan.operations,
        collected.desired,
        move_plan.recorded,
        replace_stale=options.replace_stale,
        moves=plan.moves,
    )
    try:
        works: list[NodeWork] | None = node_work(plan, collected, scope)
        error = ""
    except ApplyError as exc:
        works, error = None, str(exc)
    runtime = analyse(
        collected.desired,
        collected.recorded,
        plan,
        containers=_named(collected.containers, scope),
        listeners=_named(collected.listeners, scope),
        missing=_named(collected.missing, scope),
        work={work.node for work in works or () if not work.record_only},
        answers=_answers(options),
    )
    with_runtime = (
        tuple(replace(work, runtime=runtime.for_node(work.node)) for work in works)
        if works is not None
        else None
    )
    return Computed(plan, move_plan, findings, probes, runtime, with_runtime, error, restores)


def _plan_and_render(
    collected: Collected,
    options: Options,
    branch_notices: list[str] | None = None,
    ask: Ask | None = None,
) -> Planned:
    scope = collected.scope
    repo_notices = [*(branch_notices or []), *collected.repo_notices]
    render_nodes(collected)
    if collected.errors:
        console.print("\n[bold red]Cannot plan:[/]")
        for error in collected.errors:
            console.print(f"  [red]✗[/] {escape(error)}")
        return Planned(Plan(), ["errors above"], [], None)

    computed = _compute(collected, options, ask)
    restores = computed.restores
    # The choices made (asked, or the defaults) are what apply plans again under the lock.
    options = replace(options, fresh=(*options.fresh, *restores.fresh), restore=restores.chosen)
    plan, findings, probes, runtime = (
        computed.plan,
        computed.findings,
        computed.probes,
        computed.runtime,
    )
    work_nodes = {work.node for work in computed.works or ()}
    placeholders = {
        n: d.env.placeholders for n, d in collected.desired.items() if d.env.placeholders
    }

    # The lab config's own: two of a node's services publishing one port, and a service linked
    # to a container the lab config runs on another node. `up` would fail on either.
    named = [node for node in sorted(collected.desired) if scope.in_scope(node)]
    lab_problems = [
        *(clash for node in named for clash in lab_clashes(collected.desired[node])),
        *split_links(collected.desired, named),
    ]
    render_problems(plan.problems, placeholders, lab_problems)
    render_drift(collected)
    files_changed = render_files(collected, scope, options.verbose)
    render_stacks(plan, collected, runtime, work_nodes, probes, options.verbose)
    render_moves(plan, probes, options.verbose)
    trash = render_gone(collected, scope)
    for note in restores.notes:
        console.print(f"  [cyan]i[/] {escape(note)}")
    for error in restores.errors:
        console.print(f"  [red]✗[/] {escape(error)}")
    render_notices(plan.notices)
    shared = remote_sharing(
        {node: d.state for node, d in collected.desired.items()}, collected.recorded
    )
    if shared:
        console.print("\n[bold]Remote storage[/]")
        for note in shared:
            console.print(f"  [yellow]![/] {escape(note)}")
    render_repo_notices(repo_notices)
    render_findings(findings)
    render_runtime(runtime)
    port_moves = moved_ports(
        {node: d.state for node, d in collected.desired.items()},
        collected.recorded,
        work_nodes,
    )
    render_port_moves(port_moves, "Published ports that change")

    reasons = []
    if plan.problems or placeholders or restores.errors or lab_problems:
        reasons.append("problems above")
    blocking = [finding for finding in findings if finding.level is Level.BLOCK]
    if blocking:
        reasons.append(f"{len(blocking)} check{'s' if len(blocking) > 1 else ''} failed")
    if runtime.blocked:
        reasons.append("conflicts apply cannot resolve")
    if computed.works is None:
        console.print(f"\n[red]✗[/] {escape(computed.work_error)}")
        reasons.append("a migrate and a move on one node")
    render_summary(plan, files_changed, reasons, runtime.conflicts)
    at = "the commit applied" if collected.tree else "HEAD"
    for work in computed.works or ():
        if work.record_only:
            console.print(
                f"  {work.node}: nothing to change; apply records {at} there, and tags it"
            )

    confirm = []
    removed = sum(isinstance(op, Remove) for op in plan.operations)
    if removed:
        confirm.append(f"{removed} service{'s' if removed > 1 else ''} removed")
    if findings:
        confirm.append(f"{len(findings)} check{'s' if len(findings) > 1 else ''}")
    if plan.notices:
        confirm.append(f"{len(plan.notices)} notice{'s' if len(plan.notices) > 1 else ''}")
    if shared:
        confirm.append("remote storage newly shared between nodes")
    if collected.drift:
        confirm.append("node edits to discard")
    if trash:
        confirm.append(f"{', '.join(trash)} taken down for good (files gone)")
    first = [work.node for work in computed.works or () if _source(collected, work.node) == "first"]
    if first:
        confirm.append(f"a first apply on {', '.join(first)}")
    retiring = sorted(node for node in collected.retiring if scope.in_scope(node))
    if retiring:
        confirm.append(f"{', '.join(retiring)} retired")
    if repo_notices:
        n = len(repo_notices)
        confirm.append(f"{n} repo notice{'s' if n > 1 else ''}")
    return Planned(
        plan,
        reasons,
        confirm,
        _files(collected),
        runtime,
        computed.works or (),
        scope,
        collected.tree,
        collected.base,
        options,
        tuple(port_moves),
    )


def _named[T](found: Mapping[str, T], scope: Scope) -> dict[str, T]:
    """Of what was read on each node, the named nodes': only they are analysed for apply."""
    return {node: value for node, value in found.items() if scope.in_scope(node)}


def _source(collected: Collected, node: str) -> str:
    return next((report.source for report in collected.reports if report.node == node), "")


#############################################################################


# Rendering


#############################################################################


def _resolve_services(collected: Collected, names: tuple[str, ...]) -> set[str]:
    """--no-migrate values must be service names the plan knows about."""
    known = {
        name
        for state in [
            *(desired.state for desired in collected.desired.values()),
            *(state for state in collected.recorded.values() if state),
        ]
        for name in state.services
    }
    unknown = sorted(set(names) - known)
    if unknown:
        raise click.ClickException(f"--no-migrate: unknown service(s): {', '.join(unknown)}")
    return set(names)


#############################################################################


# Prompting


#############################################################################
