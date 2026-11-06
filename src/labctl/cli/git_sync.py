"""The lab config's git side of plan and apply: catching up with its upstream before
planning, the commit made once applied, and pushing it."""

from __future__ import annotations

import sys
from collections.abc import Callable

import click
from git_wrap import StatusEntry
from rich.markup import escape

from labctl.cli import menu
from labctl.click import Options
from labctl.console import console
from labctl.repo import ConfigRepo, RepoError, TagInfo


def commit_message(
    repo: ConfigRepo,
    base: str,
    tree: str | None,
    nodes: tuple[str, ...],
    given: str | None,
    ask: bool,
) -> str:
    """
    The message for the commit of what was just applied.

    Args:
        repo (ConfigRepo):
            The lab config.
        base (str):
            The commit the apply was planned on.
        tree (str | None):
            The tree applied (never None once applied).
        nodes (tuple[str, ...]):
            The nodes named, for the default message.
        given (str | None):
            The message given with ``-m``.
        ask (bool):
            Whether it may be asked in a terminal (not with ``--yes``).

    Returns:
        str:
            ``-m``, else the answer in a terminal (a default offered; Esc takes it: it is applied
            either way), else the default.
    """
    assert tree is not None
    paths = repo.changed_paths(base, tree)
    listed = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
    default = f"labctl apply {' '.join(nodes)}: {listed}"
    if given:
        return given
    if not (ask and sys.stdin.isatty()):
        return default
    answer = menu.edit_text(
        "[bold cyan]Applied.[/] Commit it",
        "message",
        default,
        header=f"{len(paths)} changed: {escape(listed)}. Enter commits; Esc takes the default.",
        validators=[lambda v: None if v.strip() else "A message is required"],
    )
    return (answer or default).strip()


def list_changes(repo: ConfigRepo, changes: list[StatusEntry], limit: int = 20) -> str:
    lines = [f"{repo.root}: {len(changes)} uncommitted change{'s' if len(changes) > 1 else ''}"]
    for entry in changes[:limit]:
        code = "??" if entry.is_untracked else f"{entry.index}{entry.worktree}".replace(".", " ")
        lines.append(f"  {code} {entry.path}")
    if len(changes) > limit:
        lines.append(f"  … and {len(changes) - limit} more")
    return "\n".join(lines)


def catch_up(repo: ConfigRepo, options: Options, ask: bool, applying: bool) -> None:
    """The branch behind its upstream: fast-forward it before planning (the plan is of what the
    repo holds): with --ff, or when chosen in a terminal. apply goes on without it only when
    chosen there (--yes does not fast-forward, nor go on behind)."""
    state = repo.ahead_behind()
    if not state or not state[2]:
        return
    upstream, ahead, behind = state
    branch, root = repo.branch() or "HEAD", repo.root
    count = f"{behind} commit{'s' if behind > 1 else ''}"
    if ahead:
        message = (
            f"{branch} has diverged from {upstream} ({ahead} ahead, {behind} behind): no "
            f"fast-forward. Rebase or merge it:\n  git -C {root} pull --rebase\n"
            f"  git -C {root} merge {upstream}"
        )
        if applying and not ask:
            raise click.ClickException(message)
        console.print(f"[yellow]![/] {escape(message)}")
        return
    forward = options.ff
    if not forward and ask:
        subjects = repo.subjects(f"HEAD..{upstream}")
        shown = "; ".join(subjects[:3]) + (
            f"; and {len(subjects) - 3} more" if len(subjects) > 3 else ""
        )
        index = menu.choose(
            f"[bold cyan]{escape(branch)}[/] is {count} behind {escape(upstream)}",
            escape(shown),
            ["Fast-forward first", "Plan as it is"],
        )
        forward = index == 0
    if forward:
        problem = repo.fast_forward(upstream)
        if problem:
            raise click.ClickException(
                f"Cannot fast-forward {branch} to {upstream}:\n{problem}\nSet your changes aside "
                f"first:\n  git -C {root} stash\n  git -C {root} merge --ff-only {upstream}\n"
                f"  git -C {root} stash pop   # resolve conflicts, if any; then run labctl again"
            )
        console.print(f"[green]✓[/] fast-forwarded {branch} to {upstream} ({count})")
        return
    if applying and not ask:
        raise click.ClickException(
            f"{branch} is {count} behind {upstream}: applying it would undo what was applied "
            f"from there. Pass --ff to fast-forward first, or: git -C {root} pull --ff-only"
        )


def branch_notices(repo: ConfigRepo, fetched: str | None) -> list[str]:
    """What apply asks about first: a failed fetch (with a remote), a branch behind its upstream
    (chosen in a terminal: plan as it is)."""
    notices = []
    if fetched and fetched != "no remote":
        notices.append(
            f"not fetched ({fetched}): nodes not read are planned from this clone's tags"
        )
    state = repo.ahead_behind()
    if state and state[2]:
        upstream, _, behind = state
        notices.append(
            f"{repo.branch()} is {behind} commit{'s' if behind > 1 else ''} behind {upstream}: "
            "this plan undoes what was applied from there (--ff fast-forwards first)"
        )
    return notices


def commit_applied(repo: ConfigRepo, tree: str, base: str, message: Callable[[], str]) -> str:
    """What was just applied, as a commit on ``base`` (``base`` itself when nothing changed)."""
    if repo.tree_of(base) == tree:
        return base
    text = message()
    made = repo.commit_tree(tree, base, text)
    console.print(f"[green]✓[/] committed {made[:7]} {escape(text)}")
    return made


def publish_applied(
    repo: ConfigRepo, commit: str, stamp: str, tags: list[tuple[str, TagInfo]], branch: bool
) -> None:
    """Tag each node recorded, then push the tags with the branch (when it holds the commit).
    Failures are warnings: it is applied."""
    for node, info in tags:
        try:
            name = repo.tag_applied(node, stamp, commit, info)
        except RepoError as exc:
            console.print(f"[yellow]! {node}: applied, not tagged: {escape(str(exc))}[/]")
            continue
        console.print(f"[green]✓[/] tagged {name}")
    failed = repo.publish(branch)
    root = repo.root
    if failed == "no remote":
        console.print("[yellow]! No remote: the commit and its tags stay in this clone[/]")
    elif failed:
        console.print(
            f"[yellow]! Not pushed ({escape(failed)}). The next apply pushes; or catch up and "
            f"push now:\n  git -C {root} pull --rebase\n  git -C {root} push --follow-tags[/]"
        )
    else:
        console.print(f"[green]✓[/] pushed to {repo.remote()}")
