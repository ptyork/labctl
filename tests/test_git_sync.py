"""Catching up with the upstream before a plan, and committing, tagging and pushing what was
applied: real git, with a bare repository as origin."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MakeRepo, git

from labctl.git_sync import (
    CaughtUp,
    branch_notices,
    catch_up,
    commit_applied,
    default_message,
    publish_applied,
)
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.state import Dirs

STAMP = "20261113-090000"
INFO = TagInfo("2026-11-13T09:00:00", "paul@aragorn", "0.1", ("x",), Dirs("/srv", "/srv/data"))


@pytest.fixture
def repo(tmp_path: Path, make_repo: MakeRepo) -> ConfigRepo:
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    repo = make_repo(tmp_path / "lab", {"nodes/cirdan/node.env": "A=1\n"})
    git(repo.root, "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(repo.root, "push", "-q", "-u", "origin", "main")
    return repo


def upstream_commit(repo: ConfigRepo, subject: str = "from elsewhere") -> None:
    """A commit on origin that this clone has fetched, not merged."""
    other = repo.root.parent / "other"
    if not other.exists():
        git(repo.root.parent, "clone", "-q", "origin.git", "other")
        git(other, "config", "user.name", "Other")
        git(other, "config", "user.email", "other@example.com")
    (other / f"{subject}.txt").write_text("x\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", subject)
    git(other, "push", "-q")
    assert repo.fetch() is None


def local_commit(repo: ConfigRepo) -> None:
    (repo.root / "local.txt").write_text("x\n")
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "-m", "local")


def test_up_to_date_is_left_alone(repo: ConfigRepo) -> None:
    assert catch_up(repo, ff=False, applying=True) == CaughtUp()
    assert branch_notices(repo, None) == []


def test_ff_fast_forwards_without_asking(repo: ConfigRepo) -> None:
    upstream_commit(repo)
    caught = catch_up(repo, ff=True, applying=True)
    assert caught.forwarded == "fast-forwarded main to origin/main (1 commit)"
    assert (repo.root / "from elsewhere.txt").exists()


def test_a_terminal_is_asked_and_may_go_on_behind(repo: ConfigRepo) -> None:
    upstream_commit(repo, "one")
    upstream_commit(repo, "two")
    asked: list[tuple[str, str]] = []

    def no(question: str, shown: str) -> bool:
        asked.append((question, shown))
        return False

    assert catch_up(repo, ff=False, applying=True, ask=no) == CaughtUp()
    assert asked == [("main is 2 commits behind origin/main. Fast-forward first?", "two; one")]
    assert "main is 2 commits behind origin/main" in branch_notices(repo, None)[0]
    assert catch_up(repo, ff=False, applying=True, ask=lambda q, s: True).forwarded
    assert branch_notices(repo, None) == []


def test_without_a_terminal_an_apply_stops_behind_and_a_plan_goes_on(repo: ConfigRepo) -> None:
    upstream_commit(repo)
    with pytest.raises(RepoError, match="Pass --ff"):
        catch_up(repo, ff=False, applying=True)
    assert catch_up(repo, ff=False, applying=False) == CaughtUp()


def test_a_diverged_branch_is_never_fast_forwarded(repo: ConfigRepo) -> None:
    upstream_commit(repo)
    local_commit(repo)
    with pytest.raises(RepoError, match="has diverged"):
        catch_up(repo, ff=True, applying=True)
    assert "1 ahead, 1 behind" in catch_up(repo, ff=True, applying=False).warning
    assert "has diverged" in catch_up(repo, ff=True, applying=True, ask=lambda q, s: True).warning


def test_a_fast_forward_that_would_overwrite_an_edit_fails(repo: ConfigRepo) -> None:
    upstream_commit(repo, "clash")
    (repo.root / "clash.txt").write_text("mine\n")
    with pytest.raises(RepoError, match="Cannot fast-forward main"):
        catch_up(repo, ff=True, applying=False)


def test_a_failed_fetch_is_a_notice_but_no_remote_is_not(repo: ConfigRepo) -> None:
    assert "not fetched (timed out)" in branch_notices(repo, "timed out")[0]
    assert branch_notices(repo, "no remote") == []


def test_what_was_applied_is_committed_tagged_and_pushed(repo: ConfigRepo, tmp_path: Path) -> None:
    base = repo.head()
    said: list[str] = []
    # Nothing changed: no commit, and the message is never asked for.
    assert commit_applied(repo, repo.snapshot(), base, lambda: 1 / 0, said.append) == base  # type: ignore[arg-type, return-value]
    assert said == []

    (repo.root / "nodes/cirdan/node.env").write_text("A=2\n")
    tree = repo.snapshot()
    message = default_message(repo, base, tree, ["cirdan"])
    assert message == "labctl apply cirdan: nodes/cirdan/node.env"
    commit = commit_applied(repo, tree, base, lambda: message, said.append)
    assert commit != base and said == ["All nodes successfully applied. Committing changes."]
    assert repo.advance(commit, base)

    published = publish_applied(repo, commit, STAMP, [("cirdan", INFO)], True, True)
    assert (published.commit, published.committed, published.subject) == (commit, True, message)
    assert (published.tagged, published.untagged) == (1, ())
    assert (published.remote, published.push_error) == ("origin", "")
    origin = tmp_path / "origin.git"
    assert git(origin, "rev-parse", "main") == commit
    assert git(origin, "tag") == f"applied/cirdan/{STAMP}"

    # The same stamp again: the tag exists, which is reported, not raised.
    again = publish_applied(repo, commit, STAMP, [("cirdan", INFO)], True, False)
    assert again.tagged == 0 and again.untagged[0][0] == "cirdan" and again.subject == ""


def test_without_a_remote_it_stays_in_the_clone(tmp_path: Path, make_repo: MakeRepo) -> None:
    repo = make_repo(tmp_path / "lab", {"nodes/cirdan/node.env": "A=1\n"})
    published = publish_applied(repo, repo.head(), STAMP, [("cirdan", INFO)], True, False)
    assert (published.tagged, published.remote, published.push_error) == (1, None, "")
