from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from labctl.remote import Unreachable
from labctl.repo import ConfigRepo


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


MakeRepo = Callable[[Path, Mapping[str, str]], ConfigRepo]


@pytest.fixture
def make_repo() -> MakeRepo:
    """A config repo at ``root`` holding ``files``, committed once."""

    def make(root: Path, files: Mapping[str, str]) -> ConfigRepo:
        write_files(root, files)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.name", "Test")
        git(root, "config", "user.email", "test@example.com")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "first")
        return ConfigRepo.open(root)

    return make


@pytest.fixture(autouse=True)
def no_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach a real node: collect finds every node unreachable unless a test says
    otherwise (by replacing read_node)."""

    def unreachable(remote: Any, *args: Any) -> Any:
        raise Unreachable(f"{remote.host}: not in tests")

    monkeypatch.setattr("labctl.collect.read_node", unreachable)
