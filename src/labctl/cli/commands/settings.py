from __future__ import annotations

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.common import edit_setting, save_setting
from labctl.console import console
from labctl.settings import (
    SETTINGS,
    Setting,
    check_value,
    config_file,
    read_config,
)


@click.group(name="settings")
def settings_cmd() -> None:
    """View and edit labctl's own settings (labctl.toml)."""


@settings_cmd.command(name="get")
@click.argument("name", required=False)
def settings_get(name: str | None) -> None:
    """Print a setting's value (the default when unset). Without NAME, print all as name=value."""
    values = read_config(config_file())
    if name is None:
        for setting in SETTINGS.values():
            click.echo(f"{setting.name}={values.get(setting.name, setting.default)}")
        return
    setting = _setting(name)
    click.echo(values.get(setting.name, setting.default))


@settings_cmd.command(name="set")
@click.argument("name")
@click.argument("value")
def settings_set(name: str, value: str) -> None:
    """Set a setting's value."""
    setting = _setting(name)
    error = check_value(setting, value)
    if error:
        raise click.ClickException(error)
    save_setting(setting, value)


@settings_cmd.command(name="edit")
def settings_edit() -> None:
    """Choose a setting and edit it. Esc leaves."""
    path = config_file()
    settings = list(SETTINGS.values())
    selected = 0
    while True:
        values = read_config(path)
        options = menu.rows(
            [
                (
                    setting.name,
                    escape(values[setting.name])
                    if setting.name in values
                    else f"{escape(setting.default)} (default)",
                )
                for setting in settings
            ]
        )
        index = menu.choose(f"Settings in {path}", options, selected)
        if index is None:
            return
        selected = index
        setting = settings[index]
        new = edit_setting(setting, values.get(setting.name, setting.default))
        if new is None or new == values.get(setting.name):
            continue
        problem = check_value(setting, new)
        if problem:
            console.print(f"[red]✗ {escape(problem)}; not saved[/]")
        else:
            save_setting(setting, new)


def _setting(name: str) -> Setting:
    found = SETTINGS.get(name.strip().lower().replace("-", "_"))
    if found is None:
        raise click.ClickException(f"Unknown setting '{name}'. Settings: {', '.join(SETTINGS)}")
    return found
