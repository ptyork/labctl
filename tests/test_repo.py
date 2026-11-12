from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import MakeRepo, git, write_files

from labctl.repo import ConfigRepo, RepoError, TagInfo, parse_tag_message, tag_message
from labctl.state import Dirs

FILES = {
    "nodes/cirdan/docker-compose.yml": "name: homelab-cirdan\n",
    "nodes/cirdan/node.env": "DEPLOY_DIR=/srv/docker\n",
    "apps/plex.yml": "services: {}\n",
    "run.sh": "#!/bin/sh\n",
    ".gitignore": "*.swp\n.env\n",
}
INFO = TagInfo("2026-11-03T10:00:00", "me@here", "0.1.0", ("a", "b"), Dirs("/srv/docker", "/srv/d"))


def files_in(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_not_a_repo_or_no_commits(tmp_path: Path) -> None:
    with pytest.raises(RepoError, match="not a git repository"):
        ConfigRepo.open(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    with pytest.raises(RepoError, match="no commits yet"):
        ConfigRepo.open(tmp_path)


def test_export_and_worktree_are_the_same_tree(tmp_path: Path, make_repo: MakeRepo) -> None:
    os.chmod(tmp_path, 0o755)
    repo = make_repo(tmp_path / "root", FILES)
    (tmp_path / "root" / "run.sh").chmod(0o755)
    repo.commit("executable")
    write_files(tmp_path / "root", {"apps/plex.yml.swp": "junk", "nodes/cirdan/.env": "SECRET=1"})

    repo.export("HEAD", tmp_path / "commit")
    repo.export_worktree(tmp_path / "work")
    assert files_in(tmp_path / "commit") == files_in(tmp_path / "work") == FILES
    for tree in ("commit", "work"):
        mode = stat.S_IMODE((tmp_path / tree / "run.sh").stat().st_mode)
        assert mode == 0o755, tree
        assert stat.S_IMODE((tmp_path / tree / "apps/plex.yml").stat().st_mode) == 0o644

    # The working tree as it is: an edit, a new file, a deletion; HEAD stays as committed.
    write_files(tmp_path / "root", {"apps/plex.yml": "edited\n", "apps/new.yml": "x\n"})
    (tmp_path / "root" / "run.sh").unlink()
    repo.export_worktree(tmp_path / "work2")
    assert files_in(tmp_path / "work2") == {
        **{k: v for k, v in FILES.items() if k != "run.sh"},
        "apps/plex.yml": "edited\n",
        "apps/new.yml": "x\n",
    }
    assert sorted(c.path for c in repo.changes()) == [
        "apps/new.yml",
        "apps/plex.yml",
        "run.sh",
    ]


def test_the_lab_config_is_the_root_of_its_repo(tmp_path: Path, make_repo: MakeRepo) -> None:
    root = tmp_path / "repo"
    make_repo(root, {**{f"docker/{k}": v for k, v in FILES.items()}, "notes.md": "x\n"})
    with pytest.raises(RepoError, match=f"inside the git repository at {root}"):
        ConfigRepo.open(root / "docker")
    with pytest.raises(RepoError, match=f"inside the git repository at {root}"):
        ConfigRepo.init(root / "docker")


def test_tag_message_round_trip_and_nothing_secret() -> None:
    message = tag_message("cirdan", INFO)
    assert parse_tag_message(message) == INFO
    assert "sha256" not in message and "manifest" not in message and ".env" not in message
    no_stacks = TagInfo("t", "", "", (), Dirs("/a", "/b"))
    # git strips the trailing space of "Stacks-Up: " when it stores the message
    assert parse_tag_message(tag_message("x", no_stacks).replace(": \n", ":\n")) == no_stacks
    assert parse_tag_message("some tag someone made by hand") is None
    # Made before DOCKER_HOME was renamed DEPLOY_DIR.
    assert parse_tag_message(message.replace("Deploy-Dir:", "Docker-Home:")) == INFO
    # The node's address, when `_HOST_IP_` used it (a tag from before has none).
    addressed = replace(INFO, host_ip="10.11.12.31")
    assert "Host-IP: 10.11.12.31" in tag_message("frodo", addressed)
    assert parse_tag_message(tag_message("frodo", addressed)) == addressed
    assert "Host-IP" not in message


def test_the_latest_apply_tag_of_a_node(tmp_path: Path, make_repo: MakeRepo) -> None:
    repo = make_repo(tmp_path, FILES)
    first = repo.head()
    (tmp_path / "apps/plex.yml").write_text("v2\n")
    second = repo.commit("v2")
    assert repo.last_applied("cirdan") is None
    repo.tag_applied("cirdan", "20261101-090000", first, INFO)
    repo.tag_applied("cirdan", "20261102-090000", second, INFO)
    repo.tag_applied("cirdan2", "20261103-090000", first, INFO)
    latest = repo.last_applied("cirdan")
    assert latest is not None
    assert (latest.tag, latest.commit, latest.stamp) == (
        "applied/cirdan/20261102-090000",
        second,
        "20261102-090000",
    )
    assert latest.info == INFO
    assert repo.tag_commit("applied/cirdan/20261101-090000") == first
    assert repo.tag_commit("applied/cirdan/nope") is None
    assert repo.count_between(first, second) == 1
    assert repo.subjects(f"{first}..{second}") == ["v2"]


def test_publish_and_fetch_tags(tmp_path: Path, make_repo: MakeRepo) -> None:
    repo = make_repo(tmp_path / "a", FILES)
    assert repo.publish() == "no remote" and repo.fetch() == "no remote"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "remote.git")
    git(tmp_path / "a", "remote", "add", "origin", str(tmp_path / "remote.git"))
    assert repo.publish() is None  # no tags yet: nothing to push, not an error
    repo.tag_applied("cirdan", "20261101-090000", repo.head(), INFO)
    assert repo.publish() is None
    assert git(tmp_path / "remote.git", "branch") == ""  # tags only: the branch is yours to push
    git(tmp_path / "a", "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), "b")
    other = ConfigRepo.open(tmp_path / "b")
    repo.tag_applied("cirdan", "20261102-090000", repo.head(), INFO)
    repo.publish()
    assert other.fetch() is None
    latest = other.last_applied("cirdan")
    assert latest is not None and latest.tag == "applied/cirdan/20261102-090000"

    git(tmp_path / "a", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    failed = repo.publish()
    assert failed is not None and failed.startswith("git push origin:")


def test_a_snapshot_is_the_working_tree_and_touches_nothing(
    tmp_path: Path, make_repo: MakeRepo
) -> None:
    repo = make_repo(tmp_path, FILES)
    write_files(tmp_path, {"apps/plex.yml": "edited\n", "new.yml": "x\n", "x.swp": "junk"})
    git(tmp_path, "add", "apps/plex.yml")  # staged or not, the working tree is what counts
    tree = repo.snapshot()
    assert git(tmp_path, "ls-tree", "-r", "--name-only", tree).split() == sorted(
        [*FILES, "new.yml"]
    )  # x.swp is ignored
    assert git(tmp_path, "show", f"{tree}:apps/plex.yml") == "edited"
    assert git(tmp_path, "status", "--porcelain").splitlines() == [
        "M  apps/plex.yml",
        "?? new.yml",
    ]  # the index as it was


def test_the_branch_moves_only_from_where_it_was(tmp_path: Path, make_repo: MakeRepo) -> None:
    repo = make_repo(tmp_path, FILES)
    first = repo.head()
    (tmp_path / "new.yml").write_text("x\n")
    made = repo.commit_tree(repo.snapshot(), first, "applied")
    (tmp_path / "other.yml").write_text("y\n")
    moved = repo.commit("someone committed meanwhile")
    assert not repo.advance(made, first)
    assert repo.head() == moved


def remote_and_clone(tmp_path: Path, make_repo: MakeRepo) -> tuple[ConfigRepo, Path]:
    """This clone (tracking origin/main) and another one, both of the bare origin."""
    repo = make_repo(tmp_path / "a", FILES)
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    git(tmp_path / "a", "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(tmp_path / "a", "push", "-q", "-u", "origin", "main")
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "b")
    for key, value in (("user.name", "Other"), ("user.email", "o@example.com")):
        git(tmp_path / "b", "config", key, value)
    return repo, tmp_path / "b"


def test_apply_pushes_the_branch_with_its_tags(tmp_path: Path, make_repo: MakeRepo) -> None:
    repo, _ = remote_and_clone(tmp_path, make_repo)
    (tmp_path / "a/new.yml").write_text("x\n")
    made = repo.commit_tree(repo.snapshot(), repo.head(), "applied")
    assert repo.advance(made, git(tmp_path / "a", "rev-parse", "HEAD~0"))
    repo.tag_applied("cirdan", "20261105-090000", made, INFO)
    assert repo.publish(branch=True) is None
    origin = tmp_path / "origin.git"
    assert git(origin, "rev-parse", "main") == made
    assert git(origin, "tag") == "applied/cirdan/20261105-090000"


def test_a_push_the_remote_moved_past_is_refused_not_forced(
    tmp_path: Path, make_repo: MakeRepo
) -> None:
    repo, other = remote_and_clone(tmp_path, make_repo)
    (other / "theirs.yml").write_text("t\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q")
    (tmp_path / "a/mine.yml").write_text("m\n")
    repo.commit("mine")
    failed = repo.publish(branch=True)
    assert failed is not None and "rejected" in failed
    assert git(tmp_path / "origin.git", "log", "-1", "--format=%s", "main") == "theirs"


def test_fast_forward_keeps_uncommitted_changes_unless_git_would_overwrite_them(
    tmp_path: Path, make_repo: MakeRepo
) -> None:
    repo, other = remote_and_clone(tmp_path, make_repo)
    (other / "apps/plex.yml").write_text("theirs\n")
    git(other, "commit", "-q", "-am", "plex")
    git(other, "push", "-q")
    repo.fetch()
    (tmp_path / "a/apps/plex.yml").write_text("mine\n")  # the same file: git refuses
    problem = repo.fast_forward("origin/main")
    assert problem is not None and "apps/plex.yml" in problem
    git(tmp_path / "a", "checkout", "-q", "--", "apps/plex.yml")
    (tmp_path / "a/run.sh").write_text("mine\n")  # another file: it stays
    assert repo.fast_forward("origin/main") is None
    assert (tmp_path / "a/apps/plex.yml").read_text() == "theirs\n"
    assert (tmp_path / "a/run.sh").read_text() == "mine\n"
