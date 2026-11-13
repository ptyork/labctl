from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.common import save_setting
from labctl.click import DebugOptions
from labctl.console import console
from labctl.repo import ORIGIN, RepoError, connect_origin, create_on_github, init_lab_config
from labctl.settings import LAB_CONFIG_ENV, SETTINGS, check_value, config_file, read_config


@click.command(name="init")
@click.argument(
    "directory", type=click.Path(file_okay=False, path_type=Path), default=".", required=False
)
@click.option("--remote", metavar="URL", help="Add URL as origin and push to it.")
@click.option(
    "--github",
    metavar="[OWNER/]NAME",
    help="Create a private GitHub repository with gh, as origin, and push to it.",
)
@DebugOptions().options
def init_cmd(directory: Path, remote: str | None, github: str | None, **kwargs: Any) -> None:
    """Set up a lab config in DIRECTORY (default: here), or add what one is missing.

    Creates the folder, nodes/ and nodes/_shared/, a git repository (branch main) with a
    .gitignore and .gitattributes, and the first commit; then makes it labctl's lab_config. Files
    already there are kept. With --remote or --github, pushes the branch and the apply tags.
    """
    if remote and github:
        raise click.UsageError("--remote and --github are alternatives")
    root = directory.expanduser().resolve()
    try:
        done = init_lab_config(root)
        for made, what in done.steps:
            _done(made, what)
        if done.first:
            console.print(f"[green]✓[/] first commit {done.commit[:7]} ({len(done.files)} files)")
        elif done.commit:
            console.print(f"[green]✓[/] committed {done.commit[:7]}: {', '.join(done.files)}")
        _make_it_the_lab_config(root)
        repo = done.repo
        if github:
            create_on_github(repo, github)
            console.print(f"[green]✓[/] private GitHub repository {escape(github)}, as {ORIGIN}")
        elif remote and connect_origin(repo, remote):
            console.print(f"[green]✓[/] {ORIGIN} = {escape(remote)}")
        if remote or github:
            repo.push_branch(ORIGIN)
            console.print(f"[green]✓[/] pushed {repo.branch()} to {ORIGIN}")
            failed = repo.publish()
            if failed:
                raise click.ClickException(f"apply tags not pushed: {failed}")
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc


def _done(made: bool, what: str) -> None:
    if made:
        console.print(f"[green]✓[/] {escape(what)}")
    else:
        console.print(f"{escape(what)}: already there")


def _make_it_the_lab_config(root: Path) -> None:
    setting = SETTINGS["lab_config"]
    problem = check_value(setting, str(root))
    if problem:  # e.g. nodes/ is a file
        raise click.ClickException(problem)
    current = read_config(config_file()).get(setting.name, "").strip()
    if current and Path(current).expanduser().resolve() == root:
        console.print(f"{setting.name}: already {escape(current)}")
    elif (
        not current
        or sys.stdin.isatty()
        and menu.confirm(f"{setting.name} is {current}. Make it {root}?")
    ):
        save_setting(setting, str(root))
    else:
        console.print(
            f"[yellow]![/] {setting.name} stays {escape(current)} "
            f"('labctl settings set {setting.name} {escape(str(root))}' changes it)"
        )
    override = os.environ.get(LAB_CONFIG_ENV, "").strip()
    if override and Path(override).expanduser().resolve() != root:
        console.print(f"[yellow]![/] {LAB_CONFIG_ENV} is set, to {escape(override)}: it wins")
