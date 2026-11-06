from __future__ import annotations

import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import click
from craftable import get_table
from rich.markup import escape
from rich.panel import Panel

from labctl.click import DebugOptions
from labctl.console import console
from labctl.envfile import HOST_IP, preview_env
from labctl.hostip import local_ip, ssh_address
from labctl.remote import node_remote
from labctl.settings import Settings, SettingsError, config_file

_MODULE_NAME = "labctl"
_NOT_SET = "[yellow]not set[/]"
# Node variables shown (from _shared/*.env + node.env only).
_NODE_VARS = ["DEPLOY_DIR", "APPDATA_DIR"]


@click.command(name="status")
@DebugOptions().options
def status_cmd(**kwargs: Any) -> None:
    """Show labctl's environment and each node's key settings."""
    settings: Settings | None
    try:
        settings = Settings.load()
        lab_config = str(settings.lab_config)
    except SettingsError as exc:
        settings = None
        lab_config = f"[red]{escape(str(exc))}[/]"

    rows = [
        ["CLI Version", _package_version()],
        ["Executable", _executable_path()],
        ["Python", sys.executable],
        ["Config File", str(config_file())],
        ["Lab Config", lab_config],
    ]
    width = max(console.width - 4, 50)
    table = get_table(rows, header_row=["Setting", "Value"], col_defs=["", "A"], table_width=width)
    console.print(
        Panel(
            table,
            title="labctl status",
            border_style="cyan",
        )
    )
    if settings is None:
        return

    try:
        nodes = settings.node_names()
    except SettingsError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        return
    node_rows = []
    notes = []
    for node in nodes:
        values = preview_env(settings.nodes_dir, node)
        if node_remote(node).local:
            host, ip = "local", local_ip()
        else:
            host, ip = ssh_address(node)
        node_rows.append(
            [
                node,
                escape(host),
                ip or "[red]does not resolve[/]",
                *(escape(values[var]) if values.get(var) else _NOT_SET for var in _NODE_VARS),
            ]
        )
        server_ip = values.get("SERVER_IP")
        if ip and server_ip and server_ip not in (ip, HOST_IP):
            notes.append(
                f"[yellow]![/] {node}: SERVER_IP in node.env is {escape(server_ip)}, but "
                f"{escape(host)} resolves to {ip}"
            )
    console.print(
        Panel(
            get_table(
                node_rows,
                header_row=["Node", "SSH Host", "IP Address", *_NODE_VARS],
                # Name, host and IP sized to their data; the path columns share the rest.
                col_defs=["", "", "", *["A"] * len(_NODE_VARS)],
                table_width=width,
            ),
            title="Nodes (paths from node.env; missing values are asked for by plan)",
            border_style="green",
        )
    )
    for note in notes:
        console.print(note)


def _package_version() -> str:
    try:
        return version(_MODULE_NAME)
    except PackageNotFoundError:
        return "undetermined"


def _executable_path() -> str:
    cli_path = shutil.which("labctl")
    return str(Path(cli_path or sys.argv[0]).resolve())
