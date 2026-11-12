from __future__ import annotations

import sys

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.console import console
from labctl.settings import (
    LAB_CONFIG_ENV,
    SETTINGS,
    LabConfigUnset,
    Setting,
    Settings,
    SettingsError,
    check_value,
    config_file,
    read_config,
    write_config,
)


def load_settings() -> Settings:
    """Settings for a command. With no usable lab config, a terminal is asked to choose one (and
    it is saved to labctl.toml); anywhere else that is an error."""
    try:
        return Settings.load()
    except LabConfigUnset as exc:
        if not sys.stdin.isatty():
            raise click.ClickException(
                f"{exc}. Set {LAB_CONFIG_ENV}, run 'labctl settings set lab_config DIR', "
                "or set one up with 'labctl init DIR'"
            ) from exc
        console.print(f"[yellow]{escape(str(exc))}[/]")
        setting = SETTINGS["lab_config"]
        chosen = _edit_value(setting, read_config(config_file()).get(setting.name, ""))
        if chosen is None:
            raise click.ClickException("No lab config chosen") from exc
        problem = check_value(setting, chosen)
        if problem:
            raise click.ClickException(f"{problem} ('labctl init DIR' sets one up)") from exc
        save_setting(setting, chosen)
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        return Settings.load()
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc


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
        new = _edit_value(setting, values.get(setting.name, setting.default))
        if new is None or new == values.get(setting.name):
            continue
        problem = check_value(setting, new)
        if problem:
            console.print(f"[red]✗ {escape(problem)}; not saved[/]")
        else:
            save_setting(setting, new)


def _edit_value(setting: Setting, current: str) -> str | None:
    if setting.is_dir:
        chosen = menu.choose_dir(f"{setting.name}: {escape(setting.description)}", current)
        return None if chosen is None else str(chosen)
    return menu.edit_text(
        f"{setting.name} ({escape(setting.description)})",
        current,
        validators=[lambda v: check_value(setting, v)],
    )


def save_setting(setting: Setting, value: str) -> None:
    path = config_file()
    values = read_config(path)
    values[setting.name] = value
    write_config(path, values)
    console.print(f"[green]✓[/] {setting.name} = {escape(value)}, saved to {path}")


def _setting(name: str) -> Setting:
    found = SETTINGS.get(name.strip().lower().replace("-", "_"))
    if found is None:
        raise click.ClickException(f"Unknown setting '{name}'. Settings: {', '.join(SETTINGS)}")
    return found
