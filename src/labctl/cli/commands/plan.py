"""labctl plan and labctl apply: one code path. plan is apply --dry-run."""

from __future__ import annotations

import getpass
import socket
import sys
from collections.abc import Callable
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
    remote_sharing,
    render_drift,
    render_files,
    render_findings,
    render_held,
    render_moves,
    render_nodes,
    render_notices,
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
from labctl.progress import Progress
from labctl.remote import Remote, RemoteError, node_remote
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.restore import Ask, Restores, find_archives, plan_restores
from labctl.runtime import Runtime, analyse
from labctl.settings import Secrets, Settings, SettingsError
from labctl.state import Scope


@click.command(name="plan")
@PlanOptions().options
@DebugOptions().options
def plan_cmd(options: Options, **kwargs: Any) -> None:
    """Show what apply would change on the nodes named, or 'all' (apply --dry-run).

    Plans the lab config as it is now, uncommitted changes included. The nodes not named are
    planned too (as of their last apply tag), so a service moving between them is seen; what
    touches them is held. Never changes a node.
    """
    _run(options, dry_run=True)


@click.command(name="apply")
@PlanOptions().options
@click.option("--dry-run", is_flag=True, help="Only show the plan (the same as `labctl plan`).")
@click.option(
    "--yes",
    "--force",
    "-y",
    "yes",
    is_flag=True,
    help=("Do not ask before applying a plan with checks, notices, node edits or a first apply."),
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
) -> None:
    """plan → (confirm) → lock → plan again → apply → commit → unlock → tag."""
    settings = load_settings()
    try:
        nodes = set(settings.node_names())
        scope = Scope.build(nodes, options.nodes)
        repo = ConfigRepo.open(settings.lab_config)
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
                return
            targets = [work.node for work in planned.works]
        if not targets:
            console.print("Nothing to apply.")
            return
        conflicts = _conflicts(planned.runtime)
        if conflicts:
            _confirm([*([] if yes else planned.confirm), conflicts], conflicts=True)
        elif planned.confirm and not yes:
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
    except (ComposeError, EnvError, ApplyError, RemoteError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        Remote.close_all(nodes)


def _conflicts(runtime: Runtime) -> str:
    """What apply does about the conflicts, for the question; "" when there are none."""
    parts = []
    if runtime.take_down:
        n = len(runtime.take_down)
        parts.append(f"take down {n} compose project{'s' if n > 1 else ''} from elsewhere")
    if runtime.stops:
        n = len(runtime.stops)
        parts.append(f"stop {n} container{'s' if n > 1 else ''} labctl does not manage")
    if runtime.defers:
        n = sum(len(defer.services) for defer in runtime.defers)
        parts.append(f"leave {n} service{'s' if n > 1 else ''} created but not started")
    return ", ".join(parts)


def _confirm(reasons: list[str], conflicts: bool = False) -> None:
    """Ask in a terminal. Conflicts always need the answer there: --yes does not cover stopping
    what labctl does not manage."""
    if not sys.stdin.isatty():
        if conflicts:
            raise click.ClickException(
                "The plan has conflicts with containers or ports labctl does not manage (listed "
                "above); resolve them by hand, or run apply in a terminal to have it do it"
            )
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


def _files(collected: Collected) -> object:
    return (
        {node: d.files for node, d in collected.desired.items()},
        dict(collected.node_files),
        dict(collected.node_names),
        dict(collected.projects),
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
    """Archived data for the services added: found on the nodes read (only when one is
    stateful)."""
    states = {node: d.state for node, d in collected.desired.items()}
    adds = [op for op in plan.operations if isinstance(op, Add)]
    if not any(states[op.node].services[op.service].stateful for op in adds):
        return Restores((), (), (), (), ())
    roots = {
        node: d.dirs.appdata_dir
        for node, d in collected.desired.items()
        if collected.scope.in_scope(node)
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
        containers=collected.containers,
        listeners=collected.listeners,
        missing=collected.missing,
        work={work.node for work in works or () if not work.record_only},
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

    render_problems(plan.problems, placeholders)
    render_drift(collected)
    files_changed = render_files(collected, scope, options.verbose)
    render_stacks(plan, collected, runtime, work_nodes, probes, options.verbose)
    render_moves(plan, probes)
    render_held(plan, collected)
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

    reasons = []
    if plan.problems or placeholders or restores.errors:
        reasons.append("problems above")
    blocking = [finding for finding in findings if finding.level is Level.BLOCK]
    if blocking:
        reasons.append(f"{len(blocking)} check{'s' if len(blocking) > 1 else ''} failed")
    if runtime.blocked:
        reasons.append("conflicts apply cannot resolve")
    if computed.works is None:
        console.print(f"\n[red]✗[/] {escape(computed.work_error)}")
        reasons.append("not supported yet")
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
    if plan.held:
        confirm.append("held operations")
    if shared:
        confirm.append("remote storage newly shared between nodes")
    if collected.drift:
        confirm.append("node edits to discard")
    first = [work.node for work in computed.works or () if _source(collected, work.node) == "first"]
    if first:
        confirm.append(f"a first apply on {', '.join(first)}")
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
    )


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
