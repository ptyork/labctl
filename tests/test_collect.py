from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import MakeRepo, git, needs_docker

from labctl import collect as collect_module
from labctl.bundle import Desired
from labctl.collect import (
    _READ_NODE,
    MARK,
    NOT_NAMED_TIMEOUT,
    UNREAD,
    Collected,
    NodeRead,
    NodeView,
    OnNode,
    Record,
    _collect_baseline,
    _collect_remote,
    _read_all,
    collect,
    current_state,
    data_names,
    drift_changes,
    parse_node_output,
)
from labctl.compose import ComposeError, reduce_stacks
from labctl.envfile import CompiledEnv
from labctl.remote import CONNECT_TIMEOUT, Remote, RemoteError, Unreachable
from labctl.repo import TagInfo
from labctl.runtime import Container
from labctl.settings import Secrets, Settings
from labctl.state import Dirs, NodeState, Scope, Service, Stack

HOME = "/home/paul/docker"
APPDATA = "/home/paul/docker/app_data"
RECORD_DOC = Record(
    "2026-10-30T12:00:00",
    Dirs(HOME, APPDATA),
    (Stack("homelab-cirdan", ("docker-compose.yml",)),),
    {"docker-compose.yml": "aaa111", "apps/plex.yml": "bbb222", ".env": "eee555"},
    node="cirdan",
)


def only(result: Collected) -> NodeView:
    """The record of the one node collected."""
    (view,) = result.nodes.values()
    return view


def node_output(applied: Record | None = None, projects: list[dict[str, str]] | None = None) -> str:
    lines = [
        f"{MARK} pointer",
        HOME,
        f"{MARK} applied",
        json.dumps(applied.to_json()) if applied else "",
        f"{MARK} names",
        "file docker-compose.yml",
        "dir apps",
        "file stack.yml",
        "data old_config",
        f"{MARK} files",
        "aaa111  docker-compose.yml",
        "bbb222  apps/plex.yml",
        "ccc333  apps/with space.yml",
        f"{UNREAD} stack.yml",
        f"{MARK} projects",
        json.dumps(projects or []),
    ]
    return "\n".join(lines) + "\n"


def test_parse_node_without_deploy_dir() -> None:
    on_node = parse_node_output(f"{MARK} pointer\n/old/home\n{MARK} nohome\n", HOME)
    assert (on_node.exists, on_node.pointer) == (False, "/old/home")


def test_parse_node_output() -> None:
    projects = [
        {
            "Name": "homelab-cirdan",
            "Status": "running(4)",
            "ConfigFiles": f"{HOME}/a.yml,{HOME}/b.yml",
        },
        {"Name": "elsewhere", "Status": "running(1)", "ConfigFiles": "/opt/other/compose.yml"},
    ]
    on_node = parse_node_output(node_output(RECORD_DOC, projects), HOME)
    assert on_node == OnNode(
        RECORD_DOC,
        {
            "docker-compose.yml": "aaa111",
            "apps/plex.yml": "bbb222",
            "apps/with space.yml": "ccc333",
            "stack.yml": UNREAD,
        },
        {"homelab-cirdan": ("a.yml", "b.yml")},
        pointer=HOME,
        names={
            "docker-compose.yml": "file",
            "apps": "dir",
            "stack.yml": "file",
            "old_config": "data",
        },
    )


def test_the_read_script_lists_everything_but_data(tmp_path: Path) -> None:
    """The script itself, run here on a folder laid out like a DEPLOY_DIR."""
    home = tmp_path / "docker"
    for path, text in {
        "docker-compose.yml": "name: x\n",
        "apps/plex.yml": "services: {}\n",
        "stack.yml": "name: old\n",
        "old/notes.txt": "hi\n",
        "app_data/plex/db": "data\n",
        ".labctl/applied.json": "",
        ".labctl/managed": "docker-compose.yml\n",
    }.items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)
    (home / "empty").mkdir()
    env = {**os.environ, "HOME": str(tmp_path)}
    output = subprocess.run(
        ["sh", "-c", _READ_NODE, "sh", str(home), "apps", "--", "app_data"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    on_node = parse_node_output(output, str(home))
    assert on_node.names == {
        "apps": "dir",
        "docker-compose.yml": "file",
        "empty": "dir",
        "old": "dir",
        "stack.yml": "file",
    }
    digest = hashlib.sha256(b"name: x\n").hexdigest()
    assert on_node.files == {
        "apps/plex.yml": hashlib.sha256(b"services: {}\n").hexdigest(),
        "docker-compose.yml": digest,  # in .labctl/managed
        "old/notes.txt": UNREAD,
        "stack.yml": UNREAD,
    }


def test_a_service_in_two_stacks_belongs_to_the_one_that_runs() -> None:
    def config(name: str) -> tuple[Stack, dict[str, Any]]:
        return Stack(name, (f"{name}.yml",)), {"services": {"dozzle": {"image": "dozzle"}}}

    resolved = [config("homelab-cirdan"), config("stack1")]
    state = reduce_stacks("cirdan", resolved, APPDATA, running={"stack1"})
    assert state.services["dozzle"].stack == "stack1"
    assert set(state.stacks) == {"homelab-cirdan", "stack1"}
    state = reduce_stacks("cirdan", resolved, APPDATA, running={"homelab-cirdan"})
    assert state.services["dozzle"].stack == "homelab-cirdan"
    for running in (set(), {"homelab-cirdan", "stack1"}, None):
        with pytest.raises(ComposeError, match="is in two stacks"):
            reduce_stacks("cirdan", resolved, APPDATA, running=running)


def test_record_round_trip() -> None:
    assert Record.from_json(json.loads(json.dumps(RECORD_DOC.to_json()))) == RECORD_DOC
    tracked = replace(RECORD_DOC, commit="abc1234", tag="applied/cirdan/20261030-120000")
    assert Record.from_json(json.loads(json.dumps(tracked.to_json()))) == tracked
    with pytest.raises(ComposeError, match="unsupported .* version 1"):
        Record.from_json({**RECORD_DOC.to_json(), "version": 1})


def test_drift_is_edits_to_the_files_apply_wrote() -> None:
    """Anything else in DEPLOY_DIR (a stack started by hand) is not drift: it is part of the
    node's current state, and the plan shows what apply does with it."""
    on_node = OnNode(
        RECORD_DOC,
        {
            "docker-compose.yml": "aaa111",
            "apps/plex.yml": "edited",
            "apps/new.yml": "fff666",
            "handmade.yml": UNREAD,
        },
        {"homelab-cirdan": ("docker-compose.yml",), "handmade": ("handmade.yml",)},
    )
    assert drift_changes(RECORD_DOC, on_node) == (
        ".env: removed on node",
        "apps/new.yml: added on node",
        "apps/plex.yml: changed on node",
    )
    clean = OnNode(RECORD_DOC, dict(RECORD_DOC.manifest), {"homelab-cirdan": ("x",)})
    assert drift_changes(RECORD_DOC, clean) == ()


def desired_for(bundle: Path, home: str = HOME, files: dict[str, str] | None = None) -> Desired:
    env = CompiledEnv(values={"DEPLOY_DIR": home, "APPDATA_DIR": APPDATA})
    state = NodeState.of("cirdan", stacks=[Stack("homelab-cirdan", ("docker-compose.yml",))])
    return Desired("cirdan", bundle, env, {}, state, files or {})


@pytest.fixture
def from_node(monkeypatch: pytest.MonkeyPatch) -> NodeState:
    """The current state, as if downloaded from the node and resolved."""
    recorded = NodeState.of("cirdan", Service("plex"))
    monkeypatch.setattr(collect_module, "fetch_stack_files", lambda *args: None)
    monkeypatch.setattr(collect_module, "current_state", lambda *args: recorded)
    return recorded


def test_the_node_is_the_record(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, from_node: NodeState
) -> None:
    on_node = OnNode(RECORD_DOC, dict(RECORD_DOC.manifest))
    monkeypatch.setattr(collect_module, "read_node", lambda r, h, n, d: on_node)
    desired = desired_for(Path("/unused"))

    result = Collected.of({"cirdan": desired})
    _collect_remote(result, settings, "cirdan", desired, tmp_path)
    assert result.recorded == {"cirdan": from_node}
    assert only(result).detail == "applied 2026-10-30T12:00:00"
    assert not only(result).drift and not only(result).untracked

    on_node = OnNode(RECORD_DOC, {**RECORD_DOC.manifest, "apps/plex.yml": "edited"})
    result = Collected.of({"cirdan": desired})
    _collect_remote(result, settings, "cirdan", desired, tmp_path / "again")
    assert only(result).drift


def test_changed_deploy_dir_is_found_through_the_node_pointer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, from_node: NodeState
) -> None:
    """The repo moved DEPLOY_DIR; the node still has its files (and applied.json) at the old
    path, which its ~/.labctl-home says."""
    new = "/srv/docker"
    reads: list[str] = []

    def read_node(remote: object, home: str, names: object, data: object) -> OnNode:
        reads.append(home)
        if home == HOME:
            return OnNode(RECORD_DOC, dict(RECORD_DOC.manifest), pointer=HOME)
        return OnNode(None, {}, exists=False, pointer=HOME)

    monkeypatch.setattr(collect_module, "read_node", read_node)
    desired = desired_for(Path("/unused"), home=new)

    result = Collected.of({"cirdan": desired})
    _collect_remote(result, settings, "cirdan", desired, tmp_path)
    assert reads == [new, HOME]
    assert result.recorded == {"cirdan": from_node}
    assert result.recorded_dirs["cirdan"] == Dirs(HOME, APPDATA)
    assert "at the old DEPLOY_DIR /home/paul/docker" in only(result).detail


def test_the_pointer_is_not_followed_to_another_nodes_record(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, from_node: NodeState
) -> None:
    """Two nodes on one host share the ssh user's ~/.labctl-home. A new node (ghost) whose
    DEPLOY_DIR does not exist yet must not take palantir's DEPLOY_DIR for its own old one (and
    plan to move it, taking palantir's stacks down)."""
    palantirs = replace(RECORD_DOC, tag="applied/palantir/20261103-212146")

    def read_node(remote: object, home: str, names: object, data: object) -> OnNode:
        if home == HOME:
            return OnNode(palantirs, dict(RECORD_DOC.manifest), pointer=HOME)
        return OnNode(None, {}, exists=False, pointer=HOME)

    monkeypatch.setattr(collect_module, "read_node", read_node)
    desired = replace(desired_for(Path("/unused"), home="/home/paul/docker-ghost"), node="ghost")
    result = Collected.of({"ghost": desired})
    _collect_remote(result, settings, "ghost", desired, tmp_path)
    assert result.recorded["ghost"] == NodeState.of("ghost")
    assert result.recorded_dirs["ghost"] == desired.dirs  # no move
    assert only(result).source == "first"

    # Its own record is.
    palantirs = replace(palantirs, node="ghost")
    result = Collected.of({"ghost": desired})
    _collect_remote(result, settings, "ghost", desired, tmp_path / "own")
    assert result.recorded_dirs["ghost"] == Dirs(HOME, APPDATA)


def test_never_applied_is_what_is_there_now(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, from_node: NodeState
) -> None:
    """No applied.json: the node is planned against what its DEPLOY_DIR has, nothing when it
    does not exist; a first apply is an ordinary apply."""
    monkeypatch.setattr(collect_module, "read_node", lambda r, h, n, d: OnNode(None, {}, {}))
    desired = desired_for(Path("/unused"))
    result = Collected.of({"cirdan": desired})
    _collect_remote(result, settings, "cirdan", desired, tmp_path)
    assert result.recorded["cirdan"] == from_node
    assert only(result).source == "first"
    assert "1 services in /home/paul/docker" in only(result).detail

    absent = OnNode(None, {}, exists=False)
    monkeypatch.setattr(collect_module, "read_node", lambda r, h, n, d: absent)
    result = Collected.of({"cirdan": desired})
    _collect_remote(result, settings, "cirdan", desired, tmp_path / "again")
    assert result.recorded["cirdan"] == NodeState.of("cirdan")
    assert only(result).source == "first"


def test_an_unreachable_named_node_is_an_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, make_repo: MakeRepo
) -> None:
    repo = make_repo(
        settings.lab_config,
        {f"nodes/{node}/docker-compose.yml": "services: {}\n" for node in ("cirdan", "frodo")},
    )
    on_node = OnNode(RECORD_DOC, dict(RECORD_DOC.manifest))

    def read_node(remote: Remote, home: str, names: object, data: object) -> OnNode:
        if remote.host == "frodo":
            raise Unreachable("frodo: ssh: connect to host frodo port 22: No route to host")
        return on_node

    monkeypatch.setattr(collect_module, "resolve_desired", lambda s, node, *a: desired_for(Path()))
    monkeypatch.setattr(collect_module, "read_node", read_node)
    monkeypatch.setattr(collect_module, "fetch_stack_files", lambda *args: None)
    monkeypatch.setattr(collect_module, "current_state", lambda *a: NodeState.of("cirdan"))

    with collect(settings, Scope(), Secrets(), None, repo) as result:
        assert result.errors == [
            "frodo is unreachable (ssh: connect to host frodo port 22: No route to host)"
        ]


TRACKED = replace(RECORD_DOC, tag="applied/cirdan/20261030-120000")


def test_a_missing_apply_tag_is_recreated_from_the_record(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    from_node: NodeState,
    make_repo: MakeRepo,
) -> None:
    repo = make_repo(settings.lab_config, {"nodes/cirdan/docker-compose.yml": "services: {}\n"})
    head = repo.head()
    on_node = OnNode(
        replace(TRACKED, commit=head),
        dict(RECORD_DOC.manifest),
        {"stack1": ("a",), "stack2": ("b",)},  # stack2's containers are stopped
        containers=(
            Container("s1", "running", project="stack1"),
            Container("s2", "exited", project="stack2"),
        ),
    )
    monkeypatch.setattr(collect_module, "read_node", lambda r, h, n, d: on_node)
    desired = desired_for(Path("/unused"))
    result = Collected.of({"cirdan": desired})
    _collect_remote(result, settings, "cirdan", desired, tmp_path, repo=repo, base=head)
    assert only(result).detail == f"applied 2026-10-30T12:00:00 from {head[:7]}"
    assert result.repo_notices == [] and not only(result).untracked
    latest = repo.last_applied("cirdan")
    assert latest is not None and (latest.tag, latest.commit) == (TRACKED.tag, head)
    assert latest.info is not None and latest.info.stacks_up == ("stack1",)
    assert latest.info.dirs == RECORD_DOC.dirs


def test_the_nodes_commit_is_checked_against_the_repo(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    from_node: NodeState,
    make_repo: MakeRepo,
) -> None:
    repo = make_repo(settings.lab_config, {"nodes/cirdan/docker-compose.yml": "services: {}\n"})
    first = repo.head()
    (settings.lab_config / "apps.yml").write_text("services: {}\n")
    second = repo.commit("add apps")
    git(settings.lab_config, "reset", "-q", "--hard", first)
    desired = desired_for(Path("/unused"))

    def notices(commit: str) -> list[str]:
        on_node = OnNode(replace(TRACKED, commit=commit), dict(RECORD_DOC.manifest))
        monkeypatch.setattr(collect_module, "read_node", lambda r, h, n, d: on_node)
        result = Collected.of({"cirdan": desired})
        _collect_remote(
            result, settings, "cirdan", desired, tmp_path / commit, repo=repo, base=first
        )
        untracked[:] = [node for node, view in result.nodes.items() if view.untracked]
        return result.repo_notices

    untracked: list[str] = []

    # Applied from a commit this branch does not have: applying reverts it.
    assert notices(second) == [
        f"cirdan was applied from {second[:7]}, which {first[:7]} does not contain; applying it "
        "undoes: add apps"
    ]
    # Applied from a commit this clone has never seen.
    assert notices("0" * 40) == [
        "cirdan was applied from 0000000, which this clone does not have (git fetch, or pull); "
        "planned against the node as it is, and apply records the commit planned"
    ]
    assert untracked == ["cirdan"]  # recorded again, even with nothing to change


def test_a_node_not_named_is_planned_as_of_its_apply_tag(
    settings: Settings, tmp_path: Path, make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(settings.lab_config, {"nodes/frodo/docker-compose.yml": "services: {}\n"})
    applied_at = repo.head()
    (settings.lab_config / "later.txt").write_text("x\n")
    repo.commit("later")
    info = TagInfo(
        "2026-11-01T10:00:00", "me@here", "0.1.0", ("stack1",), Dirs(HOME, APPDATA), "10.0.0.7"
    )
    repo.tag_applied("frodo", "20261101-100000", applied_at, info)
    built: list[tuple[str, Path | None, bool]] = []
    today = Secrets(global_={"API_KEY": "s3cret"})

    def resolve(
        s: Settings,
        node: str,
        secrets: Secrets,
        work: Path,
        prompt: object,
        tree: Path,
        baseline: bool,
        address: str,
    ) -> Desired:
        built.append((node, tree, baseline))
        # Today's secrets (the repo has none), the address the tag recorded, no questions.
        assert secrets is today and address == "10.0.0.7" and prompt is None
        return desired_for(tree)

    monkeypatch.setattr(collect_module, "resolve_desired", resolve)
    monkeypatch.setattr(collect_module, "reduce_stacks", lambda n, r, a, running: running)
    result = Collected.of({"frodo": desired_for(Path("/unused"))})
    result.base = repo.head()
    _collect_baseline(result, settings, "frodo", repo, tmp_path, "not named", today)
    assert result.errors == []
    assert result.recorded["frodo"] == ("stack1",)  # running: the tag's Stacks-Up
    assert result.recorded_dirs["frodo"] == Dirs(HOME, APPDATA)
    ((node, tree, baseline),) = built
    assert baseline and tree is not None
    assert (tree / "nodes/frodo/docker-compose.yml").is_file() and not (tree / "later.txt").exists()
    assert only(result).source == "tag"
    assert only(result).detail == (
        f"not named; as of applied/frodo/20261101-100000 ({applied_at[:7]}, 1 commit behind); "
        "drift not checked"
    )


def test_data_names_come_from_the_repo(tmp_path: Path) -> None:
    env = CompiledEnv(
        values={
            "DEPLOY_DIR": HOME,
            "APPDATA_DIR": APPDATA,
            "LOGS_DIR": f"{HOME}/logs",
            "TEMP_DIR": "/tmp",
            "TZ": "Europe/London",
        }
    )
    configs = {
        "homelab-cirdan": {
            "services": {
                "proxy": {
                    "volumes": [
                        {"type": "bind", "source": f"{HOME}/certs/live"},
                        {"type": "bind", "source": f"{HOME}/apps/proxy.conf"},  # the bundle's
                        {"type": "volume", "source": "cache"},
                    ]
                }
            }
        }
    }
    desired = Desired("cirdan", tmp_path, env, configs, NodeState.of("cirdan"), {"apps/a.yml": "x"})
    assert data_names(desired) == ("app_data", "certs", "logs")


# ── Reading stacks from a node's files (real compose) ─────────────────────────


def write_tree(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


APPS = {
    "apps/dozzle.yml": "services:\n  dozzle:\n    image: amir20/dozzle\n",
    "apps/fileflows.yml": "services:\n  fileflows:\n    image: revenz/fileflows\n",
}
STACK1 = "name: homelab-cirdan-stack1\ninclude:\n  - apps/dozzle.yml\n"
STACK2 = "name: homelab-cirdan-stack2\ninclude:\n  - apps/fileflows.yml\n"


@needs_docker
def test_every_stack_file_in_deploy_dir_is_a_stack(settings: Settings, tmp_path: Path) -> None:
    """Stacks started by hand, before labctl: each file is a stack, running or not."""
    folder = tmp_path / "docker"
    write_tree(folder, {**APPS, "stack.yml": STACK1, "stack2.yml": STACK2, "notes.txt": "x"})
    state = current_state(
        settings,
        "cirdan",
        folder,
        Dirs(HOME, APPDATA),
        {"homelab-cirdan-stack1": ("stack.yml",)},
    )
    assert {name: s.stack for name, s in state.services.items()} == {
        "dozzle": "homelab-cirdan-stack1",
        "fileflows": "homelab-cirdan-stack2",
    }
    assert set(state.stacks) == {"homelab-cirdan-stack1", "homelab-cirdan-stack2"}


@needs_docker
def test_a_running_stack_without_its_file_is_left_out(settings: Settings, tmp_path: Path) -> None:
    """It is trash: NodeView.gone lists it, and apply takes it down by name."""
    folder = tmp_path / "docker"
    write_tree(folder, APPS)
    state = current_state(settings, "cirdan", folder, Dirs(HOME, APPDATA), {"x": ("gone.yml",)})
    assert "x" not in state.stacks


##############################################################################


# Nodes not named (B5)


##############################################################################


def test_a_node_not_named_is_read_but_only_looked_at(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, from_node: NodeState
) -> None:
    """Its state is what runs there; its lock and its edits do not stop or concern the plan."""
    edited = {**RECORD_DOC.manifest, "apps/plex.yml": "edited"}
    on_node = OnNode(RECORD_DOC, edited, lock=("labctl apply 2026-11-11",))
    monkeypatch.setattr(collect_module, "read_node", lambda r, h, n, d: on_node)
    desired = desired_for(Path("/unused"))

    result = Collected.of({"cirdan": desired})
    why = _collect_remote(result, settings, "cirdan", desired, tmp_path, named=False)
    assert why == "" and result.errors == []
    assert result.recorded == {"cirdan": from_node}
    assert not only(result).drift and not only(result).untracked
    assert only(result).detail == "not named; applied 2026-10-30T12:00:00"


def test_a_node_not_named_that_cannot_be_read_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    def broken(remote: Remote, *args: object) -> OnNode:
        raise RemoteError("cirdan: remote script failed (1): oops")

    monkeypatch.setattr(collect_module, "read_node", broken)
    desired = desired_for(Path("/unused"))
    result = Collected.of({"cirdan": desired})
    why = _collect_remote(result, settings, "cirdan", desired, tmp_path, named=False)
    assert why == "cirdan: remote script failed (1): oops"
    assert result.errors == [] and only(result).recorded is None


def test_nodes_not_named_fall_back_to_their_tag_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, make_repo: MakeRepo
) -> None:
    repo = make_repo(
        settings.lab_config,
        {f"nodes/{node}/docker-compose.yml": "services: {}\n" for node in ("cirdan", "frodo")},
    )
    on_node = OnNode(RECORD_DOC, dict(RECORD_DOC.manifest))

    def read_node(remote: Remote, home: str, names: object, data: object) -> OnNode:
        if remote.host == "frodo":
            raise Unreachable("frodo: ssh: connect to host frodo port 22: Connection timed out")
        return on_node

    monkeypatch.setattr(collect_module, "resolve_desired", lambda s, node, *a: desired_for(Path()))
    monkeypatch.setattr(collect_module, "read_node", read_node)
    monkeypatch.setattr(collect_module, "fetch_stack_files", lambda *args: None)
    monkeypatch.setattr(collect_module, "current_state", lambda *a: NodeState.of("cirdan"))

    with collect(settings, Scope(frozenset({"frodo"})), Secrets(), None, repo) as result:
        assert result.errors == []
        assert result.nodes["frodo"].unread == "unreachable: Connection timed out"
        frodo = result.nodes["frodo"]
        assert (frodo.source, frodo.detail) == ("none", "not named, not read; no apply tag")


def test_nodes_are_read_together_and_those_not_named_get_less_time(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    timeouts: dict[str, int] = {}

    def read(s: Settings, node: str, desired: object, work: Path, timeout: int) -> NodeRead:
        timeouts[node] = timeout
        return NodeRead(node, unreachable="down")

    monkeypatch.setattr(collect_module, "read_remote", read)
    desired: Any = {"cirdan": None, "frodo": None}
    reads = _read_all(settings, ["cirdan", "frodo"], desired, Path(), Scope(frozenset({"frodo"})))
    assert set(reads) == {"cirdan", "frodo"}
    assert timeouts == {"cirdan": CONNECT_TIMEOUT, "frodo": NOT_NAMED_TIMEOUT}


def test_ssh_waits_as_long_as_the_remote_says() -> None:
    assert "ConnectTimeout=2" in Remote("frodo", connect_timeout=2).argv("true")
    assert f"ConnectTimeout={CONNECT_TIMEOUT}" in Remote("frodo").argv("true")


def test_nothing_there_for_a_node_being_retired() -> None:
    from labctl.collect import NodeRead, _nothing_there

    assert _nothing_there(NodeRead("x"))
    assert _nothing_there(NodeRead("x", OnNode(None, {}, exists=False)))
    data_only = OnNode(None, {}, names={"app_data": "data"})
    assert _nothing_there(NodeRead("x", data_only, state=NodeState.of("x")))
    files = OnNode(None, {}, names={"docker-compose.yml": "file", "app_data": "data"})
    assert not _nothing_there(NodeRead("x", files, state=NodeState.of("x")))
    running = NodeState.of("x", Service("plex"))
    assert not _nothing_there(NodeRead("x", data_only, state=running))
