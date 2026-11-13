from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest

from labctl.cli import menu
from labctl.cli.common import load_settings
from labctl.envfile import preview_env
from labctl.settings import (
    LAB_CONFIG_ENV,
    SETTINGS,
    LabConfigUnset,
    Secrets,
    Settings,
    SettingsError,
    check_value,
    read_config,
    write_config,
)


def test_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "labctl.toml"
    assert read_config(path) == {}
    write_config(path, {"lab_config": str(tmp_path), "docker": 'odd "name"'})
    assert read_config(path) == {"lab_config": str(tmp_path), "docker": 'odd "name"'}


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "labctl.toml"
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(path))
    monkeypatch.delenv("LABCTL_DOCKER", raising=False)
    monkeypatch.delenv(LAB_CONFIG_ENV, raising=False)
    return path


def lab_config(path: Path) -> Path:
    (path / "nodes").mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def test_load_uses_config_file(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(LabConfigUnset, match="has no lab_config"):
        Settings.load()
    write_config(config, {"lab_config": str(tmp_path / "missing")})
    with pytest.raises(LabConfigUnset, match="Not a directory"):
        Settings.load()
    write_config(config, {"lab_config": str(tmp_path)})
    with pytest.raises(LabConfigUnset, match="No nodes/ in"):
        Settings.load()
    root = lab_config(tmp_path / "root")
    write_config(config, {"lab_config": str(root)})
    monkeypatch.setenv("LABCTL_DOCKER", "podman")
    settings = Settings.load()
    assert (settings.lab_config, settings.docker_cli, settings.config_dir) == (
        root,
        "podman",
        tmp_path,
    )


def test_the_environment_wins(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(config, {"lab_config": str(lab_config(tmp_path / "toml"))})
    monkeypatch.setenv(LAB_CONFIG_ENV, str(lab_config(tmp_path / "env")))
    assert Settings.load().lab_config == tmp_path / "env"
    # Set, but not usable: an error, never a fall back to labctl.toml (nor a question).
    monkeypatch.setenv(LAB_CONFIG_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(SettingsError, match=f"{LAB_CONFIG_ENV}: Not a directory") as raised:
        Settings.load()
    assert not isinstance(raised.value, LabConfigUnset)


def test_without_a_lab_config_a_terminal_chooses_one(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen: list[Path | None] = []
    monkeypatch.setattr(menu, "choose_dir", lambda title, initial: chosen.pop(0))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(click.ClickException, match=f"Set {LAB_CONFIG_ENV}"):
        load_settings()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    chosen[:] = [None]
    with pytest.raises(click.ClickException, match="No lab config chosen"):
        load_settings()
    chosen[:] = [tmp_path]  # no nodes/ in it
    with pytest.raises(click.ClickException, match="No nodes/ in"):
        load_settings()
    assert not config.exists()

    chosen[:] = [lab_config(tmp_path / "root")]
    assert load_settings().lab_config == tmp_path / "root"
    assert read_config(config)["lab_config"] == str(tmp_path / "root")


def test_check_value(tmp_path: Path) -> None:
    assert check_value(SETTINGS["lab_config"], str(lab_config(tmp_path))) is None
    assert (
        check_value(SETTINGS["lab_config"], str(tmp_path / "x")) == f"Not a directory: {tmp_path}/x"
    )
    assert check_value(SETTINGS["lab_config"], str(tmp_path / "nodes")) == (
        f"No nodes/ in {tmp_path}/nodes"
    )
    (tmp_path / "plain" / "nodes").mkdir(parents=True)
    assert check_value(SETTINGS["lab_config"], str(tmp_path / "plain")) == (
        f"Not the root of a git repository: {tmp_path}/plain"
    )
    assert check_value(SETTINGS["lab_config"], " ") == "A value is required"


def test_secrets_tables_and_entries(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"
    secrets = Secrets()
    secrets.table(None)["TOKEN"] = "g"
    secrets.table("frodo")["TOKEN"] = "f"
    secrets.table("bilbo")["API"] = "b"
    secrets.save(path)
    loaded = Secrets.load(path)
    assert loaded.entries() == [
        (None, "TOKEN", "g"),
        ("bilbo", "API", "b"),
        ("frodo", "TOKEN", "f"),
    ]
    assert (loaded.get("frodo", "TOKEN"), loaded.get("bilbo", "TOKEN")) == ("f", "g")


def test_preview_env_merges_shared_then_node_and_expands(tmp_path: Path) -> None:
    (tmp_path / "_shared").mkdir()
    (tmp_path / "_shared" / "common.env").write_text("TZ=UTC\nAPPDATA_DIR=/srv\n")
    (tmp_path / "cirdan").mkdir()
    (tmp_path / "cirdan" / "node.env").write_text("APPDATA_DIR=/data\nDEPLOY_DIR=$APPDATA_DIR\n")
    assert preview_env(tmp_path, "cirdan") == {
        "TZ": "UTC",
        "APPDATA_DIR": "/data",
        "DEPLOY_DIR": "/data",
    }
    assert preview_env(tmp_path, "nowhere") == {"TZ": "UTC", "APPDATA_DIR": "/srv"}
