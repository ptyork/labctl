from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from labctl.bundle import Desired
from labctl.envfile import CompiledEnv
from labctl.planner import Migrate, Plan
from labctl.runtime import (
    UNAVAILABLE,
    Container,
    Listener,
    NetworkInUse,
    Port,
    Runtime,
    TakeDown,
    analyse,
    parse_containers,
    parse_listeners,
    published,
)
from labctl.state import Network, NodeState, Service, Stack

HOME, APPDATA = "/srv/docker", "/srv/appdata"


def row(name: str, **fields: Any) -> str:
    return json.dumps({"name": f"/{name}", "state": "running", **fields})


def test_containers_and_who_manages_them() -> None:
    lines = [
        row(
            "homelab-cirdan-dozzle-1",
            project="homelab-cirdan",
            service="dozzle",
            files=f"{HOME}/docker-compose.yml",
            dir=HOME,
            ports={"8080/tcp": [{"HostIp": "", "HostPort": "8080"}], "53/udp": None},
            restart={"Name": "unless-stopped", "MaximumRetryCount": 0},
        ),
        row(
            "portainer",
            image="portainer/portainer-ce",
            project="<no value>",
            ports={"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000-9001"}]},
            restart={"Name": "on-failure", "MaximumRetryCount": 3},
            mounts=[
                {"Type": "bind", "Source": "/opt/p", "Destination": "/data"},
                {"Type": "volume"},
            ],
        ),
        row("old-media-sonarr-1", state="exited", project="media", files="/opt/media/compose.yml"),
        "",
    ]
    dozzle, old, portainer = parse_containers(lines, HOME)
    assert dozzle.managed and dozzle.up and dozzle.restart == "unless-stopped"
    assert dozzle.ports == (Port("tcp", 8080),)
    assert not old.managed and not old.up and old.project == "media"
    assert portainer == Container(
        "portainer",
        "running",
        image="portainer/portainer-ce",
        ports=(Port("tcp", 9000, "127.0.0.1"), Port("tcp", 9001, "127.0.0.1")),
        restart="on-failure:3",
        binds=(("/data", "/opt/p"),),
    )


def test_listeners_from_ss() -> None:
    lines = [
        'tcp LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:(("docker-proxy",pid=10,fd=4))',
        "tcp LISTEN 0 4096 [::]:22 [::]:*",
        'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=9,fd=13))',
        'tcp LISTEN 0 128 *:11434 *:* users:(("ollama",pid=7,fd=3))',
        "tcp LISTEN 0 128 [fe80::1%eth0]:546 [::]:*",
    ]
    assert parse_listeners(lines) == (
        Listener(Port("tcp", 22, "::")),
        Listener(Port("tcp", 546, "fe80::1")),
        Listener(Port("tcp", 8080, "0.0.0.0"), "docker-proxy"),
        Listener(Port("tcp", 11434, "*"), "ollama"),
        Listener(Port("udp", 53, "127.0.0.53"), "systemd-resolve"),
    )
    assert parse_listeners([UNAVAILABLE]) is None


def test_ports_clash_on_the_same_address_or_any_address() -> None:
    assert Port("tcp", 80).clashes(Port("tcp", 80, "10.0.0.5"))
    assert Port("tcp", 80, "127.0.0.1").clashes(Port("tcp", 80, "::"))
    assert not Port("tcp", 80, "127.0.0.1").clashes(Port("tcp", 80, "10.0.0.5"))
    assert not Port("tcp", 53).clashes(Port("udp", 53))
    assert published({"ports": [{"published": "8000-8001", "target": 80}]}) == (
        Port("tcp", 8000),
        Port("tcp", 8001),
    )
    assert published({"network_mode": "host", "ports": [{"published": "80"}]}) == ()


# ── analyse ───────────────────────────────────────────────────────────────────

Services = dict[str, dict[str, dict[str, Any]]]  # stack -> service -> resolved config


def desired(node: str, stacks: Services) -> Desired:
    state = NodeState.of(
        node,
        *(Service(name, stack=stack) for stack, services in stacks.items() for name in services),
        stacks=[Stack(stack, (f"{stack}.yml",)) for stack in stacks],
    )
    env = CompiledEnv(values={"DEPLOY_DIR": HOME, "APPDATA_DIR": APPDATA})
    configs = {stack: {"services": services} for stack, services in stacks.items()}
    return Desired(node, Path("/unused"), env, configs, state, {})


def recorded(node: str, stacks: dict[str, list[str]]) -> NodeState:
    return NodeState.of(
        node,
        *(Service(name, stack=stack) for stack, names in stacks.items() for name in names),
        stacks=[Stack(stack, (f"{stack}.yml",)) for stack in stacks],
    )


def ours(project: str, state: str = "running") -> Container:
    return Container(f"{project}-x-1", state, project=project, managed=True)


def run(
    want: dict[str, Desired],
    have: dict[str, NodeState],
    containers: dict[str, tuple[Container, ...]],
    listeners: dict[str, tuple[Listener, ...] | None] | None = None,
    plan: Plan | None = None,
    work: set[str] | None = None,
    missing: dict[str, frozenset[str]] | None = None,
    start_all: bool = False,
) -> Runtime:
    return analyse(
        want,
        have,
        plan or Plan(),
        containers=containers,
        listeners=listeners or {},
        missing=missing or {},
        work=set(containers) if work is None else work,
        start_all=start_all,
    )


def starts(runtime: Runtime) -> dict[str, tuple[bool, str]]:
    return {s.stack: (s.start, s.why) for s in runtime.stacks}


def test_stacks_keep_their_state() -> None:
    want = {"cirdan": desired("cirdan", {"web": {"a": {}}, "tools": {"b": {}}, "fresh": {"c": {}}})}
    have = {"cirdan": recorded("cirdan", {"web": ["a"], "tools": ["b"]})}
    containers = {"cirdan": (ours("web"), ours("web", "exited"), ours("tools", "exited"))}
    runtime = run(want, have, containers)
    assert starts(runtime) == {
        "web": (True, "up"),  # one of its two containers runs: up, like `docker compose ls`
        "tools": (False, "down"),
        "fresh": (False, "new"),
    }
    assert runtime.for_node("cirdan").start == {"web"}
    assert [(s.running, s.containers) for s in runtime.stacks if s.stack == "web"] == [(1, 2)]
    assert starts(run(want, have, containers, start_all=True))["fresh"] == (True, "--start")


def test_a_service_that_was_up_brings_its_new_stack_up() -> None:
    """Moved to another stack on the node, or to another node: migrated, or stateless (an add
    there and a remove here)."""
    want = {
        "cirdan": desired("cirdan", {"new": {"dozzle": {}}}),
        "aragorn": desired("aragorn", {"apps": {"heimdall": {}, "dash": {}}}),
    }
    have = {
        "cirdan": recorded("cirdan", {"old": ["dozzle", "heimdall", "organizr"]}),
        "aragorn": recorded("aragorn", {}),
    }
    containers = {"cirdan": (ours("old"),), "aragorn": ()}
    assert starts(run(want, have, containers)) == {
        "new": (True, "dozzle was up in old"),
        "apps": (True, "heimdall was up on cirdan"),
    }
    # A migrate whose service is renamed on the way.
    want["aragorn"] = desired("aragorn", {"apps": {"dash": {}}})
    plan = Plan(operations=(Migrate(("x",), "cirdan", "aragorn", ("organizr",), ("dash",)),))
    assert starts(run(want, have, containers, plan=plan))["apps"] == (
        True,
        "organizr was up on cirdan",
    )
    # Down where it was: stays down.
    containers["cirdan"] = (ours("old", "exited"),)
    assert starts(run(want, have, containers, plan=plan))["apps"] == (False, "new")


WEB: Services = {
    "web": {
        "heimdall": {"container_name": "heimdall", "ports": [{"published": "80", "target": 80}]},
        "proxy": {"ports": [{"published": "443", "target": 443}]},
        "worker": {"depends_on": {"proxy": {"condition": "service_started"}}},
        "cron": {"depends_on": {"worker": {}}},
    }
}


def web_node(*containers: Container) -> tuple[dict[str, Desired], dict[str, NodeState]]:
    return {"cirdan": desired("cirdan", WEB)}, {"cirdan": recorded("cirdan", {"web": ["proxy"]})}


def test_containers_in_the_way_are_stopped_and_others_left_alone() -> None:
    want, have = web_node()
    containers = {
        "cirdan": (
            ours("web"),
            Container("heimdall", "exited", image="lscr.io/heimdall"),  # the name: renamed
            Container("nginx", "running", ports=(Port("tcp", 80, "0.0.0.0"),)),
            Container("old-nginx", "exited", ports=(Port("tcp", 443),)),  # stopped: no port held
            Container("other", "running", image="lscr.io/heimdall", ports=(Port("tcp", 81),)),
            Container("loopback", "running", ports=(Port("tcp", 443, "127.0.0.1"),)),
        )
    }
    runtime = run(want, have, containers)
    assert [(s.container.name, s.rename, s.why) for s in runtime.stops] == [
        ("heimdall", True, ("has the container name heimdall uses",)),
        ("loopback", False, ("holds 443/tcp, which proxy publishes",)),
        ("nginx", False, ("holds 80/tcp, which heimdall publishes",)),
    ]
    # All of them are listed; the same image, or another port, is no conflict.
    assert {c.name for c in runtime.unmanaged["cirdan"]} == {
        "heimdall",
        "loopback",
        "nginx",
        "old-nginx",
        "other",
    }
    assert runtime.conflicts == 3


def test_a_port_held_outside_docker_defers_the_service_and_its_dependents() -> None:
    want, have = web_node()
    containers = {
        "cirdan": (
            ours("web"),
            Container(
                "web-proxy-1", "running", project="web", ports=(Port("tcp", 443),), managed=True
            ),
        )
    }
    listeners: dict[str, tuple[Listener, ...] | None] = {
        "cirdan": (
            Listener(Port("tcp", 80, "0.0.0.0"), "nginx"),
            Listener(Port("tcp", 443, "0.0.0.0"), ""),  # ours, published by a running container
            Listener(Port("tcp", 8080), "docker-proxy"),
        )
    }
    runtime = run(want, have, containers, listeners)
    assert [(d.services, d.why) for d in runtime.defers] == [
        (("heimdall",), ("80/tcp is held by nginx outside Docker",))
    ]
    listeners["cirdan"] = (Listener(Port("tcp", 443, "10.0.0.5"), "haproxy"),)
    containers["cirdan"] = (ours("web"),)
    deferred = run(want, have, containers, listeners).for_node("cirdan").defer
    assert deferred == ("cron", "proxy", "worker")
    assert run(want, have, containers, {"cirdan": None}).no_port_check == ("cirdan",)


def test_a_project_from_elsewhere_with_a_stacks_name_is_taken_down() -> None:
    want = {
        "cirdan": desired(
            "cirdan",
            {
                "media": {
                    "sonarr": {
                        "volumes": [
                            {"type": "bind", "source": f"{APPDATA}/sonarr", "target": "/config"}
                        ]
                    }
                }
            },
        )
    }
    have = {"cirdan": recorded("cirdan", {})}
    elsewhere = Container(
        "media-sonarr-1",
        "running",
        project="media",
        service="sonarr",
        files=("/opt/media/compose.yml",),
        working_dir="/opt/media",
        binds=(("/config", "/opt/media/sonarr"),),
    )
    other_name = Container("x", "running", project="unrelated", files=("/opt/u/compose.yml",))
    runtime = run(want, have, {"cirdan": (elsewhere, other_name)})
    assert starts(runtime) == {"media": (True, "up")}  # the project of that name is up
    assert runtime.take_down == (
        TakeDown(
            "cirdan",
            "media",
            "/opt/media",
            ("/opt/media/compose.yml",),
            True,
            ("sonarr",),
            ("sonarr",),
            (),
            (
                f"sonarr: /config is /opt/media/sonarr there, {APPDATA}/sonarr in the repo; "
                "the data is not copied",
            ),
        ),
    )
    assert runtime.blocked == ()
    gone = run(want, have, {"cirdan": (elsewhere,)}, missing={"cirdan": frozenset(elsewhere.files)})
    assert "its files are gone" in gone.blocked[0]


def test_conflicts_of_stacks_that_stay_down_are_only_noted() -> None:
    want, have = web_node()
    nginx = Container("nginx", "running", ports=(Port("tcp", 80),))
    down = run(want, have, {"cirdan": (ours("web", "exited"), nginx)})
    assert down.conflicts == 0
    assert down.later == (
        "cirdan: container nginx holds 80/tcp, which heimdall publishes (when web is started)",
    )
    idle = run(want, have, {"cirdan": (ours("web"), nginx)}, work=set())
    assert idle.conflicts == 0 and "when the node is next applied" in idle.later[0]


def test_a_changed_network_with_other_stacks_attached_blocks_apply() -> None:
    def with_proxy(state: NodeState, stack: str, config: str) -> NodeState:
        return replace(state, networks={"proxy": Network("proxy", "proxy", stack, config)})

    want = desired("bilbo", {"homelab-bilbo": {"npm": {}}, "tools": {"x": {}}})
    want = replace(want, state=with_proxy(want.state, "homelab-bilbo", '{"ipam":{}}'))
    have = with_proxy(
        recorded("bilbo", {"docker": ["npm"], "tools": ["x"]}), "docker", '{"ipam":{"a":1}}'
    )
    mine = replace(ours("docker"), networks=("proxy",))  # its stack goes down first: fine
    other = replace(ours("tools"), name="tools-x-1", networks=("proxy",))
    loose = Container("by-hand", "running", networks=("proxy", "bridge"))
    runtime = run({"bilbo": want}, {"bilbo": have}, {"bilbo": (mine, other, loose)})
    (in_use,) = runtime.networks
    assert in_use == NetworkInUse("bilbo", "proxy", "homelab-bilbo", ("tools-x-1", "by-hand"))
    assert "docker network disconnect proxy CONTAINER" in runtime.blocked[0]

    assert run({"bilbo": want}, {"bilbo": have}, {"bilbo": (mine,)}).networks == ()
    same = replace(have, networks={"proxy": replace(have.networks["proxy"], config='{"ipam":{}}')})
    assert run({"bilbo": want}, {"bilbo": same}, {"bilbo": (other, loose)}).networks == ()


def test_containers_list_their_networks() -> None:
    (c,) = parse_containers([row("a", networks={"proxy": {}, "bridge": {}})], HOME)
    assert c.networks == ("bridge", "proxy")
