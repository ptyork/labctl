"""labctl remove NODE: take a node out of the lab config (delete nodes/NODE)."""

from __future__ import annotations

from typing import Any

import click
from rich.markup import escape

from labctl.cli.common import confirm_or_yes, load_settings
from labctl.click import DebugOptions
from labctl.console import console
from labctl.repo import ConfigRepo, RepoError, check_removable, delete_node_folder
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
        repo = ConfigRepo.open(settings.lab_config)
        node = check_removable(repo, settings.nodes_dir, settings.node_names(), name, forget)
    except (SettingsError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    if node.last is not None and node.last.info is not None:
        about = (
            f"labctl stops tracking {escape(name)}. Nothing on it changes: what labctl put there "
            "stays, unmanaged."
        )
        confirm_or_yes(f"Forget {name}?", yes, "Not forgotten", about)
        try:
            tag, failed = repo.forget(name, node.last.info.dirs)
        except RepoError as exc:
            raise click.ClickException(str(exc)) from exc
        pushed = "" if failed == "no remote" else (" (not pushed)" if failed else ", pushed")
        console.print(f"[green]✓[/] forgot {escape(name)}: tagged {escape(tag)}{pushed}")
    if node.folder:
        delete_node_folder(settings.nodes_dir, name)
        console.print(f"[green]✓[/] removed {escape(node.path)}")
