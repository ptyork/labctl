from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from conftest import MakeRepo, git, write_files

from labctl import repo as init_module
from labctl.cli import cli
from labctl.repo import ConfigRepo
from labctl.settings import LAB_CONFIG_ENV, Settings, read_config, write_config


@pytest.fixture(autouse=True)
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "labctl.toml"
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(path))
    monkeypatch.delenv(LAB_CONFIG_ENV, raising=False)
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.com")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return path


def init(*args: str, ok: bool = True) -> Result:
    result = CliRunner().invoke(cli.main, ["init", *args])
    assert (result.exit_code == 0) == ok, result.output + repr(result.exception)
    return result


def test_a_new_lab_config(tmp_path: Path, config: Path) -> None:
    root = tmp_path / "lab"
    init(str(root))
    assert sorted(git(root, "ls-files").split()) == [
        ".gitattributes",
        ".gitignore",
        "nodes/_shared/.gitkeep",
    ]
    assert git(root, "branch", "--show-current") == "main"
    assert git(root, "log", "--format=%s") == "labctl init"
    assert read_config(config)["lab_config"] == str(root)
    assert Settings.load().lab_config == root

    # Again: nothing to do.
    output = init(str(root)).output
    assert "already" in output and git(root, "log", "--format=%s") == "labctl init"


def test_an_existing_folder_is_committed_less_what_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    write_files(root, {"nodes/cirdan/node.env": "A=1\n", "nodes/cirdan/.env": "SECRET=1\n"})
    init(str(root))
    assert sorted(git(root, "ls-files").split()) == [
        ".gitattributes",
        ".gitignore",
        "nodes/_shared/.gitkeep",
        "nodes/cirdan/node.env",
    ]


def test_a_repo_with_commits_gets_only_what_is_missing(tmp_path: Path, make_repo: MakeRepo) -> None:
    root = tmp_path / "lab"
    make_repo(root, {"nodes/cirdan/node.env": "A=1\n"})
    (root / "nodes/cirdan/node.env").write_text("A=2\n")
    init(str(root))
    assert git(root, "show", "--name-only", "--format=%s", "HEAD").split() == [
        "labctl",
        "init",
        ".gitattributes",
        ".gitignore",
        "nodes/_shared/.gitkeep",
    ]
    assert git(root, "status", "--porcelain") == "M nodes/cirdan/node.env"  # yours to commit


def test_not_inside_another_repo(tmp_path: Path, make_repo: MakeRepo) -> None:
    make_repo(tmp_path / "outer", {"notes.md": "x\n"})
    result = init(str(tmp_path / "outer" / "lab"), ok=False)
    assert "inside the git repository" in result.output


def test_another_lab_config_stays(tmp_path: Path, config: Path) -> None:
    write_config(config, {"lab_config": "/elsewhere"})
    output = init(str(tmp_path / "lab")).output
    assert "lab_config stays /elsewhere" in output
    assert read_config(config)["lab_config"] == "/elsewhere"


def test_remote_gets_the_branch_and_the_tags(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "remote.git")
    url = str(tmp_path / "remote.git")
    root = tmp_path / "lab"
    init(str(root), "--remote", url)
    assert git(root, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert git(tmp_path / "remote.git", "log", "--format=%s", "main") == "labctl init"

    repo = ConfigRepo.open(root)
    git(root, "tag", "-a", "-m", "x", "applied/cirdan/20261101-090000")
    init(str(root), "--remote", url)  # again: pushes what is new
    assert git(tmp_path / "remote.git", "tag") == "applied/cirdan/20261101-090000"
    assert repo.remote_url("origin") == url

    result = init(str(root), "--remote", str(tmp_path / "other.git"), ok=False)
    assert f"origin is already {url}" in result.output


def test_github_creates_the_repo_with_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "remote.git")
    calls: list[tuple[str, ...]] = []

    def gh(*args: str, cwd: Path, quiet: bool = False) -> int:
        calls.append(args)
        if args[:2] == ("repo", "create"):
            git(cwd, "remote", "add", "origin", str(tmp_path / "remote.git"))
        return 0

    monkeypatch.setattr(init_module.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(init_module, "run_gh", gh)
    root = tmp_path / "lab"
    init(str(root), "--github", "me/lab")
    assert calls == [
        ("auth", "status"),
        ("repo", "create", "me/lab", "--private", "--source", ".", "--remote", "origin"),
    ]
    assert git(tmp_path / "remote.git", "log", "--format=%s", "main") == "labctl init"

    # origin exists now: --github never touches it.
    assert "already has origin" in init(str(root), "--github", "me/lab", ok=False).output


def test_github_needs_gh_logged_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_module.shutil, "which", lambda name: None)
    assert "--remote" in init(str(tmp_path / "a"), "--github", "lab", ok=False).output
    monkeypatch.setattr(init_module.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(init_module, "run_gh", lambda *args, cwd, quiet=False: 1)
    assert "gh auth login" in init(str(tmp_path / "b"), "--github", "lab", ok=False).output


def test_remote_and_github_are_alternatives(tmp_path: Path) -> None:
    result = init(str(tmp_path), "--remote", "x", "--github", "y", ok=False)
    assert "alternatives" in result.output
