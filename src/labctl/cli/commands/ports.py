"""labctl ports [NODE...|all]: the host ports in use on the nodes, and those the lab wants."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import click
from craftable import get_table
from rich.markup import escape
from rich.panel import Panel

from labctl.cli.common import load_settings
from labctl.click import DebugOptions
from labctl.console import console
from labctl.port_survey import survey_ports
from labctl.ports import NOT_IN_CONFIG, SERVICE, PortUse
from labctl.repo import ConfigRepo, RepoError
from labctl.runtime import is_any_address
from labctl.settings import Secrets

_SORTS: dict[str, Callable[[PortUse], tuple[Any, ...]]] = {
    "node": lambda use: (use.node, use.port.port, use.port.protocol, use.port.host_ip),
    "port": lambda use: (use.port.port, use.port.protocol, use.node, use.port.host_ip),
    "name": lambda use: (use.name.lower(), use.node, use.port.port, use.port.protocol),
}


@click.command(name="ports")
@click.argument("nodes", nargs=-1, metavar="[NODE...|all]")
@click.option(
    "--sort",
    type=click.Choice(sorted(_SORTS)),
    default="node",
    show_default=True,
    help="Order the rows by node, port number, or what uses the port.",
)
@click.option(
    "--include-host",
    "-h",
    is_flag=True,
    help="Also show the ports of containers labctl does not manage and of host processes.",
)
@DebugOptions().options
def ports_cmd(nodes: tuple[str, ...], sort: str, include_host: bool, **kwargs: Any) -> None:
    """Show the host ports in use on the nodes, those the lab config wants, and clashes.

    No node named means 'all'. Shows the lab config's ports; with --include-host also those of
    containers labctl does not manage and of processes listening on the host. A clash is a port
    the lab config wants that another of its services also wants, or that something else holds
    now. Services on the host network publish no ports. Read-only; exits 1 when a node cannot be
    read.
    """
    settings = load_settings()
    try:
        repo = ConfigRepo.open(settings.lab_config)
        survey = survey_ports(settings, repo, Secrets.load(settings.secrets_file), nodes)
    except (RepoError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for warning in survey.warnings:
        console.print(f"[yellow]![/] {escape(warning)}")
    for problem in survey.unread:
        console.print(f"[red]✗[/] {escape(problem)}")
    uses = survey.uses
    if not include_host:
        uses = [use for use in uses if use.kind == SERVICE]
    if uses:
        _print_table(sorted(uses, key=_SORTS[sort]), several=len(survey.nodes) > 1)
    if survey.unread:
        sys.exit(1)


def _print_table(uses: list[PortUse], several: bool) -> None:
    header = ["Node", "Port", "Address", "Used By", "Kind", "State"]
    rows = [
        [
            use.node,
            f"{use.port.port}/{use.port.protocol}",
            "all" if is_any_address(use.port.host_ip) else use.port.host_ip,
            use.name or "(unknown)",
            use.kind,
            f"clashes with {', '.join(use.clashes)}" if use.clashes else use.state,
        ]
        for use in uses
    ]
    colors = [_plain] * (len(header) - 1) + [_state_colored]
    if not several:  # one node: its name is in the title
        rows, header, colors = [row[1:] for row in rows], header[1:], colors[1:]

    # Columns sized to their data; markup goes on after sizing, so it never counts toward widths.
    body = get_table(rows, header_row=header, postprocessors=colors)
    title = "Ports" if several else f"Ports on {uses[0].node}"
    console.print(Panel(body, title=title, border_style="cyan", expand=False))


def _plain(value: Any, text: str, row: list[Any], col_idx: int) -> str:
    return escape(text)


def _state_colored(value: Any, text: str, row: list[Any], col_idx: int) -> str:
    """A clash in red, a container the lab config dropped in yellow."""
    state = str(value)
    color = "red" if state.startswith("clashes") else "yellow" if state == NOT_IN_CONFIG else ""
    return f"[{color}]{escape(text)}[/]" if color else escape(text)
