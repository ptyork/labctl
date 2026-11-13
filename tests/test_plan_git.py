"""plan and apply against the config repo: what is planned (working tree or a commit), commits."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import MakeRepo, git

from labctl import collect as collect_module
from labctl import git_sync, planning, rollout
from labctl.apply import ApplyError
from labctl.bundle import Desired
from labctl.cli import cli, render
from labctl.cli.commands import plan as plan_module
from labctl.collect import collect
from labctl.envfile import CompiledEnv
from labctl.git_sync import CaughtUp
from labctl.repo import ConfigRepo, RepoError
from labctl.settings import LAB_CONFIG_ENV, Secrets, Settings
from labctl.state import NodeState, Scope, Service

FILES = {"nodes/cirdan/docker-compose.yml": "services: {}\n", "nodes/cirdan/node.env": "A=1\n"}


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_repo: MakeRepo) -> Path:
    root = tmp_path / "docker"
    make_repo(root, FILES)
    monkeypatch.setenv(LAB_CONFIG_ENV, str(root))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "config" / "labctl.toml"))
    return root


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The rev each collect was asked for; collect itself stops the command there."""
    revs: list[str | None] = []

    def stop(*args: Any, **kwargs: Any) -> Iterator[None]:
        revs.append(args[5])
        raise RepoError("stopped here")

    monkeypatch.setattr(plan_module, "collect", stop)
    return revs


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.main, list(args))
    return result.exit_code, result.output


def test_nothing_is_committed_before_it_is_applied(root: Path, planned: list[str | None]) -> None:
    (root / "nodes/cirdan/docker-compose.yml").write_text("services: {x: {image: y}}\n")
    (root / "new.yml").write_text("x\n")
    head = git(root, "rev-parse", "HEAD")

    # plan takes the working tree as it is
    code, output = run("plan", "cirdan")
    assert planned == [None] and "stopped here" in output

    # apply plans a snapshot of it (a tree, no commit); here it stops before applying
    run("apply", "cirdan")
    assert planned[1] == ConfigRepo.open(root).snapshot()
    assert git(root, "rev-parse", "HEAD") == head
    assert len(git(root, "status", "--porcelain").splitlines()) == 2


def test_answers_given_at_a_prompt_are_never_committed(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ConfigRepo.open(root)
    settings = Settings(lab_config=root, config_dir=tmp_path / "config")

    def resolve(s: Settings, node: str, secrets: Secrets, work: Path, *rest: Any) -> Desired:
        with (root / "nodes" / node / "node.env").open("a") as env:  # as resolve_desired does
            env.write("TZ=UTC\n")
        values = {"DEPLOY_DIR": "/srv", "APPDATA_DIR": "/srv/a"}
        env = CompiledEnv(values=values, new_node_vars={"TZ": "UTC"})
        return Desired(node, work, env, {}, NodeState.of(node), {})

    monkeypatch.setattr(collect_module, "resolve_desired", resolve)
    not_read = Scope(frozenset({"cirdan"}))
    before = repo.snapshot()
    with collect(settings, not_read, Secrets(), None, repo, before) as result:
        assert result.tree == repo.snapshot() != before  # apply goes on with the answer in it
    with collect(settings, not_read, Secrets(), None, repo) as result:
        assert result.tree is None  # plan: the working tree
    assert git(root, "rev-list", "--count", "HEAD") == "1"
    assert git(root, "status", "--porcelain") == "M nodes/cirdan/node.env"


def test_apply_stops_when_the_lab_config_changed_since_the_plan(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks: list[str] = []
    monkeypatch.setattr(rollout, "lock", lambda remote, owner: locks.append(remote.host))
    monkeypatch.setattr(rollout, "unlock", lambda remote: locks.remove(remote.host))
    repo = ConfigRepo.open(root)
    settings = Settings(lab_config=root, config_dir=tmp_path / "config")
    options = planning.Options((), False, (), False)

    def apply(tree: str, base: str) -> None:
        planned = planning.Planned(tree=tree, base=base, options=options)
        rollout.roll_out(settings, Scope(), Secrets(), planned, ["cirdan"], 5, repo)

    with pytest.raises(ApplyError, match="HEAD moved from 0000000 to"):
        apply(repo.snapshot(), "0" * 40)
    (root / "new.yml").write_text("x\n")  # edited after the plan
    with pytest.raises(ApplyError, match="The lab config changed since the plan"):
        apply(repo.tree_of("HEAD"), repo.head())
    assert locks == []  # unlocked again


def test_once_applied_the_snapshot_is_committed_and_the_branch_moves(root: Path) -> None:
    repo = ConfigRepo.open(root)
    head = repo.head()
    (root / "new.yml").write_text("x\n")
    tree = repo.snapshot()
    asked: list[str] = []

    def message() -> str:
        asked.append("asked")
        return "add new.yml"

    assert git_sync.commit_applied(repo, repo.tree_of(head), head, message) == head  # nothing new
    assert asked == []
    said: list[str] = []
    made = git_sync.commit_applied(repo, tree, head, message, said.append)
    assert said == ["All nodes successfully applied. Committing changes."]
    assert repo.head() == head  # a commit, no branch moved yet
    assert repo.advance(made, head)
    assert git(root, "log", "-1", "--format=%s") == "add new.yml"
    assert repo.subjects(f"{made}^!") == ["add new.yml"]  # what the committed line shows
    assert git(root, "status", "--porcelain") == ""  # the index follows; the file is committed


def test_what_became_of_the_commit_is_returned_and_worded(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = ConfigRepo.open(root)
    head = repo.head()
    (root / "new.yml").write_text("x\n")
    made = git_sync.commit_applied(repo, repo.snapshot(), head, lambda: "add new.yml\n\nbody")
    advanced = repo.advance(made, head)

    published = git_sync.publish_applied(repo, made, "20261112-120000", [], advanced, True)
    assert published == git_sync.Published(made, committed=True, subject="add new.yml")
    render.render_applied(rollout.Applied(published, "main"), root)
    assert f'Committed {made[:7]} "add new.yml"; no remote' in capsys.readouterr().out

    # Nothing changed since: the commit planned on, and the branch moved meanwhile
    published = git_sync.publish_applied(repo, made, "20261112-120001", [], False, False)
    assert not published.committed and published.subject == ""
    render.render_applied(rollout.Applied(published, "main", advanced=False), root)
    output = capsys.readouterr().out
    assert "main moved during the apply" in output and f"Nothing to commit ({made[:7]})" in output


def test_remote_storage_newly_shared_between_nodes_is_pointed_out() -> None:
    nas = (("media", "nfs 10.0.0.5:/export"),)
    plex, tautulli = Service("plex", remote=nas), Service("tautulli", remote=nas)
    before = {"frodo": NodeState.of("frodo", plex), "bilbo": NodeState.of("bilbo")}
    after = {"frodo": NodeState.of("frodo", plex), "bilbo": NodeState.of("bilbo", tautulli)}
    (note,) = planning.remote_sharing(after, before)
    assert note.startswith("nfs 10.0.0.5:/export is mounted on 2 nodes: bilbo (tautulli); frodo")
    # Already shared before this plan: nothing new to say.
    assert planning.remote_sharing(after, after) == []


@pytest.fixture
def behind(root: Path, tmp_path: Path) -> Path:
    """The lab config one commit behind its upstream (someone else pushed)."""
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    git(root, "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(root, "push", "-q", "-u", "origin", "main")
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "other")
    other = tmp_path / "other"
    for key, value in (("user.name", "Other"), ("user.email", "o@example.com")):
        git(other, "config", key, value)
    (other / "theirs.yml").write_text("t\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q")
    return root


def test_ff_fast_forwards_before_planning(behind: Path, planned: list[str | None]) -> None:
    code, output = run("plan", "cirdan", "--ff")
    assert "fast-forwarded main to origin/main (1 commit)" in output
    assert git(behind, "log", "-1", "--format=%s") == "theirs"


def test_behind_without_ff_plan_goes_on_and_apply_stops(
    behind: Path, planned: list[str | None]
) -> None:
    code, output = run("plan", "cirdan")  # no terminal: as it is, with the notice
    assert planned == [None] and git(behind, "log", "-1", "--format=%s") == "first"
    code, output = run("apply", "cirdan", "--yes")  # --yes does not fast-forward
    assert code == 1 and planned == [None]
    assert "1 commit behind origin/main" in output and "--ff" in output
    assert f"git -C {behind} pull --ff-only" in output


def test_in_a_terminal_the_fast_forward_is_offered(
    behind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ConfigRepo.open(behind)
    repo.fetch()
    asked: list[tuple[str, str]] = []

    def answer(reply: bool) -> Callable[[str, str], bool]:
        return lambda question, commits: asked.append((question, commits)) or reply

    # plan as it is: goes on behind, asked first
    assert git_sync.catch_up(repo, False, applying=True, ask=answer(False)) == CaughtUp()
    assert git(behind, "log", "-1", "--format=%s") == "first"
    assert asked == [("main is 1 commit behind origin/main. Fast-forward first?", "theirs")]
    done = git_sync.catch_up(repo, False, applying=True, ask=answer(True))
    assert done.forwarded == "fast-forwarded main to origin/main (1 commit)"
    assert git(behind, "log", "-1", "--format=%s") == "theirs"


#############################################################################
# Which nodes, and plan's exit code (as tofu's)
#############################################################################


@pytest.fixture
def scopes(monkeypatch: pytest.MonkeyPatch) -> list[Scope]:
    """The scope each collect was asked for; collect itself stops the command there."""
    found: list[Scope] = []

    def stop(*args: Any, **kwargs: Any) -> Iterator[None]:
        found.append(args[1])
        raise RepoError("stopped here")

    monkeypatch.setattr(plan_module, "collect", stop)
    return found


def test_plan_with_no_node_named_plans_all(root: Path, scopes: list[Scope]) -> None:
    run("plan")
    run("plan", "all")
    assert scopes == [Scope(), Scope()]


def test_apply_still_needs_its_nodes_named(root: Path, scopes: list[Scope]) -> None:
    code, output = run("apply")
    assert code == 1 and "Name the nodes, or 'all'" in output and scopes == []


@pytest.mark.parametrize(
    ("works", "reasons", "flag", "expected"),
    [
        ((), [], True, 0),  # nothing to change
        (("work",), [], True, 2),  # something to change
        (("work",), [], False, 0),  # without the flag, changes exit 0 as before
        (("work",), ["problems above"], True, 1),  # blocked
    ],
)
def test_detailed_exitcode(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    works: tuple[Any, ...],
    reasons: list[str],
    flag: bool,
    expected: int,
) -> None:
    @contextmanager
    def collected(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield None

    planned = plan_module.Planned(reasons=tuple(reasons), works=works)
    monkeypatch.setattr(plan_module, "collect", collected)
    monkeypatch.setattr(plan_module, "_plan_and_render", lambda *args, **kwargs: planned)
    code, output = run("plan", *(["--detailed-exitcode"] if flag else []))
    assert code == expected, output
