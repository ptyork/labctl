"""labctl plan and labctl apply: one code path. plan is apply --dry-run."""

from __future__ import annotations

import getpass
import socket
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import partial
from typing import Any

import click

from labctl import __version__
from labctl.apply import ApplyError, Executor, RollbackFailed
from labctl.cli import menu
from labctl.cli.common import load_settings
from labctl.cli.git_sync import (
    branch_notices,
    catch_up,
    commit_applied,
    commit_message,
    publish_applied,
)
from labctl.cli.prompts import ask_restore, prompt_value
from labctl.cli.render import choice_label, describe, render_nodes, render_plan, render_port_moves
from labctl.click import DebugOptions, PlanOptions
from labctl.collect import Collected, collect
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import EnvError
from labctl.node import Node, NodeError, lock, unlock
from labctl.planning import Options, PlanError, Planned, answered, make_planned
from labctl.progress import Progress
from labctl.remote import Remote, RemoteError, node_remote
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.restore import Ask
from labctl.runtime import STOP
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
            targets = [work.node for work in planned.works or ()]
        if not targets:
            console.print("Nothing to apply.")
            return False
        planned, asked = _answer_conflicts(planned, sys.stdin.isatty())
        if (planned.confirm or asked) and not yes:
            _confirm(planned.confirm)
        assert planned.options is not None
        _apply(
            settings,
            scope,
            secrets,
            planned.options,
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
    except (ComposeError, EnvError, NodeError, PlanError, RemoteError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        Remote.close_all(nodes)


def _answer_conflicts(planned: Planned, terminal: bool) -> tuple[Planned, bool]:
    """
    Ask about each conflict the flags left unanswered (--yes answers none of them).

    Args:
        planned (Planned):
            The plan.
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
    stop: list[str] = []
    defer: list[str] = []
    console.print()
    for conflict in unresolved:
        choices = [*conflict.choices, ""]  # "": cancel
        labels = [choice_label(conflict, choice) for choice in choices]
        index = menu.choose(f"In the way on {conflict.node}", labels, header=describe(conflict))
        if index is None or not choices[index]:
            raise click.ClickException("Not applied")
        (stop if choices[index] == STOP else defer).append(conflict.key)
    return answered(planned, stop, defer), True


def _confirm(reasons: Sequence[str]) -> None:
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
            computed = make_planned(again, options)
            if computed.works is None:
                raise ApplyError(computed.work_error)
            if not computed.same_as(planned):
                raise ApplyError("The homelab changed since it was planned; run apply again")
            tree, base = planned.tree, planned.base
            executor = Executor(
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
            except NodeError as exc:  # already shown, with the rollback
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
                    __version__,
                    tuple(sorted(up)),
                    work.desired.dirs,
                    work.desired.env.host_ip,
                    retired=again.nodes[work.node].retiring,
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


def _plan_and_render(
    collected: Collected,
    options: Options,
    notices: Sequence[str] = (),
    ask: Ask | None = None,
) -> Planned:
    """Plan what was collected (asking which archived copies to restore), and show it."""
    render_nodes(collected)
    planned = make_planned(collected, options, notices, ask)
    render_plan(collected, planned, options.verbose)
    return planned
