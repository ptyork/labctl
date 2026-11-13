"""labctl history: the applies (from the tags), references, list/show/restore/diff."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from conftest import MakeRepo, git

from labctl.cli import cli, menu
from labctl.cli.commands import history as history_module
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.settings import LAB_CONFIG_ENV
from labctl.state import Dirs

FILES = {
    "nodes/frodo/docker-compose.yml": "name: homelab-frodo\n",
    "nodes/bilbo/docker-compose.yml": "name: homelab-bilbo\n",
    "apps/plex.yml": "services: {}\n",
}
INFO = TagInfo("2026-11-01T09:00:00", "me@here", "0.1.0", ("homelab-x",), Dirs("/srv", "/srv/a"))


def info(when: str, **changes: object) -> TagInfo:
    return replace(INFO, applied_at=when, **changes)  # type: ignore[arg-type]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_repo: MakeRepo) -> ConfigRepo:
    """Three applies: both nodes; frodo after a change; bilbo again (nothing new)."""
    root = tmp_path / "lab"
    repo = make_repo(root, FILES)
    first = repo.head()
    repo.tag_applied("frodo", "20261101-090000", first, info("2026-11-01T09:00:00"))
    repo.tag_applied("bilbo", "20261101-090000", first, info("2026-11-01T09:00:00"))
    (root / "apps/plex.yml").write_text("services: {plex: {image: plex}}\n")
    second = repo.commit("add plex")
    repo.tag_applied("frodo", "20261102-090000", second, info("2026-11-02T09:00:00"))
    repo.tag_applied("bilbo", "20261103-090000", second, info("2026-11-03T09:00:00"))
    monkeypatch.setenv(LAB_CONFIG_ENV, str(root))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    monkeypatch.setattr(history_module, "is_terminal", lambda: False)
    return repo


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.main, ["history", *args])


def test_applies_and_references(lab: ConfigRepo) -> None:
    applies = lab.applies()
    assert [(a.stamp, a.nodes, a.again) for a in applies] == [
        ("20261103-090000", ("bilbo",), True),  # the same commit as the one before
        ("20261102-090000", ("frodo",), False),
        ("20261101-090000", ("bilbo", "frodo"), False),
    ]
    assert applies[1].subject == "add plex"
    assert lab.resolve("last", applies)[1] == applies[0]
    assert lab.resolve("previous", applies)[1] == applies[1]
    assert lab.resolve("20261101-090000", applies)[1] == applies[2]
    assert lab.resolve("applied/frodo/20261102-090000", applies)[1] == applies[1]
    commit, apply = lab.resolve(applies[2].commit[:7], applies)
    assert commit == applies[2].commit and apply == applies[2]
    with pytest.raises(RepoError, match="names no apply or commit"):
        lab.resolve("nope", applies)


def test_list_filters_by_node(lab: ConfigRepo) -> None:
    rows = run("list").output.splitlines()
    assert len(rows) == 3 and "no lab config changes" in rows[0] and "add plex" in rows[1]
    assert rows[0].startswith("2026-11-03 09:00:00")
    frodo = run("list", "frodo").output.splitlines()
    assert [row[:10] for row in frodo] == ["2026-11-02", "2026-11-01"]
    assert "No apply tagged nope" in run("list", "nope").output


def test_show(lab: ConfigRepo) -> None:
    out = run("show", "20261102-090000").output
    assert "Apply 20261102-090000" in out and "frodo    homelab-x" in out
    assert "Changed since 20261101-090000" in out and "apps/plex.yml" in out
    assert "No lab config changes since" in run("show", "last").output
    assert "Name the apply: last, previous" in run("show").output  # no terminal


def test_show_picks_in_a_terminal(lab: ConfigRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    offered: list[str] = []

    def choose(title: str, options: list[str], **kwargs: object) -> int:
        offered.extend(options)
        return 2

    monkeypatch.setattr(history_module, "is_terminal", lambda: True)
    monkeypatch.setattr(menu, "choose", choose)
    assert "Apply 20261101-090000" in run("show").output
    assert len(offered) == 3 and offered[0].startswith("2026-11-03")


def test_restore_puts_the_files_back_uncommitted(lab: ConfigRepo) -> None:
    head = lab.head()
    result = run("restore", "20261101-090000")
    assert result.exit_code == 0 and "1 file differ" in result.output
    assert (lab.root / "apps/plex.yml").read_text() == "services: {}\n"
    assert lab.head() == head and [c.path for c in lab.changes()] == ["apps/plex.yml"]
    result = run("restore", "last")  # there are changes now
    assert result.exit_code == 1 and "labctl discard" in result.output
    git(lab.root, "checkout", "-q", "--", ".")
    assert "already as at" in run("restore", "last").output


def test_restore_removes_files_the_apply_did_not_have(lab: ConfigRepo) -> None:
    (lab.root / "apps/new.yml").write_text("services: {}\n")
    lab.commit("new app")
    run("restore", "last")
    assert not (lab.root / "apps/new.yml").exists()


def test_diff_limits_to_the_nodes_files(lab: ConfigRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        ConfigRepo, "diff", lambda self, rev, specs: calls.append((rev, specs)) or 0
    )
    first = lab.applies()[2].commit
    assert run("diff", "20261101-090000", "frodo").exit_code == 0
    assert run("diff", "frodo").exit_code == 1  # a node first: no REF, and no terminal
    assert run("diff", "last").exit_code == 0
    assert calls == [
        (first, [".", ":(exclude)nodes/bilbo"]),
        (lab.applies()[0].commit, []),
    ]
