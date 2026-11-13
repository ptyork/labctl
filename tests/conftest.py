from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from labctl.remote import Unreachable
from labctl.repo import ConfigRepo
from labctl.settings import Settings

# For tests that resolve real compose files.
needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")


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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(lab_config=tmp_path / "repo", config_dir=tmp_path / "config")


@pytest.fixture(autouse=True)
def no_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach a real node: collect finds every node unreachable unless a test says
    otherwise (by replacing read_node)."""

    def unreachable(remote: Any, *args: Any) -> Any:
        raise Unreachable(f"{remote.host}: not in tests")

    monkeypatch.setattr("labctl.collect.read_node", unreachable)
