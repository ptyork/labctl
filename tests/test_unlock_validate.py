"""labctl unlock (an interrupted run's lock) and labctl validate (the lab config, no node)."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import MakeRepo
from test_runtime import Services, desired

from labctl.apply import HeldLock
from labctl.bundle import Desired
from labctl.cli import cli
from labctl.cli.commands import unlock as unlock_module
from labctl.cli.commands import validate as validate_module
from labctl.compose import ComposeError
from labctl.envfile import PLACEHOLDER_PREFIX
from labctl.settings import LAB_CONFIG_ENV

FILES = {
    f"nodes/{node}/{name}": text
    for node in ("cirdan", "frodo")
    for name, text in (("docker-compose.yml", "services: {}\n"), ("node.env", "A=1\n"))
}


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_repo: MakeRepo) -> Path:
    root = tmp_path / "docker"
    make_repo(root, FILES)
    monkeypatch.setenv(LAB_CONFIG_ENV, str(root))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "config" / "labctl.toml"))
    return root


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.main, list(args))
    return result.exit_code, result.output


#############################################################################
# unlock
#############################################################################


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> dict[str, HeldLock]:
    """The locks held, by node; unlock removes them."""
    held = {"cirdan": HeldLock("paul@aragorn 2026-11-11T20:00:00", ("cirdan: stacks down",))}
    monkeypatch.setattr(unlock_module, "read_lock", lambda remote: held.get(remote.host))
    monkeypatch.setattr(unlock_module, "unlock", lambda remote: held.pop(remote.host))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return held


def test_unlock_shows_the_lock_and_needs_an_answer(root: Path, locks: dict[str, HeldLock]) -> None:
    code, output = run("unlock", "all")
    assert code == 1 and "Confirm in a terminal, or pass --yes" in output
    assert "cirdan: locked by paul@aragorn" in output and "cirdan: stacks down" in output
    assert "frodo: not locked" in output and "cirdan" in locks


def test_unlock_yes_removes_only_held_locks(root: Path, locks: dict[str, HeldLock]) -> None:
    code, output = run("unlock", "cirdan", "frodo", "--yes")
    assert code == 0 and "cirdan: unlocked" in output and locks == {}


def test_unlock_nothing_locked_asks_nothing(root: Path, locks: dict[str, HeldLock]) -> None:
    code, output = run("unlock", "frodo")
    assert code == 0 and output == "frodo: not locked\n"


def test_unlock_needs_its_nodes_named(root: Path, locks: dict[str, HeldLock]) -> None:
    code, output = run("unlock")
    assert code == 2 and "Missing argument" in output
    code, output = run("unlock", "frodo/media")
    assert code == 1 and "give NODE, not NODE/STACK" in output


#############################################################################
# validate
#############################################################################


def _desired(
    services: Services | None = None, placeholders: tuple[str, ...] = (), **not_stacks: str
) -> Desired:
    built = desired("frodo", {"homelab": services or {"s0": {}, "s1": {}}})
    env = replace(built.env, placeholders=placeholders)
    return replace(built, env=env, not_stacks=not_stacks)


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What resolving each node gives (an exception is raised); the calls must be offline."""
    results: dict[str, Any] = {}

    def resolve(settings: Any, node: str, *args: Any, **kwargs: Any) -> Any:
        assert args[2] is None and kwargs == {"offline": True}  # never asks, no lookups
        result = results.get(node, _desired())
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(validate_module, "resolve_desired", resolve)
    return results


def test_validate_all_by_default(root: Path, resolved: dict[str, Any]) -> None:
    code, output = run("validate")
    assert code == 0
    assert output == "✓ cirdan: 1 stack, 2 services\n✓ frodo: 1 stack, 2 services\n"


def test_validate_reports_every_node_and_fails(root: Path, resolved: dict[str, Any]) -> None:
    resolved["cirdan"] = ComposeError("cirdan: APPDATA_DIR must be an absolute path")
    resolved["frodo"] = _desired(placeholders=("TOKEN",), **{"notes.yml": "defines no services"})
    code, output = run("validate")
    assert code == 1
    assert "✗ cirdan: APPDATA_DIR must be an absolute path" in output
    assert "! frodo: notes.yml is not a stack (defines no services)" in output
    assert "✗ frodo: no value for TOKEN" in output


def test_validate_fails_on_services_publishing_one_port(
    root: Path, resolved: dict[str, Any]
) -> None:
    def published(port: str, host_ip: str = "") -> dict[str, Any]:
        return {"ports": [{"published": port, "target": 80, "host_ip": host_ip}]}

    resolved["frodo"] = _desired(
        {
            "web": published("80"),
            "proxy": published("80", f"{PLACEHOLDER_PREFIX}HOST_IP"),
            "dns": published("53"),
            "other": published("53", "10.0.0.9") | {"network_mode": "host"},  # publishes nothing
        }
    )
    code, output = run("validate", "frodo")
    assert code == 1
    assert output == "✗ frodo: 80/tcp is published by proxy (on the host IP) and web\n"


def test_validate_only_the_nodes_named(root: Path, resolved: dict[str, Any]) -> None:
    resolved["cirdan"] = ComposeError("cirdan: broken")
    code, output = run("validate", "frodo")
    assert code == 0 and "cirdan" not in output
    code, output = run("validate", "nope")
    assert code == 1 and "Unknown node(s): nope" in output
