from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

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
