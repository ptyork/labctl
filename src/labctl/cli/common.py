"""What several commands share: finding the settings, naming nodes, asking before a change."""

from __future__ import annotations

import sys
from collections.abc import Sequence

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
from labctl.state import ALL

#############################################################################

# The terminal

#############################################################################


def is_terminal() -> bool:
    """Whether a person is at the keyboard and reads the output: questions may be asked."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm_or_yes(question: str, yes: bool, refused: str, about: str = "") -> None:
    """
    Ask before a change, unless ``--yes`` was given.

    Args:
        question (str):
            The question (default answer: No).
        yes (bool):
            ``--yes``: nothing is asked.
        refused (str):
            The error when the answer is No ("Not deleted").
        about (str):
            Said before the question (default: a blank line).

    Raises:
        click.ClickException:
            The answer was No, or there is no terminal to ask in.
    """
    if yes:
        return
    if not sys.stdin.isatty():
        raise click.ClickException("Confirm in a terminal, or pass --yes")
    console.print(about)
    if not menu.confirm(question):
        raise click.ClickException(refused)


#############################################################################

# Settings

#############################################################################


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
        chosen = edit_setting(setting, read_config(config_file()).get(setting.name, ""))
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


def edit_setting(setting: Setting, current: str) -> str | None:
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


#############################################################################

# Naming nodes and services

#############################################################################

services_option = click.option(
    "--service",
    "-s",
    "services",
    multiple=True,
    metavar="SERVICE",
    help="Only this service (repeatable).",
)


def target_nodes(targets: Sequence[str], names: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Node -> the stacks named on it ("" for all of them), in the order given."""
    if ALL in names:
        raise SettingsError(f"a node may not be called '{ALL}'")
    if ALL in targets:
        if len(targets) > 1:
            raise SettingsError(f"'{ALL}' already means every node")
        return {name: ("",) for name in names}
    chosen: dict[str, tuple[str, ...]] = {}
    for target in targets:
        node, _, selector = target.partition("/")
        if node == ALL:
            raise SettingsError("a stack can only be named with its node")
        if node not in names:
            raise SettingsError(f"Unknown node '{node}'. Nodes: {', '.join(names)}")
        if selector not in chosen.get(node, ()):
            chosen[node] = (*chosen.get(node, ()), selector)
    return chosen
