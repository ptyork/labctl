"""labctl ports [NODE...|all]: the host ports in use on the nodes, and those the lab wants."""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import click
from craftable import get_table
from rich.markup import escape
from rich.panel import Panel

from labctl.bundle import resolve_desired
from labctl.cli.commands.settings import load_settings
from labctl.click import DebugOptions
from labctl.collect import HOME_POINTER
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import EnvError, preview_env
from labctl.ports import NOT_IN_CONFIG, SERVICE, Declared, PortUse, declared_ports, port_map
from labctl.remote import MARK, Remote, RemoteError, node_remote, sections
from labctl.repo import ConfigRepo, RepoError
from labctl.runtime import READ_RUNTIME, is_any_address, parse_containers, parse_listeners
from labctl.settings import Secrets, Settings, SettingsError, node_names
from labctl.state import ALL, Scope

_READ = f"""\
echo "{MARK} pointer"; cat "$HOME/{HOME_POINTER}" 2>/dev/null
{READ_RUNTIME}
"""

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
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    secrets = Secrets.load(settings.secrets_file)

    uses: list[PortUse] = []
    unread = 0
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        work = Path(tmp)
        source = work / ".tree"
        try:
            repo.export_worktree(source)
            known = node_names(source / "nodes")
            scope = Scope.build(set(known), nodes or (ALL,))
        except (RepoError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        chosen = [node for node in known if scope.in_scope(node)]
        try:
            with ThreadPoolExecutor(max_workers=max(len(chosen), 1)) as pool:
                reads = {node: pool.submit(_read, node_remote(node)) for node in chosen}
                for node in chosen:
                    found = _node_ports(settings, node, secrets, work, source, reads[node])
                    if found is None:
                        unread += 1
                    else:
                        uses += found
        finally:
            Remote.close_all(chosen)
    if not include_host:
        uses = [use for use in uses if use.kind == SERVICE]
    if uses:
        _print_table(sorted(uses, key=_SORTS[sort]), several=len(chosen) > 1)
    if unread:
        sys.exit(1)


def _read(remote: Remote) -> str:
    return remote.run_script(_READ)


def _node_ports(
    settings: Settings,
    node: str,
    secrets: Secrets,
    work: Path,
    source: Path,
    read: Future[str],
) -> list[PortUse] | None:
    """One node's port uses (None, said, when the node cannot be read). A lab config that does not
    resolve for the node is said too; what runs there is still shown."""
    declared: Declared | None
    deploy_dir = ""
    try:
        desired = resolve_desired(settings, node, secrets, work, None, source)
        declared, deploy_dir = declared_ports(desired), desired.dirs.deploy_dir
    except (ComposeError, EnvError, SettingsError) as exc:
        first = str(exc).splitlines()[0]
        console.print(
            f"[yellow]![/] {escape(first)}: the lab config's ports are not shown for {node}"
        )
        declared = None
    try:
        parts = sections(read.result())
    except RemoteError as exc:
        console.print(f"[red]✗[/] {escape(str(exc))}")
        return None
    deploy_dir = (
        "".join(parts.get("pointer", [])).strip()
        or deploy_dir
        or preview_env(source / "nodes", node).get("DEPLOY_DIR", "")
    )
    listeners = parse_listeners(parts.get("listeners", []))
    if listeners is None:
        console.print(f"[yellow]![/] {node}: no ss there: ports held outside Docker are not shown")
    containers = parse_containers(parts.get("containers", []), deploy_dir or "/nonexistent")
    return port_map(node, declared, containers, listeners or ())


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
