"""What runs on the nodes besides DEPLOY_DIR's files: the containers, and ports held on the host.

Read with the rest of the node (collect.py) and used for two things.

Whether each stack runs after apply. A stack keeps its state: one that is up (any container of its
project running, as `docker compose ls` counts it) comes up with the new files; one that is down,
or new, gets its files only. A service moving in (from another stack, or from another node) that
was up brings its new stack up. --start starts every stack apply writes.

Containers labctl does not manage: everything not run from a stack file in DEPLOY_DIR. They are
listed and left alone (other tools may manage them; the same image is fine), unless they clash with
what apply starts:

- a container named like a service's container_name, or holding a host port a service publishes:
  stopped, its restart policy set to "no" (and renamed, for a name), never removed;
- a compose project elsewhere with the name of one of the node's stacks: taken down, its files
  bring it back on undo. Usually the same stack moving into DEPLOY_DIR; rarely an unrelated one;
- a port held outside Docker (a host service, or a container on the host network): the services
  publishing it are created but not started, so a replacement can be phased in.

Each needs an answer in a terminal: apply resolves all of them, or none (no terminal: it stops).
One it cannot resolve: a network whose definition changes (compose removes and recreates it) with
containers of other stacks attached. Docker refuses to remove it, and `up` would fail with the
stack already stopped; the plan stops and says how to free it.
Pure functions only; collect.py runs the commands.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from labctl.bundle import Desired
from labctl.changes import network_changes
from labctl.planner import Migrate, Plan
from labctl.state import NodeState

# `docker inspect --format`: one JSON object per container, only what labctl uses (no env: it can
# hold secrets).
CONTAINER_FORMAT = (
    '{"name":{{json .Name}},"state":{{json .State.Status}},"image":{{json .Config.Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
    '"dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '"ports":{{json .HostConfig.PortBindings}},"restart":{{json .HostConfig.RestartPolicy}},'
    '"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}}}'
)
# The listener section when the node has no `ss`.
UNAVAILABLE = "-unavailable"

_UP = frozenset({"running", "restarting", "paused"})
_ANY_ADDRESS = frozenset({"", "0.0.0.0", "::", "*"})
# What holds a published port for Docker itself.
_DOCKER_PROCESSES = frozenset({"docker-proxy", "rootlesskit", "rootlessport"})


@dataclass(frozen=True, order=True)
class Port:
    protocol: str
    port: int
    host_ip: str = ""  # "" (or 0.0.0.0, ::, *): every address

    def clashes(self, other: Port) -> bool:
        same = (self.protocol, self.port) == (other.protocol, other.port)
        return same and (
            self.host_ip in _ANY_ADDRESS
            or other.host_ip in _ANY_ADDRESS
            or self.host_ip == other.host_ip
        )

    def __str__(self) -> str:
        where = "" if self.host_ip in _ANY_ADDRESS else f" on {self.host_ip}"
        return f"{self.port}/{self.protocol}{where}"


@dataclass(frozen=True)
class Container:
    name: str
    state: str  # running, exited, created, paused, restarting, dead
    image: str = ""
    project: str = ""  # compose labels; "" when not started by compose
    service: str = ""
    files: tuple[str, ...] = ()
    working_dir: str = ""
    ports: tuple[Port, ...] = ()  # published host ports
    restart: str = "no"  # as `docker update --restart` takes it
    binds: tuple[tuple[str, str], ...] = ()  # (container path, host path)
    managed: bool = False  # run from a stack file in DEPLOY_DIR
    networks: tuple[str, ...] = ()  # the networks it is attached to

    @property
    def up(self) -> bool:
        return self.state in _UP


@dataclass(frozen=True, order=True)
class Listener:
    """A port held on the node's host."""

    port: Port
    process: str = ""  # "" when ss cannot see it (another user's process, without root)


def parse_containers(lines: Iterable[str], deploy_dir: str) -> tuple[Container, ...]:
    """The node's containers from CONTAINER_FORMAT lines."""
    prefix = deploy_dir.rstrip("/") + "/"
    found = []
    for line in lines:
        if not line.strip():
            continue
        row: dict[str, Any] = json.loads(line)
        files = tuple(path for path in _label(row, "files").split(",") if path)
        policy = row.get("restart") or {}
        restart = str(policy.get("Name") or "no")
        if restart == "on-failure" and policy.get("MaximumRetryCount"):
            restart += f":{policy['MaximumRetryCount']}"
        found.append(
            Container(
                name=str(row.get("name", "")).lstrip("/"),
                state=str(row.get("state", "")),
                image=str(row.get("image") or ""),
                project=_label(row, "project"),
                service=_label(row, "service"),
                files=files,
                working_dir=_label(row, "dir"),
                ports=_bindings(row.get("ports") or {}),
                restart=restart,
                binds=tuple(
                    (str(mount.get("Destination", "")), str(mount.get("Source", "")))
                    for mount in row.get("mounts") or []
                    if mount.get("Type") == "bind"
                ),
                managed=bool(files) and all(file.startswith(prefix) for file in files),
                networks=tuple(sorted(row.get("networks") or {})),
            )
        )
    return tuple(sorted(found, key=lambda c: c.name))


def _label(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value in (None, "<no value>") else str(value)


def _bindings(bindings: Mapping[str, Any]) -> tuple[Port, ...]:
    ports: set[Port] = set()
    for key, hosts in bindings.items():
        protocol = key.split("/", 1)[1] if "/" in key else "tcp"
        for host in hosts or []:
            for number in _port_range(str(host.get("HostPort") or "")):
                ports.add(Port(protocol, number, str(host.get("HostIp") or "")))
    return tuple(sorted(ports))


def _port_range(value: str) -> list[int]:
    """ "8080" or "8000-8002"; nothing for "" (Docker picks a port) or anything else."""
    first, _, last = value.partition("-")
    if not first.isdigit() or (last and not last.isdigit()):
        return []
    return list(range(int(first), int(last or first) + 1))


def parse_listeners(lines: Iterable[str]) -> tuple[Listener, ...] | None:
    """`ss -Htulnp` output; None when the node has no ss."""
    found: set[Listener] = set()
    for line in lines:
        if line.strip() == UNAVAILABLE:
            return None
        fields = line.split()
        if len(fields) < 5 or fields[0] not in ("tcp", "udp"):
            continue
        address, _, port = fields[4].rpartition(":")
        if not port.isdigit():
            continue
        address = address.strip("[]").split("%", 1)[0]
        process = re.search(r'users:\(\("([^"]+)"', line)
        found.add(
            Listener(Port(fields[0], int(port), address), process.group(1) if process else "")
        )
    return tuple(sorted(found))


def published(config: Mapping[str, Any]) -> tuple[Port, ...]:
    """The host ports a resolved compose service publishes (none on the host network)."""
    if config.get("network_mode") == "host":
        return ()
    ports = []
    for entry in config.get("ports") or []:
        if isinstance(entry, Mapping):
            for number in _port_range(str(entry.get("published") or "")):
                ports.append(
                    Port(
                        str(entry.get("protocol") or "tcp"), number, str(entry.get("host_ip") or "")
                    )
                )
    return tuple(ports)


#############################################################################


# What apply does about it


#############################################################################


@dataclass(frozen=True)
class StackRun:
    """Whether a stack runs after apply, and why."""

    node: str
    stack: str
    start: bool
    why: str = field(default="", compare=False)  # "up", "new", "down", "--start", or who moves in
    running: int = field(default=0, compare=False)  # its containers running now, of
    containers: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Stop:
    """A container labctl does not manage, in the way of what apply starts."""

    node: str
    container: Container
    rename: bool  # it has a name a service needs
    why: tuple[str, ...]


@dataclass(frozen=True)
class TakeDown:
    """A compose project from outside DEPLOY_DIR with the name of one of the node's stacks."""

    node: str
    project: str
    working_dir: str
    files: tuple[str, ...]
    running: bool
    services: tuple[str, ...]  # its services
    in_repo: tuple[str, ...]  # those of them the repo's stack also has
    missing: tuple[str, ...]  # its files that are gone: it could not be brought back
    data: tuple[str, ...]  # bind mounts that differ from the repo's: data that is not copied


@dataclass(frozen=True)
class Defer:
    """Services created but not started: a port they publish is held outside Docker."""

    node: str
    stack: str
    services: tuple[str, ...]  # the service, then those that depend on it
    why: tuple[str, ...]


@dataclass(frozen=True)
class NetworkInUse:
    """A network apply changes (compose recreates it) with containers of other stacks attached:
    compose cannot remove it, so `up` would fail with the stack already stopped."""

    node: str
    network: str
    stack: str  # the stack that recreates it
    containers: tuple[str, ...]

    def __str__(self) -> str:
        names = ", ".join(self.containers)
        one = len(self.containers) == 1
        return (
            f"{self.node}: network {self.network} changes, so compose recreates it for "
            f"{self.stack}, but {names} (outside {self.stack}) {'is' if one else 'are'} attached "
            f"and compose cannot remove it; disconnect {'it' if one else 'them'} first "
            f"(docker network disconnect {self.network} CONTAINER) or take "
            f"{'it' if one else 'them'} down"
        )


@dataclass(frozen=True)
class NodeRuntime:
    """One node's part, for apply."""

    start: frozenset[str] = frozenset()  # stacks that run after apply
    stops: tuple[Stop, ...] = ()
    take_down: tuple[TakeDown, ...] = ()
    defer: tuple[str, ...] = ()  # services created, not started
    running: frozenset[str] = frozenset()  # projects that are up now


@dataclass(frozen=True)
class Runtime:
    stacks: tuple[StackRun, ...] = ()
    stops: tuple[Stop, ...] = ()
    take_down: tuple[TakeDown, ...] = ()
    defers: tuple[Defer, ...] = ()
    networks: tuple[NetworkInUse, ...] = ()
    # Conflicts of stacks that stay down (or nodes apply does not change): shown, nothing done.
    later: tuple[str, ...] = field(default=(), compare=False)
    unmanaged: Mapping[str, tuple[Container, ...]] = field(default_factory=dict, compare=False)
    running: Mapping[str, frozenset[str]] = field(default_factory=dict, compare=False)
    no_port_check: tuple[str, ...] = field(default=(), compare=False)  # nodes without ss

    @property
    def conflicts(self) -> int:
        return len(self.stops) + len(self.take_down) + len(self.defers)

    @property
    def blocked(self) -> tuple[str, ...]:
        """Conflicts apply cannot resolve."""
        return (
            *(
                f"{down.node}: project {down.project} runs from {down.working_dir}, but its files "
                f"are gone ({', '.join(down.missing)}): nothing could bring it back on undo; take "
                "it down by hand"
                for down in self.take_down
                if down.missing
            ),
            *(str(network) for network in self.networks),
        )

    def for_node(self, node: str) -> NodeRuntime:
        return NodeRuntime(
            start=frozenset(
                stack.stack for stack in self.stacks if stack.node == node and stack.start
            ),
            stops=tuple(stop for stop in self.stops if stop.node == node),
            take_down=tuple(down for down in self.take_down if down.node == node),
            defer=tuple(
                sorted(
                    {
                        service
                        for defer in self.defers
                        if defer.node == node
                        for service in defer.services
                    }
                )
            ),
            running=self.running.get(node, frozenset()),
        )


def analyse(
    desired: Mapping[str, Desired],
    recorded: Mapping[str, NodeState | None],
    plan: Plan,
    *,
    containers: Mapping[str, tuple[Container, ...]],
    listeners: Mapping[str, tuple[Listener, ...] | None],
    missing: Mapping[str, Collection[str]],
    work: Collection[str],
    start_all: bool = False,
) -> Runtime:
    """
    What runs after apply on each node read, and what is in the way.

    Args:
        desired (Mapping[str, Desired]):
            Every node's bundle.
        recorded (Mapping[str, NodeState | None]):
            Every node's current state.
        plan (Plan):
            The plan (migrates bring a stack up when the service was up on the source).
        containers (Mapping[str, tuple[Container, ...]]):
            The containers on each node read; only these nodes are analysed.
        listeners (Mapping[str, tuple[Listener, ...] | None]):
            The ports held on each node's host (None: no ``ss`` there).
        missing (Mapping[str, Collection[str]]):
            The compose files containers name that are gone, per node.
        work (Collection[str]):
            The nodes apply changes; only their stacks are started.
        start_all (bool):
            ``--start``: every stack apply writes runs (default: False).

    Returns:
        Runtime:
            Whether each stack runs, the conflicts apply resolves (stops, projects taken down,
            deferred services), those it cannot (``blocked``), and what is only noted.
    """
    running = {
        node: frozenset(
            container.project for container in found if container.project and container.up
        )
        for node, found in containers.items()
    }
    moving_in = _moving_in(desired, recorded, plan, running)
    stacks: list[StackRun] = []
    stops: dict[tuple[str, str], Stop] = {}
    take_down: list[TakeDown] = []
    defers: list[Defer] = []
    networks: list[NetworkInUse] = []
    later: list[str] = []
    unmanaged: dict[str, tuple[Container, ...]] = {}
    no_port_check: list[str] = []

    for node, here in sorted(containers.items()):
        state = recorded.get(node)
        if node not in desired or state is None:
            continue
        want = desired[node]
        others = tuple(container for container in here if not container.managed)
        if others:
            unmanaged[node] = others

        started: set[str] = set()
        for stack in sorted(want.state.stacks):
            mine = [container for container in here if container.project == stack]
            up = sum(container.up for container in mine)
            arriving = [
                moving_in[(node, s)]
                for s, service in sorted(want.state.services.items())
                if service.stack == stack and (node, s) in moving_in
            ]
            if start_all:
                start, why = True, "--start"
            elif stack in running[node]:
                start, why = True, "up"
            elif arriving:
                start, why = True, arriving[0]
            else:
                start, why = False, ("new" if stack not in state.stacks and not mine else "down")
            stacks.append(StackRun(node, stack, start, why, up, len(mine)))
            if start and node in work:
                started.add(stack)

        route = partial(_route, node, frozenset(started), node in work, later)

        # Compose projects from elsewhere named like one of the node's stacks.
        foreign: dict[str, list[Container]] = {}
        for c in others:
            if c.project in want.state.stacks:
                foreign.setdefault(c.project, []).append(c)
        for project, found in sorted(foreign.items()):
            services = tuple(
                sorted({container.service for container in found if container.service})
            )
            repo = {s for s, svc in want.state.services.items() if svc.stack == project}
            in_repo = tuple(service for service in services if service in repo)
            gone = missing.get(node, ())
            down = TakeDown(
                node,
                project,
                found[0].working_dir,
                found[0].files,
                any(container.up for container in found),
                services,
                in_repo,
                tuple(file for file in found[0].files if file in gone),
                _data_moved(want, found, in_repo),
            )
            if route(project, f"compose project {project} also runs from {down.working_dir}"):
                take_down.append(down)

        # Containers in the way of a service, and ports held outside Docker.
        loose = [other for other in others if other.project not in foreign]
        docker_ports = {
            (port.protocol, port.port)
            for container in here
            if container.up
            for port in container.ports
        }
        found_listeners = listeners.get(node)
        if found_listeners is None:
            no_port_check.append(node)
        held = [
            listener
            for listener in found_listeners or ()
            if listener.process not in _DOCKER_PROCESSES
            and (listener.port.protocol, listener.port.port) not in docker_ports
        ]
        for name, service in sorted(want.state.services.items()):
            config = want.service_config(name)
            ports = published(config)
            for squatter in loose:
                clashes: list[str] = []
                rename = (
                    bool(config.get("container_name")) and squatter.name == config["container_name"]
                )
                if rename:
                    clashes.append(f"has the container name {name} uses")
                if squatter.up:
                    clashes += [
                        f"holds {port}, which {name} publishes"
                        for port in ports
                        if any(port.clashes(other) for other in squatter.ports)
                    ]
                if clashes and route(
                    service.stack, f"container {squatter.name} {'; '.join(clashes)}"
                ):
                    before = stops.get((node, squatter.name))
                    stops[(node, squatter.name)] = Stop(
                        node,
                        squatter,
                        rename or bool(before and before.rename),
                        (*(before.why if before else ()), *clashes),
                    )
            holders = [
                f"{port} is held by {listener.process or 'a process'} outside Docker"
                for port in ports
                for listener in held
                if port.clashes(listener.port)
            ]
            if holders and route(service.stack, f"{name}: {'; '.join(holders)}"):
                defers.append(
                    Defer(node, service.stack, (name, *_dependents(want, name)), tuple(holders))
                )

        # A changed network is recreated; containers still attached to it (not of the stack that
        # recreates it, nor of a stack apply takes down first) keep compose from removing it.
        going = set(state.stacks) - set(want.state.stacks)
        for change in network_changes(state, want.state):
            attached = tuple(
                container.name
                for container in here
                if change.name in container.networks
                and container.project not in {*going, change.stack}
            )
            message = f"network {change.name} changes; {', '.join(attached)} attached"
            if attached and route(change.stack, message):
                networks.append(NetworkInUse(node, change.name, change.stack, attached))

    return Runtime(
        stacks=tuple(stacks),
        stops=tuple(stops[key] for key in sorted(stops)),
        take_down=tuple(take_down),
        defers=tuple(defers),
        networks=tuple(networks),
        later=tuple(later),
        unmanaged=unmanaged,
        running=running,
        no_port_check=tuple(no_port_check),
    )


def _route(
    node: str, started: Collection[str], changes: bool, later: list[str], stack: str, message: str
) -> bool:
    """True when apply resolves a conflict (its stack is started); else it is noted for later."""
    if stack in started:
        return True
    when = f"when {stack} is started" if changes else "when the node is next applied"
    later.append(f"{node}: {message} ({when})")
    return False


def _moving_in(
    desired: Mapping[str, Desired],
    recorded: Mapping[str, NodeState | None],
    plan: Plan,
    running: Mapping[str, frozenset[str]],
) -> dict[tuple[str, str], str]:
    """(node, service) -> where it was up, for services that move into another stack while up:
    from another stack on the node, or from another node (migrated, or stateless and re-added)."""
    found: dict[tuple[str, str], str] = {}

    def up_on(node: str, service: str) -> str:
        state = recorded.get(node)
        before = state.services.get(service) if state else None
        if before and before.stack in running.get(node, frozenset()):
            return before.stack
        return ""

    for node, want in desired.items():
        state = recorded.get(node)
        for name, service in want.state.services.items():
            before = state.services.get(name) if state else None
            if before is not None:
                if before.stack != service.stack and up_on(node, name):
                    found[(node, name)] = f"{name} was up in {before.stack}"
                continue
            for other in sorted(recorded):
                leaving = other in desired and name not in desired[other].state.services
                if other != node and leaving and up_on(other, name):
                    found[(node, name)] = f"{name} was up on {other}"
                    break
    for op in plan.operations:
        if isinstance(op, Migrate):
            source = next((service for service in op.removed if up_on(op.source, service)), "")
            for name in op.added:
                if source:
                    found.setdefault((op.target, name), f"{source} was up on {op.source}")
    return found


def _dependents(want: Desired, service: str) -> tuple[str, ...]:
    """Services of the same stack that depend on ``service``, directly or not."""
    stack = want.state.services[service].stack
    names = [s for s, svc in sorted(want.state.services.items()) if svc.stack == stack]
    found: list[str] = []
    pending = [service]
    while pending:
        current = pending.pop()
        for name in names:
            depends = want.service_config(name).get("depends_on") or {}
            if current in depends and name not in found and name != service:
                found.append(name)
                pending.append(name)
    return tuple(found)


def _data_moved(
    want: Desired, found: Iterable[Container], in_repo: Collection[str]
) -> tuple[str, ...]:
    """Where a project from elsewhere mounts other host paths than the repo: data left there."""
    notes = []
    for container in found:
        if container.service not in in_repo:
            continue
        repo = {
            str(mount.get("target")): str(mount.get("source"))
            for mount in want.service_config(container.service).get("volumes") or []
            if mount.get("type") == "bind"
        }
        for target, source in container.binds:
            if repo.get(target, source) != source:
                notes.append(
                    f"{container.service}: {target} is {source} there, {repo[target]} in the repo; "
                    "the data is not copied"
                )
    return tuple(notes)
