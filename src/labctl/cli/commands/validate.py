"""labctl validate [NODE...|all]: check the lab config without touching a node."""

from __future__ import annotations

import sys
from typing import Any

import click
from rich.markup import escape

from labctl.bundle import Desired
from labctl.cli.common import load_settings
from labctl.cli.render import render_checked
from labctl.click import DebugOptions
from labctl.console import console
from labctl.links import split_links
from labctl.repo import ConfigRepo, RepoError
from labctl.settings import Secrets, SettingsError
from labctl.state import ALL, Scope
from labctl.validate import check_lab


@click.command(name="validate")
@click.argument("nodes", nargs=-1, metavar="[NODE...|all]")
@DebugOptions().options
def validate_cmd(nodes: tuple[str, ...], **kwargs: Any) -> None:
    """Check the lab config without touching a node. Exits 1 when a node is not valid.

    No node named means 'all'. Each node's stacks are built and resolved as plan does (the working
    tree, uncommitted changes included), with every value they need, and no two of a node's
    services may publish the same host port. Never asks, writes nothing, and leaves _HOST_IP_
    unresolved.
    """
    settings = load_settings()
    try:
        scope = Scope.build(set(settings.node_names()), nodes or (ALL,))
        repo = ConfigRepo.open(settings.lab_config)
    except (SettingsError, ValueError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    secrets = Secrets.load(settings.secrets_file)

    invalid = 0
    found: dict[str, Desired] = {}
    try:
        for checked in check_lab(settings, repo, scope, secrets):
            render_checked(checked)
            if checked.valid and checked.desired is not None:
                found[checked.node] = checked.desired
            else:
                invalid += 1
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    # Across the nodes checked: a service linked to a container another node runs.
    for problem in split_links(found):
        console.print(f"[red]✗[/] {escape(problem)}")
        invalid += 1
    if invalid:
        sys.exit(1)
