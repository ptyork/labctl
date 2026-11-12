"""The port map: who holds which host port on a node, and what clashes."""

from __future__ import annotations

from labctl.ports import (
    CONTAINER,
    LISTENING,
    NOT_IN_CONFIG,
    NOT_RUNNING,
    PROCESS,
    RUNNING,
    SERVICE,
    PortUse,
    port_map,
)
from labctl.runtime import Container, Listener, Port

TCP80, TCP443 = Port("tcp", 80), Port("tcp", 443)


def ours(service: str, *ports: Port, up: bool = True) -> Container:
    state = "running" if up else "exited"
    return Container(
        f"x-{service}-1", state, project="x", service=service, ports=ports, managed=True
    )


def other(name: str, *ports: Port) -> Container:
    return Container(name, "running", ports=ports)


def summary(uses: list[PortUse]) -> list[tuple[str, str, str, str, tuple[str, ...]]]:
    return [(str(u.port), u.name, u.kind, u.state, u.clashes) for u in uses]


def test_a_running_service_is_one_row_and_clashes_with_nothing() -> None:
    uses = port_map(
        "n",
        {("x", "web"): (TCP80,)},
        [ours("web", TCP80), other("foo", TCP443)],
        [Listener(Port("tcp", 80, "0.0.0.0"), "docker-proxy"), Listener(Port("tcp", 22), "sshd")],
    )
    assert summary(uses) == [
        ("22/tcp", "sshd", PROCESS, LISTENING, ()),
        ("80/tcp", "web", SERVICE, RUNNING, ()),
        ("443/tcp", "foo", CONTAINER, RUNNING, ()),
    ]


def test_a_wanted_port_held_by_something_else_clashes() -> None:
    uses = port_map(
        "n",
        {("x", "web"): (TCP80,), ("x", "proxy"): (TCP443,)},
        [other("foo", TCP443), ours("web", up=False)],
        [Listener(Port("tcp", 80, "127.0.0.1"), "")],
    )
    assert summary(uses) == [
        ("80/tcp", "web", SERVICE, NOT_RUNNING, ("a process",)),
        ("80/tcp on 127.0.0.1", "", PROCESS, LISTENING, ("web",)),
        ("443/tcp", "foo", CONTAINER, RUNNING, ("proxy",)),
        ("443/tcp", "proxy", SERVICE, NOT_RUNNING, ("container foo",)),
    ]


def test_two_services_wanting_one_port_clash() -> None:
    uses = port_map("n", {("x", "a"): (TCP80,), ("y", "b"): (TCP80,)}, [ours("a", TCP80)], [])
    assert [(u.name, u.state, u.clashes) for u in uses] == [
        ("a", RUNNING, ("b",)),
        ("b", NOT_RUNNING, ("a",)),
    ]


def test_addresses_that_do_not_overlap_do_not_clash() -> None:
    declared = {("x", "web"): (Port("tcp", 80, "10.0.0.2"),)}
    uses = port_map("n", declared, [], [Listener(Port("tcp", 80, "127.0.0.1"), "nginx")])
    assert all(not use.clashes for use in uses)
    uses = port_map("n", {("x", "dns"): (Port("udp", 53),)}, [], [Listener(TCP80, "nginx")])
    assert all(not use.clashes for use in uses)


def test_a_port_the_lab_config_dropped_is_left_to_apply() -> None:
    uses = port_map("n", {("x", "new"): (TCP80,)}, [ours("old", TCP80)], [])
    assert summary(uses) == [
        ("80/tcp", "old", SERVICE, NOT_IN_CONFIG, ()),
        ("80/tcp", "new", SERVICE, NOT_RUNNING, ()),
    ]


def test_without_the_lab_config_what_runs_is_shown_as_it_is() -> None:
    uses = port_map("n", None, [ours("web", TCP80)], [])
    assert summary(uses) == [("80/tcp", "web", SERVICE, RUNNING, ())]


def test_listeners_on_both_wildcards_are_one_row() -> None:
    listeners = [
        Listener(Port("tcp", 22, "0.0.0.0"), "sshd"),
        Listener(Port("tcp", 22, "::"), "sshd"),
    ]
    assert len(port_map("n", {}, [], listeners)) == 1
