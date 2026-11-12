from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.commands.settings import save_setting
from labctl.click import DebugOptions
from labctl.console import console
from labctl.repo import ConfigRepo, RepoError
from labctl.settings import LAB_CONFIG_ENV, SETTINGS, check_value, config_file, read_config

REMOTE = "origin"

GITIGNORE = """\
# compiled env (holds secrets) and secrets files
.env
*.env.secrets
secrets.toml
# a node's DEPLOY_DIR bookkeeping, should one be copied here
.labctl/
# editors and OS
*.swp
*.swo
*~
.#*
.DS_Store
Thumbs.db
desktop.ini
*:Zone.Identifier
.vscode/
.idea/
"""

GITATTRIBUTES = "* text=auto eol=lf\n"


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
        _init(root, remote, github)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc


def _init(root: Path, remote: str | None, github: str | None) -> None:
    if root.exists() and not root.is_dir():
        raise click.ClickException(f"Not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    repo, created = ConfigRepo.init(root)
    _done(created, f"git repository {root} (branch main)")

    written: list[Path] = []
    for folder in (root / "nodes", root / "nodes" / "_shared"):
        if folder.exists() and not folder.is_dir():
            raise click.ClickException(f"Not a directory: {folder}")
        new = not folder.exists()
        if new:
            folder.mkdir()
        _done(new, f"{folder.relative_to(root).as_posix()}/")
    shared = root / "nodes" / "_shared"
    if not any(shared.iterdir()):
        (shared / ".gitkeep").touch()  # git keeps no empty folders
        written.append(shared / ".gitkeep")
    for name, text in ((".gitignore", GITIGNORE), (".gitattributes", GITATTRIBUTES)):
        path = root / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            written.append(path)
        _done(path in written, name)

    if not repo.has_commits():
        made = repo.commit("labctl init")
        count = len(repo.git.list_files())
        console.print(f"[green]✓[/] first commit {made[:7]} ({count} files)")
    elif written:
        made = repo.commit("labctl init", written)
        console.print(f"[green]✓[/] committed {made[:7]}: {', '.join(p.name for p in written)}")

    _make_it_the_lab_config(root)

    if github:
        _create_on_github(repo, root, github)
    elif remote:
        current = repo.remote_url(REMOTE)
        if current is None:
            repo.add_remote(REMOTE, remote)
            console.print(f"[green]✓[/] {REMOTE} = {escape(remote)}")
        elif current != remote:
            raise click.ClickException(f"{REMOTE} is already {current}")
    if remote or github:
        repo.push_branch(REMOTE)
        console.print(f"[green]✓[/] pushed {repo.branch()} to {REMOTE}")
        failed = repo.publish()
        if failed:
            raise click.ClickException(f"apply tags not pushed: {failed}")


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


def _create_on_github(repo: ConfigRepo, root: Path, name: str) -> None:
    if repo.remote_url(REMOTE) is not None:
        raise click.ClickException(f"{root} already has {REMOTE}; --github creates it")
    if shutil.which("gh") is None:
        raise click.ClickException(
            "gh (the GitHub CLI) is not installed; create the repository yourself and use --remote"
        )
    if run_gh("auth", "status", cwd=root, quiet=True) != 0:
        raise click.ClickException("gh is not logged in (gh auth login)")
    # gh adds origin; the push follows, with the tags.
    if run_gh("repo", "create", name, "--private", "--source", ".", "--remote", REMOTE, cwd=root):
        raise click.ClickException(f"gh repo create {name} failed")
    console.print(f"[green]✓[/] private GitHub repository {escape(name)}, as {REMOTE}")


def run_gh(*args: str, cwd: Path, quiet: bool = False) -> int:
    output = subprocess.DEVNULL if quiet else None
    return subprocess.run(["gh", *args], cwd=cwd, stdout=output, stderr=output).returncode
