"""settings edit / secrets edit, driven by scripted keys through the real get_rich controls."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner
from get_rich import BaseControl

from labctl.cli import cli, menu
from labctl.cli.commands import secrets as secrets_module
from labctl.settings import Secrets, read_config, write_config


class Keys:
    def __init__(self, keys: Iterable[str]) -> None:
        self.keys = list(keys)

    def __enter__(self) -> Keys:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read_key(self) -> str:
        if not self.keys:
            raise AssertionError("ran out of keys (a menu did not exit)")
        return self.keys.pop(0)


@pytest.fixture
def press(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Keys:
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    keys = Keys([])
    monkeypatch.setattr(BaseControl, "_get_reader", lambda self: keys)
    monkeypatch.setattr(secrets_module, "_node_names", lambda: ["cirdan", "frodo"])
    return keys


def run(*args: str) -> str:
    result = CliRunner().invoke(cli.main, list(args))
    assert result.exit_code == 0, result.output + repr(result.exception)
    return result.output


def test_settings_edit_escape_in_chooser_changes_nothing(press: Keys, tmp_path: Path) -> None:
    press.keys = ["ENTER", "DOWN_ARROW", "ESC", "ESC"]
    run("settings", "edit")
    assert not (tmp_path / "labctl.toml").exists()


def test_settings_edit_directory_with_file_chooser(press: Keys, tmp_path: Path) -> None:
    (tmp_path / "repo" / "nodes").mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    write_config(tmp_path / "labctl.toml", {"lab_config": str(tmp_path)})
    # The chooser opens at the current value; rows are SELECT, .., then subdirectories.
    press.keys = ["ENTER", "DOWN_ARROW", "DOWN_ARROW", "ENTER", "ENTER", "ESC"]
    run("settings", "edit")
    assert read_config(tmp_path / "labctl.toml")["lab_config"] == str(tmp_path / "repo")


def test_secrets_edit_change_and_remove(
    press: Keys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.toml"
    secrets = Secrets(global_={"A": "first-value", "B": "x"})
    secrets.save(path)
    press.keys = [
        *["DOWN_ARROW", "ENTER", *["BACKSPACE"] * 5, *"one", "ENTER"],  # A -> first-one
        *["DOWN_ARROW", "ENTER", "BACKSPACE", "ENTER"],  # clear B -> removed
        "ESC",
    ]
    shown: list[str] = []
    choose = menu.choose

    def spy(title: str, header: str, options: Sequence[str], selected: int = 0) -> int | None:
        shown.extend(options)
        return choose(title, header, options, selected)

    monkeypatch.setattr(menu, "choose", spy)
    run("secrets", "edit")
    assert Secrets.load(path).global_ == {"A": "first-one"}
    assert any("first-value" in option for option in shown)  # values are shown


def test_secrets_edit_add_node_override(press: Keys, tmp_path: Path) -> None:
    press.keys = [
        *["ENTER", *"TOKEN", "ENTER"],  # name
        *["DOWN_ARROW", "DOWN_ARROW", "ENTER"],  # scope: global, cirdan, frodo
        *"s3cret",
        "ENTER",
        "ESC",
    ]
    run("secrets", "edit")
    assert Secrets.load(tmp_path / "secrets.toml").nodes == {"frodo": {"TOKEN": "s3cret"}}
