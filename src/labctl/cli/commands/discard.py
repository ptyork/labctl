"""labctl discard: undo the lab config's changes no apply has committed."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.commands.settings import load_settings
from labctl.click import DebugOptions
from labctl.console import console
from labctl.repo import ConfigRepo, RepoError


@click.command(name="discard")
@click.option("--dry-run", is_flag=True, help="Only show what would be discarded.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask before discarding.")
@DebugOptions().options
def discard_cmd(dry_run: bool, yes: bool, **kwargs: Any) -> None:
    """Undo every change to the lab config no apply has committed: back to its last commit.

    The changes, new files included, are kept in a git stash named "labctl discard <timestamp>"
    (`git stash pop` in the lab config brings them back). Files git ignores stay as they are, and
    so does secrets.toml, which is not part of the lab config.
    """
    settings = load_settings()
    try:
        repo = ConfigRepo.open(settings.lab_config)
        changes = repo.changes()
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    if not changes:
        console.print("Nothing to discard")
        return
    console.print("[bold]Not applied[/]")
    for change in changes:
        if change.is_untracked:
            what = "new"
        elif "D" in (change.index, change.worktree):
            what = "deleted"
        else:
            what = "changed"
        console.print(f"  {what:<8} {escape(change.path)}", highlight=False)
    if dry_run:
        return
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("Confirm in a terminal, or pass --yes")
        console.print()
        if not menu.confirm("Discard these changes?"):
            raise click.ClickException("Not discarded")
    name = f"labctl discard {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        repo.stash(name)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    n = len(changes)
    console.print(f"[green]✓[/] discarded {n} change{'' if n == 1 else 's'} (git stash: {name})")
