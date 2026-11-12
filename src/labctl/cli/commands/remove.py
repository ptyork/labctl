"""labctl remove NODE: take a node out of the lab config (delete nodes/NODE)."""

from __future__ import annotations

import getpass
import shutil
import socket
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.commands.settings import load_settings
from labctl.click import DebugOptions
from labctl.console import console
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.settings import SettingsError


@click.command(name="remove")
@click.argument("name")
@click.option(
    "--forget",
    is_flag=True,
    help="Stop tracking a node that is gone for good, without touching it.",
)
@click.option("--yes", "-y", is_flag=True, help="Do not ask before forgetting.")
@DebugOptions().options
def remove_cmd(name: str, forget: bool, yes: bool, **kwargs: Any) -> None:
    """Take a node out of the lab config: delete nodes/NAME, and nothing else.

    Nothing is committed and nothing changes on the node yet. The next apply of NAME retires it:
    everything labctl put there is removed (data archived, never deleted) and the deletion is
    committed. Until then, `labctl discard` undoes it; after, `labctl add NAME` brings nodes/NAME
    back from the lab config's history.

    --forget is for a node that cannot be retired because it is gone (dead, reinstalled, never
    reachable): labctl records it as retired without contacting it, and stops looking for it.
    Whatever labctl put there stays as it is.
    """
    settings = load_settings()
    try:
        defined = settings.node_names()
        repo = ConfigRepo.open(settings.lab_config)
        path = f"{settings.nodes_dir.relative_to(repo.root).as_posix()}/{name}"
        pending = [
            change.path
            for change in repo.changes()
            if change.path == path or change.path.startswith(path + "/")
        ]
        tracked = name in defined or name in repo.retiring(defined)
    except (SettingsError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    if name not in defined and not (forget and tracked):
        raise click.ClickException(f"The lab config has no node '{name}'")
    if pending:
        raise click.ClickException(
            f"{path} has changes no apply has committed ({', '.join(pending)}); apply them, or "
            "`labctl discard` them, first"
        )
    if forget:
        _forget(repo, name, yes)
    if name in defined:
        shutil.rmtree(settings.nodes_dir / name)
        console.print(f"[green]✓[/] removed {escape(path)}")


def _forget(repo: ConfigRepo, name: str, yes: bool) -> None:
    """Tag the node retired on HEAD (its last dirs kept), and push the tag."""
    last = repo.last_applied(name)
    if last is None or last.info is None:
        return  # never applied: there is nothing to forget, only the folder
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("Confirm in a terminal, or pass --yes")
        console.print(
            f"labctl stops tracking {escape(name)}. Nothing on it changes: what labctl put there "
            "stays, unmanaged."
        )
        if not menu.confirm(f"Forget {name}?"):
            raise click.ClickException("Not forgotten")
    now = datetime.now()
    info = TagInfo(
        now.isoformat(timespec="seconds"),
        f"{getpass.getuser()}@{socket.gethostname()}",
        _version(),
        (),
        last.info.dirs,
        retired=True,
    )
    try:
        tag = repo.tag_applied(name, now.strftime("%Y%m%d-%H%M%S"), repo.head(), info)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    failed = repo.publish()
    pushed = "" if failed == "no remote" else (" (not pushed)" if failed else ", pushed")
    console.print(f"[green]✓[/] forgot {escape(name)}: tagged {escape(tag)}{pushed}")


def _version() -> str:
    try:
        return version("labctl")
    except PackageNotFoundError:
        return "undetermined"
