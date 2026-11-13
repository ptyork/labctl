from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from labctl import docker
from labctl import remote as remote_module
from labctl import stack as stack_module
from labctl.bundle import Location
from labctl.cli import cli
from labctl.cli.commands import node as node_module
from labctl.remote import Remote
from labctl.settings import write_config
from labctl.stack import Seen, ServiceStatus, parse_restarts, ps_rows, statuses
from labctl.state import Stack

POINTERS: dict[str, str] = {}  # node -> what its ~/.labctl-home says

CIRDAN = Stack("homelab-cirdan", ("docker-compose.yml",))
INFRA = Stack("homelab-frodo-infra", ("infra.yml",))
MEDIA = Stack("homelab-frodo-media", ("media-extra.yml", "media.yml"))
SERVICES = {
    "homelab-cirdan": ["heimdall"],
    "homelab-frodo-infra": ["npm"],
    "homelab-frodo-media": ["plex", "tdarr"],
}


@pytest.fixture
def ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> list[tuple[str, str]]:
    """cirdan with one stack in its DEPLOY_DIR, frodo with two (discovery itself is tested in
    test_bundle); records (host, command) instead of running ssh."""
    for node in ("cirdan", "frodo"):
        (tmp_path / "nodes" / node).mkdir(parents=True)
        (tmp_path / "nodes" / node / "docker-compose.yml").write_text("services: {}\n")
        (tmp_path / "nodes" / node / "node.env").write_text(f"DEPLOY_DIR=/srv/{node}\n")
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    write_config(tmp_path / "labctl.toml", {"lab_config": str(tmp_path)})
    on_node = {"cirdan": [CIRDAN], "frodo": [INFRA, MEDIA], "bare": []}
    calls: list[tuple[str, str]] = []
    failing = {"cirdan"} if "failing" in request.fixturenames else set()

    def discover(location: Location) -> tuple[list[Stack], dict[str, str]]:
        assert location.remote is not None
        calls.append((location.remote.host, f"discover {location.root}"))
        return on_node[location.remote.host], {"notes.yml": "defines no services"}

    monkeypatch.setattr(Location, "read", lambda self, path: None)

    def run(self: Remote, command: str, tty: bool = False) -> int:
        calls.append((self.host, command))
        return 1 if self.host in failing else 0

    def execute(self: Remote, args: Sequence[str], cwd: str) -> remote_module.Result:
        """The ~/.labctl-home pointer (set in POINTERS) and the containers (none) are read this
        way."""
        if tuple(args[:2]) == ("docker", "ps"):
            return remote_module.Result("", "", 0)
        home = POINTERS.get(self.host)
        if home:
            return remote_module.Result(home + "\n", "", 0)
        return remote_module.Result("", "No such file", 1)

    POINTERS.clear()
    monkeypatch.setattr(stack_module, "discover_stacks", discover)
    monkeypatch.setattr(Remote, "execute", execute)
    monkeypatch.setattr(Remote, "run", run)
    monkeypatch.setattr(Remote, "close_all", staticmethod(lambda hosts: None))
    monkeypatch.setattr(node_module, "stack_services", lambda loc, stack: SERVICES[stack.name])
    return calls


def invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.main, ["node", *args])


def commands(calls: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(host, command) for host, command in calls if not command.startswith("discover ")]


def test_stacks_are_discovered_in_deploy_dir_on_the_node(ran: list[tuple[str, str]]) -> None:
    assert invoke("restart", "frodo").exit_code == 0
    assert ran[0] == ("frodo", "discover /srv/frodo")


def test_single_stack_with_services(
    ran: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(SERVICES, "homelab-cirdan", ["plex", "sonarr"])
    assert invoke("up", "cirdan", "-s", "plex", "--service", "sonarr").exit_code == 0
    up = ("up", "-d", "--remove-orphans")
    assert commands(ran) == [
        ("cirdan", docker.compose_command("/srv/cirdan", CIRDAN, *up, "plex", "sonarr"))
    ]


def test_every_stack_in_order_and_down_in_reverse(ran: list[tuple[str, str]]) -> None:
    assert invoke("up", "frodo").exit_code == 0
    assert invoke("down", "frodo").exit_code == 0
    up = ("up", "-d", "--remove-orphans")
    assert commands(ran) == [
        ("frodo", docker.compose_command("/srv/frodo", INFRA, *up)),
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, *up)),
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, "down")),
        ("frodo", docker.compose_command("/srv/frodo", INFRA, "down")),
    ]


def test_one_stack_by_name_or_suffix(ran: list[tuple[str, str]]) -> None:
    assert invoke("restart", "frodo/media").exit_code == 0
    assert invoke("restart", "frodo/homelab-frodo-infra").exit_code == 0
    assert commands(ran) == [
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, "restart")),
        ("frodo", docker.compose_command("/srv/frodo", INFRA, "restart")),
    ]
    output = invoke("up", "frodo/books").output
    assert "frodo/books is not a stack; stacks: homelab-frodo-infra, homelab-frodo-media" in output


def test_services_run_in_their_own_stacks(ran: list[tuple[str, str]]) -> None:
    assert invoke("pull", "frodo", "-s", "tdarr", "-s", "npm").exit_code == 0
    pull = ("pull", "--ignore-pull-failures")
    assert commands(ran) == [
        ("frodo", docker.compose_command("/srv/frodo", INFRA, *pull, "npm")),
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, *pull, "tdarr")),
    ]
    assert "frodo: no stack has sonarr" in invoke("up", "frodo", "-s", "sonarr").output


def test_update_pulls_ups_and_prunes(ran: list[tuple[str, str]]) -> None:
    assert invoke("update", "frodo/media", "-s", "plex").exit_code == 0
    assert commands(ran) == [
        (
            "frodo",
            docker.compose_command("/srv/frodo", MEDIA, "pull", "--ignore-pull-failures", "plex"),
        ),
        (
            "frodo",
            docker.compose_command("/srv/frodo", MEDIA, "up", "-d", "--remove-orphans", "plex"),
        ),
        ("frodo", "docker image prune --force"),  # node-wide, also for one stack
    ]


@pytest.mark.usefixtures("failing")
def test_update_goes_no_further_on_a_node_that_fails(ran: list[tuple[str, str]]) -> None:
    result = invoke("update", "cirdan", "frodo")
    assert result.exit_code == 1
    assert [command.split()[-1] for host, command in commands(ran) if host == "cirdan"] == [
        "--ignore-pull-failures"
    ]
    assert len(commands(ran)) == 1 + 4 + 1  # frodo: pull and up on two stacks, then prune


def test_several_targets(ran: list[tuple[str, str]]) -> None:
    """Targets run in the order given; a node and one of its stacks is the whole node."""
    assert invoke("restart", "frodo/media", "cirdan", "frodo/infra").exit_code == 0
    assert commands(ran) == [
        ("frodo", docker.compose_command("/srv/frodo", INFRA, "restart")),
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, "restart")),
        ("cirdan", docker.compose_command("/srv/cirdan", CIRDAN, "restart")),
    ]
    ran.clear()
    assert invoke("restart", "frodo/media", "frodo").exit_code == 0
    assert len(commands(ran)) == 2


def test_services_run_only_where_they_are(ran: list[tuple[str, str]]) -> None:
    result = invoke("up", "all", "-s", "plex")
    assert result.exit_code == 0, result.output
    up = ("up", "-d", "--remove-orphans")
    assert commands(ran) == [("frodo", docker.compose_command("/srv/frodo", MEDIA, *up, "plex"))]
    ran.clear()
    output = invoke("up", "all", "-s", "plex", "-s", "sonarr").output
    assert "the targets: no stack has sonarr" in output
    assert commands(ran) == []  # nothing runs


def test_all_runs_every_node(ran: list[tuple[str, str]]) -> None:
    result = invoke("pull", "all")
    assert result.exit_code == 0, result.output
    assert [host for host, _ in commands(ran)] == ["cirdan", "frodo", "frodo"]


def test_logs_one_after_another(ran: list[tuple[str, str]]) -> None:
    assert invoke("logs", "frodo").exit_code == 0
    assert invoke("logs", "cirdan", "frodo", "-s", "plex", "-n", "5").exit_code == 0
    logs = ("logs", "--tail", "100")
    assert commands(ran) == [
        ("frodo", docker.compose_command("/srv/frodo", INFRA, *logs)),
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, *logs)),
        ("frodo", docker.compose_command("/srv/frodo", MEDIA, "logs", "--tail", "5", "plex")),
    ]


def test_following_several_stacks_prefixes_each_line(
    ran: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Proc:
        def __init__(self, lines: list[str], code: int) -> None:
            self.stdout, self.code = iter(lines), code

        def wait(self) -> int:
            return self.code

        def terminate(self) -> None:
            pass

    output = {"cirdan": ["heimdall  | started\n"], "frodo": ["npm  | ready\n", "plex  | up\n"]}
    streams: list[str] = []

    def stream(self: Remote, command: str) -> Proc:
        streams.append(command)
        return Proc([output[self.host].pop(0)], 1 if "infra" in command else 0)

    monkeypatch.setattr(Remote, "stream", stream)
    result = invoke("logs", "cirdan", "frodo", "-f")
    assert result.exit_code == 1
    assert re.search(r"^cirdan {20}\| heimdall  \| started$", result.output, re.MULTILINE)
    assert "frodo/homelab-frodo-infra | npm  | ready" in result.output
    assert "frodo/homelab-frodo-media | plex  | up" in result.output
    assert "Failed on: frodo/homelab-frodo-infra" in result.output
    assert all("--follow" in command for command in streams)
    assert commands(ran) == []  # nothing ran attached to the terminal


@pytest.fixture
def failing() -> None:
    """With this fixture, commands on cirdan fail."""


@pytest.mark.usefixtures("failing")
def test_a_failure_does_not_stop_the_other_nodes(ran: list[tuple[str, str]]) -> None:
    result = invoke("up", "all")
    assert result.exit_code == 1
    assert [host for host, _ in commands(ran)] == ["cirdan", "frodo", "frodo"]
    assert "Failed on: cirdan" in result.output


def test_rejected_targets(ran: list[tuple[str, str]]) -> None:
    assert "its node" in invoke("up", "all/media").output
    assert "already means every node" in invoke("up", "all", "frodo").output
    assert "Unknown node 'bilbo'" in invoke("up", "frodo", "bilbo").output
    assert "prune is node-wide" in invoke("prune", "frodo/media").output
    assert "No such command 'frodo'" in invoke("frodo", "up").output
    assert "Missing argument" in invoke("up").output
    assert commands(ran) == []


def test_a_node_may_be_named_like_a_command(
    ran: list[tuple[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nodes" / "prune").mkdir()
    (tmp_path / "nodes" / "prune" / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "nodes" / "prune" / "node.env").write_text("DEPLOY_DIR=/srv/prune\n")
    monkeypatch.setattr(stack_module, "discover_stacks", lambda location: ([CIRDAN], {}))
    assert invoke("prune", "prune").exit_code == 0
    assert commands(ran) == [("prune", "docker image prune --force")]


def test_command_help(ran: list[tuple[str, str]]) -> None:
    output = invoke("pull", "--help").output
    assert "Usage: main node pull [OPTIONS] NODE[/STACK]...|all" in output
    assert "Pull the stacks' images" in output
    assert "-s, --service SERVICE" in output


def test_the_node_says_where_deploy_dir_is(ran: list[tuple[str, str]]) -> None:
    """After an apply, DEPLOY_DIR is where labctl put the files (~/.labctl-home on the node),
    even if node.env has moved on."""
    POINTERS["frodo"] = "/old/home"
    assert invoke("prune", "frodo").exit_code == 0
    assert invoke("down", "frodo").exit_code == 0
    assert commands(ran) == [
        ("frodo", "docker image prune --force"),
        ("frodo", docker.compose_command("/old/home", MEDIA, "down")),
        ("frodo", docker.compose_command("/old/home", INFRA, "down")),
    ]


def test_compose_command_quotes_and_checks_the_home() -> None:
    command = docker.compose_command("/srv/my home", MEDIA, "up")
    assert command.startswith("cd '/srv/my home' 2>/dev/null || {")
    assert command.endswith(
        "; docker compose -p homelab-frodo-media -f media-extra.yml -f media.yml up"
    )


@pytest.mark.parametrize("array", [False, True])
def test_statuses_from_compose_ps(array: bool) -> None:
    rows = [
        {"Service": "plex", "State": "running", "Status": "Up 2 hours", "Image": "plex:1"},
        {"Service": "old", "State": "exited", "Status": "Exited (0)", "Image": "old:1"},
    ]
    ps = json.dumps(rows) if array else "\n".join(json.dumps(r) for r in rows)
    assert statuses("media", ["plex", "sonarr"], ps_rows(ps)) == [
        ServiceStatus("media", "old", "exited", "Exited (0)", "old:1", configured=False),
        ServiceStatus("media", "plex", "running", "Up 2 hours", "plex:1", configured=True),
        ServiceStatus("media", "sonarr", "", "", "", configured=True),
    ]


def test_restarts_and_restart_loops() -> None:
    output = "\n".join(
        [
            "1762970400",  # 2025-11-12T18:00:00Z
            "/media-sonarr-1 12 2025-11-12T17:59:00.123456789Z",  # up a minute, 12 restarts
            "/media-plex-1 4 2025-11-10T08:00:00Z",  # up two days: crashed long ago
            "/media-bazarr-1 1 2025-11-12T17:59:30Z",
            "/media-radarr-1 7 0001-01-01T00:00:00Z",  # Docker restarting it now
            "garbage",
        ]
    )
    restarts = parse_restarts(output)
    assert restarts["media-sonarr-1"] == (12, 60) and restarts["media-plex-1"][0] == 4
    rows = [
        {"Service": s, "Name": f"media-{s}-1", "State": state, "Status": "", "Image": ""}
        for s, state in [
            ("sonarr", "running"),
            ("plex", "running"),
            ("bazarr", "running"),
            ("radarr", "restarting"),
        ]
    ]
    looping = {s.service: s.looping for s in statuses("media", [], rows, restarts)}
    assert looping == {"bazarr": False, "plex": False, "radarr": True, "sonarr": True}
    assert parse_restarts("") == {}


def test_a_node_without_stack_files(ran: list[tuple[str, str]], tmp_path: Path) -> None:
    (tmp_path / "nodes" / "bare").mkdir()
    (tmp_path / "nodes" / "bare" / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "nodes" / "bare" / "node.env").write_text("DEPLOY_DIR=/srv/bare\n")
    output = invoke("up", "bare").output
    assert "bare: no stack files in /srv/bare" in output
    assert "notes.yml: defines no services" in output


def test_every_file_in_deploy_dir_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node is what it is: compose files left beside an applied DEPLOY_DIR (an old test
    stack) are stacks too, as plan sees them, until apply archives them."""
    from labctl import bundle as bundle_module
    from labctl.settings import Settings

    seen: list[list[str]] = []
    monkeypatch.setattr(Location, "names", lambda self: ["docker-compose.yml", "stack.yml", "apps"])
    monkeypatch.setattr(
        bundle_module, "stack_candidates", lambda location, names: seen.append(names) or names
    )
    monkeypatch.setattr(bundle_module, "find_stacks", lambda location, candidates: ([CIRDAN], {}))
    monkeypatch.setattr(
        stack_module,
        "execute",
        lambda args, cwd, remote: remote_module.Result("/srv/cirdan\n", "", 0),
    )
    settings = Settings(lab_config=Path("/unused"), config_dir=Path("/unused"))
    stack_module.deployed(settings, "cirdan", Remote("cirdan"))
    assert seen == [["docker-compose.yml", "stack.yml", "apps"]]


def test_up_says_which_containers_changed(
    ran: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = [
        {("homelab-cirdan", "heimdall"): Seen(frozenset({"1"}), True)},
        {("homelab-cirdan", "heimdall"): Seen(frozenset({"2"}), True)},
    ]
    monkeypatch.setattr(node_module, "read_snapshot", lambda remote, project: seen.pop(0))
    result = invoke("up", "cirdan")
    assert result.exit_code == 0, result.output
    assert "✓ cirdan: recreated heimdall" in result.output
    seen.extend([{}, {}])
    assert "✓ cirdan: no containers changed" in invoke("up", "cirdan").output
