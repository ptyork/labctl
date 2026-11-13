"""labctl plan and labctl apply: one code path. plan is apply --dry-run."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.common import load_settings
from labctl.cli.prompts import ask_restore, prompt_value
from labctl.cli.render import (
    choice_label,
    describe,
    render_applied,
    render_nodes,
    render_plan,
    render_port_moves,
)
from labctl.click import DebugOptions, PlanOptions
from labctl.collect import Collected, collect
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import EnvError
from labctl.git_sync import branch_notices, catch_up, default_message
from labctl.node import NodeError
from labctl.planning import Options, PlanError, Planned, answered, make_planned
from labctl.progress import Progress
from labctl.remote import Remote, RemoteError
from labctl.repo import ConfigRepo, RepoError
from labctl.restore import Ask
from labctl.rollout import roll_out
from labctl.runtime import STOP
from labctl.settings import Secrets, SettingsError
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
        ask_ff = _ask_fast_forward if sys.stdin.isatty() and not yes else None
        caught_up = catch_up(repo, options.ff, applying=not dry_run, ask=ask_ff)
        if caught_up.warning:
            console.print(f"[yellow]![/] {escape(caught_up.warning)}")
        if caught_up.forwarded:
            console.print(f"[green]✓[/] {caught_up.forwarded}")
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
        assert planned.tree is not None
        default = default_message(repo, planned.base, planned.tree, options.nodes)
        applied = roll_out(
            settings,
            scope,
            secrets,
            planned,
            targets,
            wait,
            repo,
            lambda: _commit_message(default, message, ask=not yes),
            console.print,
            Progress(console.print, console),
        )
        render_applied(applied, repo.root)
        render_port_moves(planned.port_moves, "Published ports that changed")
        return False
    except (ComposeError, EnvError, NodeError, PlanError, RemoteError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        Remote.close_all(nodes)


def _ask_fast_forward(question: str, commits: str) -> bool:
    """Whether to fast-forward the lab config before planning (Esc: no)."""
    return bool(menu.confirm(escape(question), default=True, header=escape(commits)))


def _commit_message(default: str, given: str | None, ask: bool) -> str:
    """
    The message for the commit of what was just applied.

    Args:
        default (str):
            The message offered.
        given (str | None):
            The message given with ``-m``.
        ask (bool):
            Whether it may be asked in a terminal (not with ``--yes``).

    Returns:
        str:
            ``-m``, else the answer in a terminal (Esc takes the default: it is applied either
            way), else the default.
    """
    if given:
        return given
    if not (ask and sys.stdin.isatty()):
        return default
    answer = menu.edit_text(
        "Enter commit message",
        default,
        validators=[lambda v: None if v.strip() else "A message is required"],
    )
    return (answer or default).strip()


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
