"""labctl validate [NODE...|all]: check the lab config without touching a node."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from labctl.bundle import resolve_desired
from labctl.cli.commands.settings import load_settings
from labctl.cli.render import not_a_stack
from labctl.click import DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import EnvError
from labctl.repo import ConfigRepo, RepoError
from labctl.settings import Secrets, SettingsError, node_names
from labctl.state import ALL, Scope


@click.command(name="validate")
@click.argument("nodes", nargs=-1, metavar="[NODE...|all]")
@DebugOptions().options
def validate_cmd(nodes: tuple[str, ...], **kwargs: Any) -> None:
    """Check the lab config without touching a node. Exits 1 when a node is not valid.

    No node named means 'all'. Each node's stacks are built and resolved as plan does (the working
    tree, uncommitted changes included), with every value they need. Never asks, writes nothing,
    and leaves _HOST_IP_ unresolved.
    """
    settings = load_settings()
    try:
        scope = Scope.build(set(settings.node_names()), nodes or (ALL,))
        repo = ConfigRepo.open(settings.lab_config)
    except (SettingsError, ValueError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    secrets = Secrets.load(settings.secrets_file)

    invalid = 0
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        work = Path(tmp)
        source = work / ".tree"
        try:
            repo.export_worktree(source)
        except RepoError as exc:
            raise click.ClickException(str(exc)) from exc
        for node in node_names(source / "nodes"):
            if not scope.in_scope(node):
                continue
            try:
                desired = resolve_desired(settings, node, secrets, work, None, source, offline=True)
            except (ComposeError, EnvError) as exc:
                console.print(f"[red]✗[/] {escape(str(exc))}")
                invalid += 1
                continue
            for file, why in sorted(desired.not_stacks.items()):
                console.print(f"[yellow]![/] {not_a_stack(node, file, why)}")
            missing = desired.env.placeholders
            if missing:
                console.print(f"[red]✗[/] {node}: no value for {', '.join(missing)}")
                invalid += 1
                continue
            stacks, services = len(desired.configs), len(desired.state.services)
            console.print(
                f"[green]✓[/] {node}: {stacks} stack{'' if stacks == 1 else 's'}, "
                f"{services} service{'' if services == 1 else 's'}"
            )
    if invalid:
        sys.exit(1)
