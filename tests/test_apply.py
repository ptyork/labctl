from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from labctl.apply import ApplyError, Executor, Node, NodeWork, RollbackFailed, node_work
from labctl.bundle import Desired, Location, manifest, resolve_stacks
from labctl.collect import HOME_POINTER, RECORD, Collected, NodeReport, Record, current_state
from labctl.envfile import CompiledEnv
from labctl.moves import DATA, VOLUME, Move
from labctl.planner import Migrate, Plan, Restore, Update, make_plan
from labctl.remote import Remote
from labctl.runtime import Container, NodeRuntime, Stop, TakeDown, analyse
from labctl.settings import Settings
from labctl.stack import Seen, Snapshot
from labctl.state import Dirs, NodeState, Scope, Service, Stack, Volume

HOME, APPDATA = "/srv/docker", "/srv/appdata"
OLD = Stack("docker", ("docker-compose.yml",))
NEW = Stack("homelab-cirdan", ("docker-compose.yml",))


class FakeNode:
    """Records what apply runs; ``fail`` names a step (a substring of it) that fails."""

    def __init__(
        self, fail: str = "", undo_fails: bool = False, says: tuple[str, ...] = ()
    ) -> None:
        self.calls: list[str] = []
        self.says = says  # what compose up prints
        self.fail = fail
        self.undo_fails = undo_fails
        self.rolling_back = False
        self.volumes: dict[str, bool] = {}
        self.users: dict[str, list[str]] = {}
        self.sidecars: dict[str, dict[str, object]] = {}

    @property
    def name(self) -> str:
        remote: Remote | None = getattr(self, "remote", None)
        return remote.host if remote else "cirdan"

    def _call(self, text: str) -> None:
        self.calls.append(text)
        if self.fail and self.fail in text and not self.rolling_back:
            self.rolling_back = True
            raise ApplyError(f"failed: {text}")
        if self.rolling_back and self.undo_fails:
            raise ApplyError(f"undo failed: {text}")

    def run(self, *args: str, cwd: str = "/") -> str:
        if "ps" in args:
            return "\n".join(
                json.dumps({"Service": name, "State": "running", "Health": ""})
                for name in ("plex", "heimdall", "dozzle", "fileflows", "pg")
            )
        self._call(" ".join(args))
        return ""

    snapshots: list[Snapshot] = []  # what snapshot() returns, in turn (then the last again)

    def snapshot(self) -> Snapshot:
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0] if self.snapshots else {}

    def script(self, script: str, *args: str) -> None:
        if HOME_POINTER in script:
            name = "pointer"
        else:
            name = "swap-out" if "previous/*" in script else "swap-in"
        self._call(f"{name} {' '.join(args)}")

    def compose(self, home: str, stack: Stack, *args: str, watch: Any = None) -> None:
        self._call(f"compose {home} {stack.name} {' '.join(args)}")

    def upload(self, local: Path, dest: str) -> None:
        if (local / RECORD).is_file():
            self.record = Record.from_json(json.loads((local / RECORD).read_text()))
        self._call(f"upload {dest}")

    record: Record | None = None

    def exists(self, path: str) -> bool:
        return True

    def move(self, now: str, new: str) -> Callable[[], object]:
        self._call(f"move {now} {new}")
        return partial(self.move, new, now)

    def delete_moved_away(self) -> list[str]:
        return []

    def helper(self, root: str, script: str, *args: str, read_only: bool = False) -> str:
        if "printf" in script:  # an archive entry's sidecar
            self.sidecars[args[0]] = json.loads(args[1])
            self._call(f"sidecar {args[0]}")
            return ""
        self._call(f"helper {root} {' '.join(args)}")
        return ""

    # Volumes: name -> has data.
    def volume_exists(self, name: str) -> bool:
        return name in self.volumes

    def has_data(self, spec: str) -> bool:
        return self.volumes.get(spec, True)

    def volume_labels(self, name: str) -> dict[str, str]:
        return {"com.docker.compose.project": "docker"}

    def volume_create(self, name: str, labels: dict[str, str]) -> None:
        self._call(f"volume create {name} {sorted(labels.items())}")
        self.volumes[name] = False

    def volume_remove(self, name: str) -> None:
        self._call(f"volume rm {name}")
        self.volumes.pop(name, None)

    def volume_users(self, name: str) -> list[str]:
        return self.users.get(name, [])

    def copy(self, source: str, target: str) -> None:
        self._call(f"copy {source} {target}")

    def sums(self, spec: str) -> str:
        self._call(f"sums {spec}")
        return self.sums_of.get(spec, "abc  ./x\n")

    def data_state(self, appdata: str, key: str) -> str:
        return self.states.get(key, "missing")

    def checksums(self, root: str, paths: list[str]) -> str:
        self._call(f"checksums {root} {' '.join(paths)}")
        return self.checksum_text

    def log(self, step: str) -> None:
        pass

    states: dict[str, str] = {}
    checksum_text = "abc  heimdall/config.ini\n"
    sums_of: dict[str, str] = {}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(lab_config=tmp_path / "repo", config_dir=tmp_path / "config")


UP = NodeRuntime(start=frozenset({NEW.name}), running=frozenset({OLD.name}))


def work(
    tmp_path: Path,
    moves: tuple[tuple[Move, str, str], ...] = (),
    node: str = "cirdan",
    services: tuple[str, ...] = ("plex",),
    archive: tuple[str, ...] = ("sonarr",),
    runtime: NodeRuntime = UP,
    volumes: tuple[Volume, ...] = (),
    reason: str = "removed",
    old_services: tuple[Service, ...] = (),
) -> NodeWork:
    """The old stack is up; the repo's stack runs after apply."""
    bundle = tmp_path / f"bundle-{node}"
    bundle.mkdir(exist_ok=True)
    (bundle / "docker-compose.yml").write_text("name: homelab-cirdan\n")
    env = CompiledEnv(values={"DEPLOY_DIR": HOME, "APPDATA_DIR": APPDATA})
    state = NodeState.of(
        node, *(Service(s, stack=NEW.name) for s in services), stacks=[NEW], volumes=volumes
    )
    desired = Desired(node, bundle, env, {}, state, {"docker-compose.yml": "x"})
    return NodeWork(
        node=node,
        desired=desired,
        old_home=HOME,
        old_appdata=APPDATA,
        old_stacks=(OLD,),
        old_projects=frozenset({OLD.name}),
        old_stack_of={"heimdall": OLD.name, **{s.name: s.stack for s in old_services}},
        moves=moves,
        archive=tuple((key, reason) for key in archive),
        replace=("apps", "docker-compose.yml"),
        discard=(),
        keep_drift=False,
        runtime=runtime,
        old_services={s.name: s for s in old_services},
    )


def run(settings: Settings, node: FakeNode, *works: NodeWork, plan: Plan | None = None) -> Executor:
    executor = Executor(
        settings, lambda text: None, wait=5, when=datetime(2026, 10, 31, 9, 0), commit="c0ffee1"
    )
    nodes = {w.node: node for w in works}
    executor.run(nodes, list(works), plan or Plan())  # type: ignore[arg-type]
    return executor


def test_steps_in_order_and_the_record(settings: Settings, tmp_path: Path) -> None:
    node = FakeNode()
    executor = run(settings, node, work(tmp_path))
    assert node.calls == [
        f"compose {HOME} docker down",  # the old stack goes away
        f"mkdir -p {HOME}/.labctl",
        f"rm -rf {HOME}/.labctl/incoming",
        f"upload {HOME}/.labctl/incoming",
        f"swap-in {HOME} apps docker-compose.yml",
        f"compose {HOME} homelab-cirdan up -d --remove-orphans",
        f"move {APPDATA}/sonarr {APPDATA}/.labctl/archive/sonarr-20261031-090000",
        "sidecar .labctl/archive/sonarr-20261031-090000.labctl.json",
        f"upload {HOME}",  # .labctl/applied.json and managed
        f"pointer {HOME}",  # ~/.labctl-home
    ]
    assert node.record is not None and node.record.stacks == (NEW,)
    assert (node.record.commit, node.record.tag) == ("c0ffee1", "applied/cirdan/20261031-090000")
    assert executor.recorded == ["cirdan"]


def test_record_only_writes_just_the_record(settings: Settings, tmp_path: Path) -> None:
    node = FakeNode()
    executor = run(settings, node, replace(work(tmp_path), record_only=True))
    assert node.calls == [f"upload {HOME}", f"pointer {HOME}"]
    assert executor.recorded == ["cirdan"]


def test_a_node_whose_record_has_no_commit_is_recorded_again(tmp_path: Path) -> None:
    """Nothing to change: no work, unless the record predates git tracking (then only the record
    is written, so the node gets its first apply tag)."""
    desired = work(tmp_path).desired
    collected = Collected(
        desired={"cirdan": desired},
        recorded={"cirdan": desired.state},
        reports=[NodeReport("cirdan", "node")],
        node_files={"cirdan": dict(desired.files)},
        node_names={"cirdan": {"docker-compose.yml": "file"}},
    )
    plan = make_plan({"cirdan": desired.state}, {"cirdan": desired.state})
    assert node_work(plan, collected, Scope()) == []
    collected.untracked.add("cirdan")
    (job,) = node_work(plan, collected, Scope())
    assert job.record_only


def test_a_failure_undoes_every_step_in_reverse(settings: Settings, tmp_path: Path) -> None:
    node = FakeNode(fail="homelab-cirdan up")
    with pytest.raises(ApplyError, match="failed: compose"):
        run(settings, node, work(tmp_path))
    undone = node.calls[
        node.calls.index(f"compose {HOME} homelab-cirdan up -d --remove-orphans") + 1 :
    ]
    assert undone == [
        f"compose {HOME} homelab-cirdan down",
        f"swap-out {HOME} docker-compose.yml",
        f"compose {HOME} docker up -d",
    ]
    assert node.record is None


def test_moves_stop_every_stack_and_are_undone(settings: Settings, tmp_path: Path) -> None:
    move = Move("cirdan", DATA, "plexms", "plex", f"{APPDATA}/plexms", f"{APPDATA}/plex", ("plex",))
    moves = ((move, f"{APPDATA}/plexms", f"{APPDATA}/plex"),)
    node = FakeNode(fail="swap-in")
    with pytest.raises(ApplyError):
        run(settings, node, work(tmp_path, moves))
    assert node.calls == [
        f"compose {HOME} docker down",
        f"move {APPDATA}/plexms {APPDATA}/plex",
        f"mkdir -p {HOME}/.labctl",
        f"rm -rf {HOME}/.labctl/incoming",
        f"upload {HOME}/.labctl/incoming",
        f"swap-in {HOME} apps docker-compose.yml",
        f"move {APPDATA}/plex {APPDATA}/plexms",  # undo, newest first
        f"compose {HOME} docker up -d",
    ]


def test_a_failed_rollback_is_reported(settings: Settings, tmp_path: Path) -> None:
    node = FakeNode(fail="homelab-cirdan up", undo_fails=True)
    with pytest.raises(RollbackFailed, match="the locks stay in place"):
        run(settings, node, work(tmp_path))


def test_a_stack_that_kept_running_comes_back_as_it_was(settings: Settings, tmp_path: Path) -> None:
    same = replace(
        work(tmp_path, runtime=replace(UP, running=frozenset({NEW.name}))),
        old_stacks=(NEW,),
        old_projects=frozenset({NEW.name}),
    )
    node = FakeNode(fail="homelab-cirdan up")
    with pytest.raises(ApplyError):
        run(settings, node, same)
    assert node.calls == [
        f"mkdir -p {HOME}/.labctl",
        f"rm -rf {HOME}/.labctl/incoming",
        f"upload {HOME}/.labctl/incoming",
        f"swap-in {HOME} apps docker-compose.yml",
        f"compose {HOME} homelab-cirdan up -d --remove-orphans",
        f"swap-out {HOME} docker-compose.yml",  # old files back, then the stack with them
        f"compose {HOME} homelab-cirdan up -d",
    ]


# ── What runs after apply ─────────────────────────────────────────────────────


def test_a_stack_that_was_down_gets_its_files_only(settings: Settings, tmp_path: Path) -> None:
    """Nothing was up: the old stack's stopped containers go (and come back stopped on undo), the
    repo's stack is checked, not started."""
    down = work(tmp_path, runtime=NodeRuntime())
    node = FakeNode(fail="pointer")
    with pytest.raises(ApplyError):
        run(settings, node, down)
    assert node.calls == [
        f"compose {HOME} docker down",
        f"mkdir -p {HOME}/.labctl",
        f"rm -rf {HOME}/.labctl/incoming",
        f"upload {HOME}/.labctl/incoming",
        f"swap-in {HOME} apps docker-compose.yml",
        f"compose {HOME} homelab-cirdan config --quiet",
        f"move {APPDATA}/sonarr {APPDATA}/.labctl/archive/sonarr-20261031-090000",
        "sidecar .labctl/archive/sonarr-20261031-090000.labctl.json",
        f"upload {HOME}",
        "pointer /srv/docker",
        # undo
        f"move {APPDATA}/.labctl/archive/sonarr-20261031-090000 {APPDATA}/sonarr",
        f"helper {APPDATA}/.labctl/archive sonarr-20261031-090000",  # its sidecar
        f"swap-out {HOME} docker-compose.yml",
        f"compose {HOME} docker up --no-start --remove-orphans",
        f"compose {HOME} docker stop",
    ]


def test_a_stopped_stack_that_apply_starts_is_stopped_again_on_undo(
    settings: Settings, tmp_path: Path
) -> None:
    started = replace(
        work(tmp_path, runtime=NodeRuntime(start=frozenset({NEW.name}))),
        old_stacks=(NEW,),
        old_projects=frozenset({NEW.name}),
    )
    node = FakeNode(fail="homelab-cirdan up -d")
    with pytest.raises(ApplyError):
        run(settings, node, started)
    assert node.calls[-3:] == [
        f"swap-out {HOME} docker-compose.yml",
        f"compose {HOME} homelab-cirdan up --no-start --remove-orphans",
        f"compose {HOME} homelab-cirdan stop",
    ]


def test_services_on_a_port_held_outside_docker_are_created_not_started(
    settings: Settings, tmp_path: Path
) -> None:
    held = work(tmp_path, services=("plex", "heimdall"), runtime=replace(UP, defer=("heimdall",)))
    node = FakeNode()
    run(settings, node, held)
    ups = [call for call in node.calls if " up " in call]
    assert ups == [
        f"compose {HOME} homelab-cirdan up -d --remove-orphans plex",
        f"compose {HOME} homelab-cirdan up --no-start heimdall",
    ]


def test_stopped_services_stay_down_and_stay_stopped_on_undo(
    settings: Settings, tmp_path: Path
) -> None:
    """A service its restart policy leaves down is created, not started, and not waited for; undo
    starts only what ran."""
    runtime = replace(
        UP,
        running=frozenset({NEW.name}),
        stay_down=("heimdall",),
        partly_up={NEW.name: ("plex",)},
    )
    kept = replace(
        work(tmp_path, services=("plex", "heimdall"), runtime=runtime),
        old_stacks=(NEW,),
        old_projects=frozenset({NEW.name}),
    )
    node = FakeNode(fail="pointer")
    with pytest.raises(ApplyError):
        run(settings, node, kept)
    assert [call for call in node.calls if " up " in call] == [
        f"compose {HOME} homelab-cirdan up -d --remove-orphans plex",
        f"compose {HOME} homelab-cirdan up --no-start heimdall",
        # undo
        f"compose {HOME} homelab-cirdan up --no-start --remove-orphans",
        f"compose {HOME} homelab-cirdan up -d plex",
    ]


def test_a_stack_left_down_is_named_at_the_end(settings: Settings, tmp_path: Path) -> None:
    said: list[str] = []
    executor = Executor(
        settings, said.append, wait=5, when=datetime(2026, 10, 31, 9, 0), commit="c0ffee1"
    )
    executor.run({"cirdan": FakeNode()}, [work(tmp_path, runtime=NodeRuntime())], Plan())  # type: ignore[dict-item]
    assert said[-1] == (
        "cirdan/homelab-cirdan was not running, so we didn't automatically start it. Use labctl "
        "node up cirdan/homelab-cirdan to start it."
    )


def test_each_step_is_marked_and_the_commit_is_asked_only_once_all_applied(
    settings: Settings, tmp_path: Path
) -> None:
    said: list[str] = []
    seen_before_commit: list[str] = []

    def commit() -> str:
        seen_before_commit.extend(said)
        return "c0ffee1"

    executor = Executor(
        settings, said.append, wait=5, when=datetime(2026, 10, 31, 9, 0), commit_with=commit
    )
    executor.run({"cirdan": FakeNode()}, [work(tmp_path)], Plan())  # type: ignore[dict-item]
    assert seen_before_commit == [
        "[green]✓[/] cirdan: down docker",
        "[green]✓[/] cirdan: files",
        "[green]✓[/] cirdan: up homelab-cirdan: 1/1 running",
        "[green]✓[/] cirdan: archive: sonarr/",
        "",
        "[green]✓[/] cirdan: applied",
        "",
    ]


def test_a_failed_step_is_marked_and_nothing_is_committed(
    settings: Settings, tmp_path: Path
) -> None:
    said: list[str] = []
    committed: list[str] = []
    executor = Executor(
        settings,
        said.append,
        wait=5,
        when=datetime(2026, 10, 31, 9, 0),
        commit_with=lambda: committed.append("x") or "c0ffee1",
    )
    with pytest.raises(ApplyError):
        executor.run({"cirdan": FakeNode(fail="homelab-cirdan up")}, [work(tmp_path)], Plan())  # type: ignore[dict-item]
    assert "[bold red]✗[/] cirdan: up homelab-cirdan" in said
    assert "Rolling back" in said
    assert committed == []


class StreamingRemote(Remote):
    """A node whose compose prints two lines, then exits with ``code``."""

    def __init__(self, code: int) -> None:
        super().__init__("cirdan")
        self.code = code

    def stream(self, command: str) -> Any:
        assert "--progress plain" in command
        code = self.code

        class Proc:
            stdout = iter([" Container web  Created\r\n", " Container web  Started\r\n"])

            def wait(self) -> int:
                return code

        return Proc()


def test_compose_output_is_read_and_shown_only_when_it_fails() -> None:
    seen: list[str] = []
    Node(StreamingRemote(0)).compose(HOME, NEW, "up", "-d", watch=seen.append)
    assert seen == ["Container web  Created", "Container web  Started"]
    with pytest.raises(ApplyError, match=r"failed \(homelab-cirdan\):\n    Container web  Created"):
        Node(StreamingRemote(1)).compose(HOME, NEW, "up", "-d")


DOCKER_RUN = Container("heimdall", "running", image="heimdall", restart="unless-stopped")
ELSEWHERE = TakeDown(
    "cirdan",
    "media",
    "/opt/media",
    ("/opt/media/compose.yml",),
    True,
    ("sonarr",),
    ("sonarr",),
    (),
    (),
)


def test_what_is_in_the_way_goes_first_and_comes_back_on_undo(
    settings: Settings, tmp_path: Path
) -> None:
    runtime = replace(
        UP,
        stops=(Stop("cirdan", DOCKER_RUN, True, ("has the container name heimdall uses",)),),
        take_down=(ELSEWHERE,),
    )
    node = FakeNode(fail="homelab-cirdan up")
    with pytest.raises(ApplyError):
        run(settings, node, work(tmp_path, runtime=runtime))
    renamed = "heimdall-labctl-20261031-090000"
    assert node.calls[:5] == [
        "compose /opt/media media down",
        "docker update --restart=no heimdall",
        "docker stop heimdall",
        f"docker rename heimdall {renamed}",
        f"compose {HOME} docker down",
    ]
    assert node.calls[-4:] == [
        f"docker rename {renamed} heimdall",
        "docker start heimdall",
        "docker update --restart=unless-stopped heimdall",
        "compose /opt/media media up -d",
    ]


def test_a_stopped_container_in_the_way_is_only_renamed(settings: Settings, tmp_path: Path) -> None:
    stopped = replace(DOCKER_RUN, state="exited", restart="no")
    runtime = replace(UP, stops=(Stop("cirdan", stopped, True, ("name",)),))
    node = FakeNode()
    run(settings, node, work(tmp_path, runtime=runtime))
    assert node.calls[0] == "docker rename heimdall heimdall-labctl-20261031-090000"


HEIMDALL = Migrate(("heimdall",), "cirdan", "aragorn", ("heimdall",), ("heimdall",))
STAGING = ".labctl/incoming/cirdan-20261031-090000-0"


@pytest.fixture
def relayed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    copies: list[str] = []
    monkeypatch.setattr(
        "labctl.apply.relay",
        lambda source, read, target, write: copies.append(f"{source.host}->{target.host}"),
    )
    return copies


class Nodes(dict[str, FakeNode]):
    """One FakeNode per node, each with its own calls."""


def run_two(settings: Settings, tmp_path: Path, nodes: Nodes, plan: Plan) -> None:
    source = work(tmp_path, node="cirdan", services=(), archive=HEIMDALL.archive)
    target = work(tmp_path, node="aragorn", services=("heimdall",), archive=())
    executor = Executor(settings, lambda text: None, wait=5, when=datetime(2026, 10, 31, 9, 0))
    executor.run(nodes, [target, source], plan)  # type: ignore[arg-type]


def test_a_migrate_stops_copies_verifies_places_then_archives_the_source(
    settings: Settings, tmp_path: Path, relayed: list[str]
) -> None:
    nodes = Nodes(cirdan=FakeNode(), aragorn=FakeNode())
    for name, fake in nodes.items():
        fake.remote = Remote(name)  # type: ignore[attr-defined]
    run_two(settings, tmp_path, nodes, Plan(operations=(HEIMDALL,)))
    assert relayed == ["cirdan->aragorn"]
    cirdan, aragorn = nodes["cirdan"].calls, nodes["aragorn"].calls
    assert cirdan[:2] == [f"compose {HOME} docker stop heimdall", f"checksums {APPDATA} heimdall"]
    assert aragorn[:3] == [
        f"checksums {APPDATA}/{STAGING} heimdall",
        f"move {APPDATA}/{STAGING}/heimdall {APPDATA}/heimdall",
        f"helper {APPDATA} {STAGING}",  # empty staging dirs removed
    ]
    # The source's copy is archived only after every node is up.
    assert cirdan.index(
        f"move {APPDATA}/heimdall {APPDATA}/.labctl/archive/heimdall-20261031-090000"
    ) > (cirdan.index(f"compose {HOME} homelab-cirdan up -d --remove-orphans"))


def test_a_bad_copy_is_archived_and_the_source_restarted(
    settings: Settings, tmp_path: Path, relayed: list[str]
) -> None:
    source, target = FakeNode(), FakeNode()
    target.checksum_text = "different  heimdall/config.ini\n"
    nodes = Nodes(cirdan=source, aragorn=target)
    for name, fake in nodes.items():
        fake.remote = Remote(name)  # type: ignore[attr-defined]
    with pytest.raises(ApplyError, match="differs from the source"):
        run_two(settings, tmp_path, nodes, Plan(operations=(HEIMDALL,)))
    assert source.calls[-1] == f"compose {HOME} docker start heimdall"
    assert target.calls[-1] == (
        f"move {APPDATA}/{STAGING} {APPDATA}/.labctl/archive/incoming-failed-20261031-090000"
    )


def test_renamed_on_the_way(settings: Settings, tmp_path: Path, relayed: list[str]) -> None:
    renamed = Migrate(
        ("heimdall",), "cirdan", "aragorn", ("heimdall",), ("heimdall",), (("heimdall", "dash"),)
    )
    nodes = Nodes(cirdan=FakeNode(), aragorn=FakeNode())
    nodes["aragorn"].checksum_text = nodes["cirdan"].checksum_text
    for name, fake in nodes.items():
        fake.remote = Remote(name)  # type: ignore[attr-defined]
    run_two(settings, tmp_path, nodes, Plan(operations=(renamed,)))
    assert f"move {APPDATA}/{STAGING}/dash {APPDATA}/heimdall" in nodes["aragorn"].calls


# ── Named volumes ─────────────────────────────────────────────────────────────

STAMP = "20261031-090000"
DB = Volume("homelab-cirdan_db", "db", NEW.name)
PG_OLD = Service(
    "pg", volumes=("docker_db",), mounts=(("/data", "volume:docker_db"),), stack="docker"
)
RENAME = Move(
    "cirdan", VOLUME, "volume:docker_db", "volume:homelab-cirdan_db", "volume:docker_db",
    "volume:homelab-cirdan_db", ("pg",),
)  # fmt: skip


def renamed(tmp_path: Path) -> NodeWork:
    return work(
        tmp_path,
        moves=((RENAME, RENAME.source, RENAME.target),),
        services=("pg",),
        archive=("volume:docker_db",),
        reason="renamed",
        volumes=(DB,),
        old_services=(PG_OLD,),
    )


def test_a_renamed_volume_is_copied_while_down_and_archived_once_up(
    settings: Settings, tmp_path: Path
) -> None:
    node = FakeNode()
    node.volumes = {"docker_db": True}
    run(settings, node, renamed(tmp_path))
    entry = f".labctl/archive/volume-docker_db-{STAMP}"
    calls = node.calls
    labels = [("com.docker.compose.project", NEW.name), ("com.docker.compose.volume", "db")]
    assert calls[:5] == [
        f"compose {HOME} docker down",
        f"volume create homelab-cirdan_db {labels}",  # as compose would make it
        "copy docker_db homelab-cirdan_db",
        "sums docker_db",
        "sums homelab-cirdan_db",
    ]
    up = calls.index(f"compose {HOME} homelab-cirdan up -d --remove-orphans")
    assert calls[up + 1 :][:6] == [
        f"helper {APPDATA} {entry}",  # mkdir
        f"copy docker_db {APPDATA}/{entry}",
        "sums docker_db",
        f"sums {APPDATA}/{entry}",
        f"sidecar {entry}.labctl.json",
        "volume rm docker_db",
    ]
    sidecar = node.sidecars[f"{entry}.labctl.json"]
    assert sidecar["reason"] == "renamed" and sidecar["key"] == "volume:docker_db"
    assert sidecar["mounts"] == [
        {"service": "pg", "stack": "docker", "target": "/data", "key": "volume:docker_db"}
    ]
    assert sidecar["labels"] == {"com.docker.compose.project": "docker"}


def test_a_failed_apply_removes_the_new_volume_and_keeps_the_old(
    settings: Settings, tmp_path: Path
) -> None:
    node = FakeNode(fail="up -d --remove-orphans")
    node.volumes = {"docker_db": True}
    with pytest.raises(ApplyError):
        run(settings, node, renamed(tmp_path))
    assert "volume rm homelab-cirdan_db" in node.calls
    assert "volume rm docker_db" not in node.calls


def test_a_volume_still_used_elsewhere_is_not_archived(settings: Settings, tmp_path: Path) -> None:
    node = FakeNode()
    node.volumes = {"docker_db": True}
    node.users = {"docker_db": ["backup-job"]}
    run(settings, node, renamed(tmp_path))
    assert "volume rm docker_db" not in node.calls


def test_data_in_the_target_volume_needs_replace_stale(settings: Settings, tmp_path: Path) -> None:
    node = FakeNode()
    node.volumes = {"docker_db": True, "homelab-cirdan_db": True}
    with pytest.raises(ApplyError, match="already has data"):
        run(settings, node, renamed(tmp_path))
    assert not any(c.startswith("copy") for c in node.calls)


PG_MIGRATE = Migrate(
    ("volume:aragorn_db",), "cirdan", "aragorn", ("pg",), ("pg",),
    (("volume:aragorn_db", "volume:cirdan_db"),),
)  # fmt: skip


def test_a_volume_migrates_straight_into_the_target_s_volume(
    settings: Settings, tmp_path: Path, relayed: list[str]
) -> None:
    nodes = Nodes(cirdan=FakeNode(), aragorn=FakeNode())
    for name, fake in nodes.items():
        fake.remote = Remote(name)  # type: ignore[attr-defined]
    nodes["cirdan"].volumes = {"cirdan_db": True}
    source = work(
        tmp_path,
        node="cirdan",
        services=(),
        archive=PG_MIGRATE.archive,
        old_services=(Service("pg", stack="docker"),),
    )
    target = work(
        tmp_path,
        node="aragorn",
        services=("pg",),
        archive=(),
        volumes=(Volume("aragorn_db", "db", NEW.name),),
    )
    executor = Executor(settings, lambda text: None, wait=5, when=datetime(2026, 10, 31, 9, 0))
    executor.run(nodes, [target, source], Plan(operations=(PG_MIGRATE,)))  # type: ignore[arg-type]
    assert relayed == ["cirdan->aragorn"]
    aragorn = nodes["aragorn"].calls
    assert aragorn[0].startswith("volume create aragorn_db")
    assert aragorn[1] == "sums aragorn_db"
    assert nodes["cirdan"].calls[:2] == [
        f"compose {HOME} docker stop pg",
        "sums cirdan_db",
    ]
    assert "volume rm cirdan_db" in nodes["cirdan"].calls  # archived once aragorn is up


def test_a_same_node_restore_moves_the_directory_back(settings: Settings, tmp_path: Path) -> None:
    restore = Restore(
        "plex",
        "cirdan",
        "cirdan",
        "20261101-090000",
        APPDATA,
        (("plexms-20261101-090000", "plex"),),
    )
    node = FakeNode()
    run(settings, node, work(tmp_path, archive=()), plan=Plan(operations=(restore,)))
    assert node.calls[:2] == [
        f"move {APPDATA}/.labctl/archive/plexms-20261101-090000 {APPDATA}/plex",
        f"helper {APPDATA}/.labctl/archive plexms-20261101-090000",  # its sidecar: .restored
    ]


def test_a_restore_from_another_node_copies_into_a_new_volume(
    settings: Settings, tmp_path: Path, relayed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source node is not changed: only read (its archive stays)."""
    restore = Restore(
        "pg", "aragorn", "cirdan", "20261101-090000", APPDATA,
        (("volume-cirdan_db-20261101-090000", "volume:aragorn_db"),),
    )  # fmt: skip
    nodes = Nodes(aragorn=FakeNode())
    nodes["aragorn"].remote = Remote("aragorn")  # type: ignore[attr-defined]
    target = work(
        tmp_path, node="aragorn", services=("pg",), archive=(),
        volumes=(Volume("aragorn_db", "db", NEW.name),),
    )  # fmt: skip
    executor = Executor(settings, lambda text: None, wait=5, when=datetime(2026, 10, 31, 9, 0))
    reader = FakeNode()
    reader.remote = Remote("cirdan")  # type: ignore[attr-defined]
    monkeypatch.setattr("labctl.apply.Node", lambda remote: reader)
    executor.run(nodes, [target], Plan(operations=(restore,)))  # type: ignore[arg-type]
    assert relayed == ["cirdan->aragorn"]
    assert nodes["aragorn"].calls[0].startswith("volume create aragorn_db")
    assert reader.calls == [f"sums {APPDATA}/.labctl/archive/volume-cirdan_db-20261101-090000"]


# ── Services moving between stacks (real compose) ─────────────────────────────

docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")

APPS = {
    "apps/dozzle.yml": "services:\n  dozzle:\n    image: amir20/dozzle\n",
    "apps/fileflows.yml": "services:\n  fileflows:\n    image: revenz/fileflows\n",
}
REPO = "name: homelab-cirdan\ninclude:\n  - apps/dozzle.yml\n  - apps/fileflows.yml\n"
STACK1 = "name: homelab-cirdan-stack1\ninclude:\n  - apps/dozzle.yml\n"
STACK2 = "name: homelab-cirdan-stack2\ninclude:\n  - apps/fileflows.yml\n"


def tree(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def restack(
    settings: Settings,
    tmp_path: Path,
    on_node: dict[str, str],
    projects: dict[str, tuple[str, ...]],
) -> tuple[Plan, NodeWork]:
    """Plan the repo's one stack against a DEPLOY_DIR laid out as ``on_node``."""
    dirs = Dirs(HOME, APPDATA)
    bundle = tree(tmp_path / "bundle" / "docker", {**APPS, "docker-compose.yml": REPO})
    node = tree(tmp_path / "node" / "docker", on_node)
    env = CompiledEnv(values={"DEPLOY_DIR": HOME, "APPDATA_DIR": APPDATA})
    wanted = current_state(settings, "cirdan", bundle, dirs, {})
    location = Location(str(bundle), docker_cli=settings.docker_cli)
    configs = dict(resolve_stacks(location, list(wanted.stacks.values()), HOME))
    desired = Desired(
        "cirdan", bundle, env, {s.name: c for s, c in configs.items()}, wanted, manifest(bundle)
    )
    current = current_state(settings, "cirdan", node, dirs, projects)
    running = tuple(
        Container(
            f"{service.stack}-{service.name}-1",
            "running",
            project=service.stack,
            service=service.name,
            files=projects[service.stack],
            managed=True,
        )
        for service in current.services.values()
        if service.stack in projects
    )
    collected = Collected(
        desired={"cirdan": desired},
        recorded={"cirdan": current},
        recorded_dirs={"cirdan": dirs},
        reports=[NodeReport("cirdan", "node")],
        node_files={"cirdan": manifest(node)},
        node_names={"cirdan": {p.name: "dir" if p.is_dir() else "file" for p in node.iterdir()}},
        projects={"cirdan": frozenset(projects)},
        containers={"cirdan": running},
    )
    plan = make_plan({"cirdan": wanted}, {"cirdan": current})
    (node_work_,) = node_work(plan, collected, Scope())
    runtime = analyse(
        collected.desired,
        collected.recorded,
        plan,
        containers=collected.containers,
        listeners={},
        missing={},
        work={"cirdan"},
    )
    return plan, replace(node_work_, runtime=runtime.for_node("cirdan"))


@docker
def test_hand_made_stacks_merge_into_the_repos_stack(settings: Settings, tmp_path: Path) -> None:
    """The first apply on cirdan: two stacks started by hand (stack.yml, stack2.yml) run the
    services the repo puts in one. They move stacks (an update each, nothing removed, so nothing
    to confirm); the two emptied stacks go down and their files are archived."""
    plan, job = restack(
        settings,
        tmp_path,
        {**APPS, "stack.yml": STACK1, "stack2.yml": STACK2},
        {"homelab-cirdan-stack1": ("stack.yml",), "homelab-cirdan-stack2": ("stack2.yml",)},
    )
    assert plan.operations == (Update("dozzle", "cirdan"), Update("fileflows", "cirdan"))
    assert job.discard == ("stack.yml", "stack2.yml")
    node = FakeNode()
    run(settings, node, job)
    assert node.calls == [
        f"compose {HOME} homelab-cirdan-stack2 down",
        f"compose {HOME} homelab-cirdan-stack1 down",
        f"mkdir -p {HOME}/.labctl",
        f"rm -rf {HOME}/.labctl/incoming",
        f"upload {HOME}/.labctl/incoming",
        f"swap-in {HOME} apps docker-compose.yml stack.yml stack2.yml",
        f"compose {HOME} homelab-cirdan up -d --remove-orphans",
        f"mkdir -p {HOME}/.labctl/archive",
        *(
            f"mv -T -- {HOME}/.labctl/previous/{name} {HOME}/.labctl/archive/{name}-20261031-090000"
            for name in ("stack.yml", "stack2.yml")
        ),
        f"upload {HOME}",
        f"pointer {HOME}",
    ]


@docker
def test_stack_files_left_behind_are_archived(settings: Settings, tmp_path: Path) -> None:
    """After that apply had left the two files in place: they define the same services, but the
    repo's stack is the one that runs. Nothing changes but the files."""
    plan, job = restack(
        settings,
        tmp_path,
        {**APPS, "docker-compose.yml": REPO, "stack.yml": STACK1, "stack2.yml": STACK2},
        {"homelab-cirdan": ("docker-compose.yml",)},
    )
    assert plan.operations == ()
    assert job.discard == ("stack.yml", "stack2.yml")
    node = FakeNode()
    run(settings, node, job)
    assert not [call for call in node.calls if " down" in call]  # nothing to take down
    archive = f"{HOME}/.labctl/archive"
    assert [call for call in node.calls if call.startswith("mv")] == [
        f"mv -T -- {HOME}/.labctl/previous/stack.yml {archive}/stack.yml-20261031-090000",
        f"mv -T -- {HOME}/.labctl/previous/stack2.yml {archive}/stack2.yml-20261031-090000",
    ]


def test_node_commands_name_paths_inside_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Node, not the fake: what the helper container is given."""
    from labctl.apply import HELPER, Node

    ran: list[tuple[str, ...]] = []
    node = Node(Remote("cirdan"))
    monkeypatch.setattr(node, "run", lambda *args, cwd="/": ran.append(args) or "")
    node.move(f"{APPDATA}/.labctl/incoming/op/heimdall", f"{APPDATA}/heimdall")
    node.data_state(APPDATA, "heimdall")
    node.checksums(f"{APPDATA}/.labctl/incoming/op", ["heimdall"])
    move, state, sums = ran
    assert move[:6] == ("docker", "run", "--rm", "-v", f"{APPDATA}:/x", HELPER)
    assert move[-2:] == ("/x/.labctl/incoming/op/heimdall", "/x/heimdall")
    assert state[4] == f"{APPDATA}:/x" and state[-1] == "heimdall"
    assert sums[4] == f"{APPDATA}/.labctl/incoming/op:/x:ro" and sums[-1] == "heimdall"


def test_the_up_step_says_which_containers_changed(settings: Settings, tmp_path: Path) -> None:
    said: list[str] = []
    node = FakeNode()
    node.snapshots = [
        {  # before: plex in the old stack, a container compose removes as an orphan
            ("docker", "plex"): Seen(frozenset({"a1"}), True),
            ("homelab-cirdan", "old"): Seen(frozenset({"o1"}), True),
        },
        {  # after: plex moved in with a new container, heimdall made
            ("homelab-cirdan", "plex"): Seen(frozenset({"b2"}), True),
            ("homelab-cirdan", "heimdall"): Seen(frozenset({"h1"}), False),
        },
    ]
    executor = Executor(settings, said.append, wait=5, when=datetime(2026, 10, 31, 9, 0))
    executor.run({"cirdan": node}, [work(tmp_path)], Plan())  # type: ignore[dict-item]
    assert (
        "[green]✓[/] cirdan: up homelab-cirdan: 1/1 running, recreated plex; created heimdall; "
        "removed old" in said
    )


def test_a_rollback_says_what_does_not_run_as_before(settings: Settings, tmp_path: Path) -> None:
    said: list[str] = []
    node = FakeNode(fail="archive")
    node.snapshots = [
        {("docker", "plex"): Seen(frozenset({"a1"}), True)},
        {("docker", "plex"): Seen(frozenset({"a1"}), False)},
    ]
    executor = Executor(settings, said.append, wait=5, when=datetime(2026, 10, 31, 9, 0))
    with pytest.raises(ApplyError):
        executor.run({"cirdan": node}, [work(tmp_path)], Plan())  # type: ignore[dict-item]
    assert "[yellow]![/] cirdan: not running as before the apply: plex" in said


class Helper:
    """The real Node with its helper container and copy faked: the filesystems ``elsewhere``
    names are not the one the move starts on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, elsewhere: bool, same_copy: bool = True):
        self.node = Node(Remote("frodo"))
        self.calls: list[str] = []

        def helper(root: str, script: str, *args: str, read_only: bool = False) -> str:
            if "elsewhere" in script:
                self.calls.append(f"try {' '.join(args)}")
                return "elsewhere\n" if elsewhere else ""
            self.calls.append(f"helper {root} {' '.join(args)}")
            return ""

        def sums(spec: str) -> str:
            return "abc  ./x\n" if same_copy or spec.startswith("/zconfig") else "def  ./x\n"

        monkeypatch.setattr(self.node, "helper", helper)
        monkeypatch.setattr(self.node, "copy", lambda s, t: self.calls.append(f"copy {s} {t}"))
        monkeypatch.setattr(self.node, "sums", sums)


def test_a_move_on_one_filesystem_is_a_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Helper(monkeypatch, elsewhere=False)
    back = fake.node.move("/zconfig/appdata", "/zconfig/data")
    assert fake.calls == ["try /x/appdata /x/data"] and fake.node.moved_away == []
    back()
    assert fake.calls[-1] == "try /x/data /x/appdata"


def test_a_move_to_another_filesystem_copies_and_deletes_the_original_only_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Helper(monkeypatch, elsewhere=True)
    fake.node.move("/zconfig/appdata", "/optane/appdata")
    assert fake.calls == [
        "try /x/zconfig/appdata /x/optane/appdata",
        "copy /zconfig/appdata /optane/appdata",
    ]
    assert fake.node.moved_away == ["/zconfig/appdata"]
    assert fake.node.delete_moved_away() == []
    assert fake.calls[-1] == "helper /zconfig appdata"  # rm -rf, once the apply succeeded
    assert fake.node.moved_away == []


def test_undoing_a_move_to_another_filesystem_drops_the_copy_and_keeps_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Helper(monkeypatch, elsewhere=True)
    back = fake.node.move("/zconfig/appdata", "/optane/appdata")
    back()
    assert fake.calls[-1] == "helper /optane appdata"
    assert fake.node.moved_away == []


def test_a_copy_that_differs_is_removed_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Helper(monkeypatch, elsewhere=True, same_copy=False)
    with pytest.raises(ApplyError, match="differs from the original"):
        fake.node.move("/zconfig/appdata", "/optane/appdata")
    assert fake.calls[-1] == "helper /optane appdata" and fake.node.moved_away == []


def test_originals_are_deleted_only_after_everything_applied(
    settings: Settings, tmp_path: Path
) -> None:
    deleted: list[str] = []

    class Moving(FakeNode):
        def delete_moved_away(self) -> list[str]:
            deleted.append("now")
            return ["/zconfig/appdata was copied, but is still there: busy"]

    said: list[str] = []
    executor = Executor(settings, said.append, wait=5, when=datetime(2026, 10, 31, 9, 0))
    with pytest.raises(ApplyError):
        executor.run({"cirdan": Moving(fail="up -d")}, [work(tmp_path)], Plan())  # type: ignore[dict-item]
    assert deleted == []  # rolled back: the originals stay
    executor.run({"cirdan": Moving()}, [work(tmp_path)], Plan())  # type: ignore[dict-item]
    assert deleted == ["now"]
    assert any("still there: busy" in line for line in said)
