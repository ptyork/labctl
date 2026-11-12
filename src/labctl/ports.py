"""Who holds which host port on a node: the lab config's services, and what runs there now.

The lab config's services publish ports (compose's resolved ``ports:``; a service on the host
network publishes none: its ports are not known, and taken to be fine). On the node, a running
container holds the ports it publishes, and anything else listening on the host (a host service, or
a container on the host network) shows in ``ss``.

Each use of a port is a row. A service's port and its running container's are one row. A running
container from DEPLOY_DIR whose service no longer publishes that port is "not in the lab config":
apply removes it, so it clashes with nothing.

A clash needs the lab config: two of its services want the same port, or one of its services wants
a port something else holds now and it is not the one holding it. What already runs side by side
is never a clash (it plainly works). Pure functions only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from itertools import combinations

from labctl.bundle import Desired
from labctl.envfile import PLACEHOLDER_PREFIX
from labctl.runtime import DOCKER_PROCESSES, Container, Listener, Port, is_any_address, published
from labctl.state import NodeState

# What uses a port.
SERVICE = "labctl"  # a service of the lab config, or a container run from DEPLOY_DIR
CONTAINER = "container"  # a container labctl does not manage
PROCESS = "host"  # listening on the host, outside Docker's published ports

# How.
RUNNING = "running"
NOT_RUNNING = "not running"  # in the lab config; no running container holds it
NOT_IN_CONFIG = "not in lab config"  # a running container from DEPLOY_DIR the lab config dropped
LISTENING = "listening"

# (stack, service) -> the host ports it publishes.
Declared = Mapping[tuple[str, str], tuple[Port, ...]]


@dataclass(frozen=True)
class PortUse:
    """One use of a host port on a node."""

    node: str
    port: Port
    name: str  # the service, the container, or the process ("": one ss cannot name)
    kind: str  # SERVICE, CONTAINER or PROCESS
    state: str  # RUNNING, NOT_RUNNING, NOT_IN_CONFIG or LISTENING
    clashes: tuple[str, ...] = ()  # what else wants or holds the port

    @property
    def owner(self) -> str:
        """How a clash names it: "plex", "container foo", "process sshd", "a process"."""
        if self.kind == SERVICE:
            return self.name
        if self.kind == CONTAINER:
            return f"container {self.name}"
        return f"process {self.name}" if self.name else "a process"

    @property
    def wanted(self) -> bool:
        """A port the lab config publishes."""
        return self.kind == SERVICE and self.state in (RUNNING, NOT_RUNNING)


def declared_ports(desired: Desired) -> dict[tuple[str, str], tuple[Port, ...]]:
    """The host ports each of the node's services publishes, by (stack, service)."""
    return {
        (service.stack, name): ports
        for name, service in sorted(desired.state.services.items())
        if (ports := published(desired.service_config(name)))
    }


@dataclass(frozen=True)
class PortMove:
    """A service whose published host ports change: another node, or another port."""

    service: str
    before: tuple[tuple[str, tuple[Port, ...]], ...]  # (node, its ports), by node
    after: tuple[tuple[str, tuple[Port, ...]], ...]


def moved_ports(
    desired: Mapping[str, NodeState],
    recorded: Mapping[str, NodeState | None],
    nodes: Iterable[str],
) -> list[PortMove]:
    """
    The services whose published host ports change on ``nodes`` (only those nodes are shown, so a
    service running on several nodes lists the ones this plan changes): what to check by hand
    afterwards (proxy hosts, DNS, dashboards point at node and port; each app keeps them its own
    way, so labctl cannot fix them).

    Args:
        desired (Mapping[str, NodeState]):
            Every node as the lab config wants it.
        recorded (Mapping[str, NodeState | None]):
            Every node as it is (or as of its tag).
        nodes (Iterable[str]):
            The nodes the plan changes.

    Returns:
        list[PortMove]:
            By service.
    """
    named = set(nodes)

    def published_by(
        states: Mapping[str, NodeState | None],
    ) -> dict[str, dict[str, tuple[Port, ...]]]:
        """On the nodes the plan changes: service -> node -> its ports."""
        found: dict[str, dict[str, tuple[Port, ...]]] = {}
        for node, state in states.items():
            if node not in named:
                continue
            for name, service in (state.services if state else {}).items():
                ports = tuple(sorted(map(_plain, published(json.loads(service.config or "{}")))))
                if ports:
                    found.setdefault(name, {})[node] = ports
        return found

    before, after = published_by(recorded), published_by(desired)
    return [
        PortMove(name, tuple(sorted(before.get(name, {}).items())), tuple(sorted(was_now.items())))
        for name in sorted(before.keys() | after.keys())
        if (was_now := after.get(name, {})) != before.get(name, {})
    ]


def lab_clashes(desired: Desired) -> list[str]:
    """
    The ports two or more of a node's services publish: the port map with nothing running, so
    only the lab config's own clashes are left (validate, and plan, which blocks on them).

    Args:
        desired (Desired):
            The node's bundle.

    Returns:
        list[str]:
            One line per port: "NODE: 80/tcp is published by proxy and web (on 10.0.0.2)".
    """
    by_port: dict[tuple[int, str], list[str]] = {}
    for use in port_map(desired.node, declared_ports(desired), (), ()):
        if use.clashes:
            where = _where(use.port.host_ip)
            by_port.setdefault((use.port.port, use.port.protocol), []).append(use.name + where)
    return [
        f"{desired.node}: {port}/{protocol} is published by {', '.join(names[:-1])} and {names[-1]}"
        for (port, protocol), names in sorted(by_port.items())
    ]


def port_map(
    node: str,
    declared: Declared | None,
    containers: Iterable[Container],
    listeners: Iterable[Listener],
) -> list[PortUse]:
    """
    Every use of a host port on one node, with its clashes.

    Args:
        node (str):
            The node.
        declared (Declared | None):
            What the lab config's services publish (None: the lab config could not be read; the
            node's running containers are then taken as they are, and nothing clashes).
        containers (Iterable[Container]):
            The node's containers.
        listeners (Iterable[Listener]):
            The ports held on its host.

    Returns:
        list[PortUse]:
            The uses, by port.
    """
    wanted = {key: set(map(_plain, ports)) for key, ports in (declared or {}).items()}
    held: set[tuple[tuple[str, str], Port]] = set()
    uses: list[PortUse] = []
    docker_ports: set[tuple[str, int]] = set()
    for container in containers:
        if not container.up:
            continue
        key = (container.project, container.service)
        for port in map(_plain, container.ports):
            docker_ports.add((port.protocol, port.port))
            if not container.managed:
                uses.append(PortUse(node, port, container.name, CONTAINER, RUNNING))
            elif declared is None or port in wanted.get(key, ()):
                held.add((key, port))
                uses.append(PortUse(node, port, container.service, SERVICE, RUNNING))
            else:
                uses.append(PortUse(node, port, container.service, SERVICE, NOT_IN_CONFIG))
    for key, ports in sorted(wanted.items()):
        for port in sorted(ports):
            if (key, port) not in held:
                uses.append(PortUse(node, port, key[1], SERVICE, NOT_RUNNING))
    seen: set[tuple[Port, str]] = set()
    for listener in listeners:
        port = _plain(listener.port)
        docker = (port.protocol, port.port) in docker_ports
        if docker or listener.process in DOCKER_PROCESSES or (port, listener.process) in seen:
            continue
        seen.add((port, listener.process))
        uses.append(PortUse(node, port, listener.process, PROCESS, LISTENING))
    return sorted(_with_clashes(uses), key=lambda use: (use.port.port, use.port.protocol))


def _with_clashes(uses: list[PortUse]) -> list[PortUse]:
    """The uses, each with the others it clashes with (see the module's docstring)."""
    clashes: dict[int, list[str]] = {}
    for (i, one), (j, other) in combinations(enumerate(uses), 2):
        if not one.port.clashes(other.port) or (one.kind, one.name) == (other.kind, other.name):
            continue
        both_wanted = one.wanted and other.wanted
        none_leaving = NOT_IN_CONFIG not in (one.state, other.state)
        if none_leaving and (both_wanted or NOT_RUNNING in (one.state, other.state)):
            clashes.setdefault(i, []).append(other.owner)
            clashes.setdefault(j, []).append(one.owner)
    return [
        replace(use, clashes=tuple(clashes[i])) if i in clashes else use
        for i, use in enumerate(uses)
    ]


def _where(host_ip: str) -> str:
    """The suffix for a port on one address, " (on 10.0.0.2)"; "" on all of them."""
    if is_any_address(host_ip):
        return ""
    # validate leaves _HOST_IP_ a placeholder
    return " (on the host IP)" if host_ip.startswith(PLACEHOLDER_PREFIX) else f" (on {host_ip})"


def _plain(port: Port) -> Port:
    """The port with every-address spellings (0.0.0.0, ::, *) as ""."""
    return replace(port, host_ip="") if is_any_address(port.host_ip) else port
