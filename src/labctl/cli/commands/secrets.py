from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.console import console
from labctl.envfile import VAR_NAME
from labctl.settings import Secrets, Settings, SettingsError, secrets_file


@click.group(name="secrets")
def secrets_cmd() -> None:
    """View and edit secrets (secrets.toml): [global] values and per-node overrides."""


@secrets_cmd.command(name="get")
@click.argument("key")
@click.option("--node", "-n", help="The value this node gets (its override, else global).")
def secrets_get(key: str, node: str | None) -> None:
    """Print a secret's value (global unless --node). Exits 1 when it is not set."""
    secrets = Secrets.load(secrets_file())
    value = secrets.get(node, key) if node else secrets.global_.get(key)
    if value is None:
        click.echo(f"{key} is not set{f' for {node}' if node else ' globally'}", err=True)
        sys.exit(1)
    click.echo(value)


@secrets_cmd.command(name="set")
@click.argument("key")
@click.argument("value", required=False)
@click.option("--node", "-n", help="Set the override for this node instead of the global value.")
def secrets_set(key: str, value: str | None, node: str | None) -> None:
    """Set a secret. Without VALUE, asks for it, keeping it out of shell history."""
    error = _check_name(key)
    if error:
        raise click.ClickException(error)
    if value is None:
        value = menu.edit_text(f"[bold cyan]{key}[/] ({_scope(node)})", key, validators=[_required])
        if value is None:
            raise click.ClickException("Cancelled")
    path = secrets_file()
    secrets = Secrets.load(path)
    secrets.table(node)[key] = value
    secrets.save(path)
    console.print(f"[green]✓[/] {key} set ({_scope(node)}) [dim]({path})[/]")


@secrets_cmd.command(name="edit")
def secrets_edit() -> None:
    """Choose a secret and edit it, or add one. Esc leaves."""
    path = secrets_file()
    selected = 0
    while True:
        secrets = Secrets.load(path)
        entries = secrets.entries()
        width = max((len(_scope(n)) for n, _, _ in entries), default=0)
        options = [
            "[green]+ add a secret[/]",
            *menu.rows([(f"{_scope(n).ljust(width)}  {k}", escape(v)) for n, k, v in entries]),
        ]
        index = menu.choose(
            "[bold cyan]labctl secrets[/]",
            f"{path}. Enter to edit, Esc to leave.",
            options,
            selected,
        )
        if index is None:
            return
        selected = index
        if index == 0:
            _add(secrets, path)
            continue
        node, key, value = entries[index - 1]
        new = menu.edit_text(
            f"[bold cyan]{escape(key)}[/] ({_scope(node)})",
            key,
            value,
            header="Enter to accept (clear the value to remove the secret), Esc to cancel.",
        )
        if new is None or new == value:
            continue
        table = secrets.table(node)
        if new:
            table[key] = new
            console.print(f"[green]✓[/] {key} updated ({_scope(node)})")
        else:
            del table[key]
            console.print(f"[yellow]-[/] {key} removed ({_scope(node)})")
        secrets.save(path)


def _add(secrets: Secrets, path: Path) -> None:
    key = menu.edit_text("[bold cyan]New secret[/]", "Name", validators=[_required, _check_name])
    if key is None:
        return
    scopes: list[str | None] = [None, *_node_names()]
    index = menu.choose(
        f"[bold cyan]{escape(key)}[/]: where?",
        "Global applies to every node; a node value overrides it there. Esc to cancel.",
        [_scope(scope) for scope in scopes],
    )
    if index is None:
        return
    node = scopes[index]
    value = menu.edit_text(
        f"[bold cyan]{escape(key)}[/] ({_scope(node)})",
        key,
        secrets.table(node).get(key, ""),
        validators=[_required],
    )
    if value is None:
        return
    secrets.table(node)[key] = value
    secrets.save(path)
    console.print(f"[green]✓[/] {key} set ({_scope(node)})")


def _node_names() -> list[str]:
    try:
        return Settings.load().node_names()
    except SettingsError:
        return []


def _scope(node: str | None) -> str:
    return "global" if node is None else f"node {node}"


def _required(value: str) -> str | None:
    return None if value else "A value is required (Esc to cancel)"


def _check_name(value: str) -> str | None:
    if VAR_NAME.fullmatch(value):
        return None
    return "Names are letters, digits and _ (not starting with a digit)"
