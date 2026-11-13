"""labctl unlock NODE...|all: remove the apply lock an interrupted run left on a node."""

from __future__ import annotations

import sys
from typing import Any

import click
from rich.markup import escape

from labctl.cli.common import confirm_or_yes, load_settings, target_nodes
from labctl.click import DebugOptions
from labctl.console import console
from labctl.node import HeldLock, read_lock, unlock
from labctl.remote import Remote, RemoteError, node_remote
from labctl.settings import SettingsError


@click.command(name="unlock")
@click.argument("targets", nargs=-1, required=True, metavar="NODE...|all")
@click.option("--yes", "-y", is_flag=True, help="Do not ask before removing the locks.")
@DebugOptions().options
def unlock_cmd(targets: tuple[str, ...], yes: bool, **kwargs: Any) -> None:
    """Remove the apply lock a run left on the nodes, once nothing runs there.

    Shows who took each lock and the steps its run recorded, then asks. A run interrupted part-way
    may have left the node half done: check it first (labctl plan NODE).
    """
    settings = load_settings()
    try:
        chosen = target_nodes(targets, settings.node_names())
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    if any(selectors != ("",) for selectors in chosen.values()):
        raise click.ClickException("A lock belongs to a node: give NODE, not NODE/STACK")

    remotes = {node: node_remote(node) for node in chosen}
    failed: list[str] = []
    try:
        held: dict[str, HeldLock] = {}
        for node, remote in remotes.items():
            try:
                found = read_lock(remote)
            except RemoteError as exc:
                console.print(f"[red]✗[/] {escape(str(exc))}")
                failed.append(node)
                continue
            if found is None:
                console.print(f"{escape(node)}: not locked")
                continue
            held[node] = found
            console.print(f"[yellow]![/] {escape(node)}: locked by {escape(found.owner)}")
            for step in found.steps or ("no steps recorded",):
                console.print(f"    {escape(step)}")
        if held:
            locks = "the lock" if len(held) == 1 else f"these {len(held)} locks"
            confirm_or_yes(f"Remove {locks}?", yes, "Not unlocked")
            for node in held:
                unlock(remotes[node])
                console.print(f"[green]✓[/] {escape(node)}: unlocked")
    finally:
        Remote.close_all(chosen)
    if failed:
        sys.exit(1)
