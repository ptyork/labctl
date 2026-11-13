"""Controller-side files: labctl.toml (settings) and secrets.toml, both in ~/.config/labctl/.

labctl.toml (or $LABCTL_CONFIG_FILE):

    [global]
    lab_config = "/home/paul/dev/homelab/docker"

lab_config is the lab config: the folder that describes the lab (nodes/ and what they run), the root
of its own git repository. $LABCTL_LAB_CONFIG overrides it. With neither, a terminal is asked to
choose one (cli.common.load_settings); `labctl init` sets one up.

secrets.toml, next to it: a [global] table and [nodes.<node>] overrides.

$LABCTL_DOCKER overrides the docker CLI used for local compose resolution (default: docker).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SECRETS = "secrets.toml"
LAB_CONFIG_ENV = "LABCTL_LAB_CONFIG"


class SettingsError(Exception):
    pass


class LabConfigUnset(SettingsError):
    """Neither $LABCTL_LAB_CONFIG nor labctl.toml gives a usable lab config."""


@dataclass(frozen=True)
class Setting:
    """A setting in labctl.toml's [global] table."""

    name: str
    description: str
    default: str
    is_dir: bool = False
    needs: str = ""  # a folder the directory must have
    git_root: bool = False  # the directory must be the root of a git repository


SETTINGS: dict[str, Setting] = {
    s.name: s
    for s in (
        Setting(
            "lab_config",
            "The lab config (holds nodes/; the root of a git repo)",
            "",
            is_dir=True,
            needs="nodes",
            git_root=True,
        ),
    )
}


def config_file() -> Path:
    override = os.environ.get("LABCTL_CONFIG_FILE", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".config/labctl/labctl.toml"


def secrets_file() -> Path:
    return config_file().parent / _SECRETS


def read_config(path: Path) -> dict[str, str]:
    """The [global] table as strings; empty when the file does not exist."""
    return _strings(_read_toml(path).get("global", {}))


def write_config(path: Path, values: Mapping[str, str]) -> None:
    _write_tables(path, {"global": values})


def check_value(setting: Setting, value: str) -> str | None:
    """Error message for an unusable value, or None."""
    if not value.strip():
        return "A value is required"
    if setting.is_dir and not Path(value).expanduser().is_dir():
        return f"Not a directory: {value}"
    if setting.needs and not (Path(value).expanduser() / setting.needs).is_dir():
        return f"No {setting.needs}/ in {value}"
    if setting.git_root and not (Path(value).expanduser() / ".git").exists():
        return f"Not the root of a git repository: {value}"
    return None


@dataclass
class Secrets:
    """secrets.toml: [global] values and [nodes.<node>] overrides."""

    global_: dict[str, str] = field(default_factory=dict)
    nodes: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Secrets:
        data = _read_toml(path)
        nodes = {node: _strings(values) for node, values in data.get("nodes", {}).items()}
        return cls(global_=_strings(data.get("global", {})), nodes=nodes)

    def save(self, path: Path) -> None:
        tables = {"global": self.global_, **{f"nodes.{n}": v for n, v in self.nodes.items()}}
        _write_tables(path, tables)
        path.chmod(0o600)

    def get(self, node: str, key: str) -> str | None:
        """The value the node gets: its override, else the global value."""
        return self.nodes.get(node, {}).get(key, self.global_.get(key))

    def table(self, node: str | None) -> dict[str, str]:
        """[global] when node is None, else [nodes.<node>] (created if needed)."""
        return self.global_ if node is None else self.nodes.setdefault(node, {})

    def entries(self) -> list[tuple[str | None, str, str]]:
        """(node or None for global, key, value), global first."""
        found: list[tuple[str | None, str, str]] = [
            (None, k, v) for k, v in sorted(self.global_.items())
        ]
        for node, values in sorted(self.nodes.items()):
            found += [(node, k, v) for k, v in sorted(values.items())]
        return found


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _strings(table: Mapping[str, object]) -> dict[str, str]:
    return {k: str(v) for k, v in table.items()}


def _write_tables(path: Path, tables: Mapping[str, Mapping[str, str]]) -> None:
    """String-valued tables, e.g. {"global": {...}, "nodes.frodo": {...}}; empty ones skipped."""
    lines: list[str] = []
    for name, values in tables.items():
        if values or name == "global":
            lines += [f"[{name}]", *(_toml_line(k, v) for k, v in sorted(values.items())), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_line(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key} = "{escaped}"'


@dataclass(frozen=True)
class Settings:
    lab_config: Path
    config_dir: Path
    docker_cli: str = "docker"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / _SECRETS

    @property
    def nodes_dir(self) -> Path:
        return self.lab_config / "nodes"

    def node_names(self) -> list[str]:
        return node_names(self.nodes_dir)

    @classmethod
    def load(cls) -> Settings:
        """The lab config from $LABCTL_LAB_CONFIG, else labctl.toml's lab_config."""
        path = config_file()
        setting = SETTINGS["lab_config"]
        override = os.environ.get(LAB_CONFIG_ENV, "").strip()
        if override:
            problem = check_value(setting, override)
            if problem:
                raise SettingsError(f"{LAB_CONFIG_ENV}: {problem}")
            raw = override
        else:
            raw = read_config(path).get(setting.name, "").strip()
            if not raw:
                raise LabConfigUnset(f"No lab config: {path} has no {setting.name}")
            problem = check_value(setting, raw)
            if problem:
                raise LabConfigUnset(f"{setting.name} in {path}: {problem}")
        docker_cli = os.environ.get("LABCTL_DOCKER") or "docker"
        return cls(lab_config=Path(raw).expanduser(), config_dir=path.parent, docker_cli=docker_cli)


def node_names(nodes_dir: Path) -> list[str]:
    """Folders in nodes/ (not _shared and the like) holding at least one yml file."""
    if not nodes_dir.is_dir():
        raise SettingsError(f"No nodes directory: {nodes_dir}")
    return sorted(
        entry.name
        for entry in nodes_dir.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(("_", "."))
        and (any(entry.glob("*.yml")) or any(entry.glob("*.yaml")))
    )
