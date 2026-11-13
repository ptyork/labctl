"""A node's stacks as they are on the node, for the node commands.

DEPLOY_DIR is where the node's ~/.labctl-home says the last apply put the files, else node.env's
value (before the first apply). The stacks are found on the node with the same rule plan uses on
the bundle: the node's own files decide, so this is right before the first apply, after `down`,
and after edits made on the node.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from labctl import docker
from labctl.bundle import Location, discover_stacks
from labctl.collect import projects_in
from labctl.compose import ComposeError
from labctl.envfile import preview_env
from labctl.layout import HOME_POINTER
from labctl.remote import Remote, execute
from labctl.settings import Settings, SettingsError
from labctl.state import Stack


@dataclass(frozen=True)
class Deployed:
    """Where a node's stacks are and what they are."""

    node: str
    location: Location
    stacks: tuple[Stack, ...]
    not_stacks: dict[str, str] = field(default_factory=dict)  # yml -> why it is not a stack

    @property
    def deploy_dir(self) -> str:
        return self.location.root

    def label(self, stack: Stack) -> str:
        """`node` with a single stack, `node/stack` with several."""
        return f"{self.node}/{stack.name}" if len(self.stacks) > 1 else self.node

    def select(self, selector: str) -> tuple[Stack, ...]:
        """All stacks for "", else the one whose name is ``selector`` or ends in -``selector``."""
        if not selector:
            return self.stacks
        matches = [stack for stack in self.stacks if stack.name == selector]
        matches = matches or [stack for stack in self.stacks if stack.name.endswith("-" + selector)]
        if len(matches) != 1:
            problem = "matches several stacks" if matches else "is not a stack"
            names = ", ".join(stack.name for stack in self.stacks)
            raise SettingsError(f"{self.node}/{selector} {problem}; stacks: {names}")
        return tuple(matches)


def deploy_dir_on(settings: Settings, node: str, remote: Remote) -> str:
    """Where the node's DEPLOY_DIR is: where its ~/.labctl-home says the last apply put it, else
    node.env's value (before the first apply); "" when neither says."""
    pointer = execute(["sh", "-c", f'cat "$HOME/{HOME_POINTER}"'], "/", remote)
    home = pointer.stdout.strip() if pointer.code == 0 else ""
    return home or preview_env(settings.nodes_dir, node).get("DEPLOY_DIR", "")


def deployed(settings: Settings, node: str, remote: Remote) -> Deployed:
    home = deploy_dir_on(settings, node, remote)
    if not home:
        raise SettingsError(f"{node}: DEPLOY_DIR is not set (run 'labctl plan {node}' to set it)")
    location = Location(home, remote)
    stacks, not_stacks = discover_stacks(location)
    if not stacks:
        why = "".join(f"\n  {name}: {error.splitlines()[-1]}" for name, error in not_stacks.items())
        raise SettingsError(f"{node}: no stack files in {home}{why}")
    return Deployed(node, location, tuple(stacks), not_stacks)


@dataclass(frozen=True)
class ServiceStatus:
    stack: str
    service: str
    state: str  # running, exited, ...; "" when the service has no container
    status: str  # e.g. "Up 3 hours (healthy)"
    image: str
    configured: bool  # False: a container left from a service no longer in the compose files
    restarts: int = 0  # how often Docker restarted its container, by its restart policy
    looping: bool = False  # it looks like a restart loop (see restart_loop)


def stack_services(location: Location, stack: Stack) -> list[str]:
    """The stack's services, as its files define them."""
    return location.compose(*docker.stack_args(stack), "config", "--services").split()


def read_services(location: Location, stack: Stack) -> list[ServiceStatus]:
    """The stack's services and their containers."""
    rows = ps_rows(location.compose(*docker.stack_args(stack), "ps", "--all", "--format", "json"))
    names = [str(row["Name"]) for row in rows if row.get("Name")]
    restarts = parse_restarts(location.run("sh", "-c", docker.RESTARTS, "sh", *names).stdout)
    return statuses(stack.name, stack_services(location, stack), rows, restarts)


# A restart loop: restarting now, or restarted this often and up for less than this long.
LOOP_RESTARTS = 3
LOOP_WINDOW = 300  # seconds


def parse_restarts(output: str) -> dict[str, tuple[int, int]]:
    """docker.RESTARTS's output: container name -> (restarts, seconds up)."""
    lines = output.split("\n")
    if not lines or not lines[0].strip().isdigit():
        return {}
    now = int(lines[0].strip())
    found = {}
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 3 or not fields[1].isdigit():
            continue
        try:
            started = datetime.strptime(fields[2][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        found[fields[0].lstrip("/")] = (int(fields[1]), max(now - int(started.timestamp()), 0))
    return found


def restart_loop(state: str, restarts: int, up: int) -> bool:
    """Whether a container looks like it is in a restart loop: Docker is restarting it now, or it
    restarted at least LOOP_RESTARTS times and has been up less than LOOP_WINDOW seconds."""
    if state == "restarting":
        return True
    return state == "running" and restarts >= LOOP_RESTARTS and up < LOOP_WINDOW


def unbacked_projects(deployed: Deployed) -> dict[str, str]:
    """Projects running from DEPLOY_DIR that no stack file there defines: name -> compose's
    status (e.g. "running(3)"). Their files were removed or renamed under them."""
    try:
        rows: list[dict[str, Any]] = json.loads(
            deployed.location.compose("ls", "--all", "--format", "json") or "[]"
        )
    except ComposeError:
        return {}
    known = {stack.name for stack in deployed.stacks}
    status = {str(row["Name"]): str(row.get("Status", "")) for row in rows}
    return {
        name: status[name] for name in projects_in(rows, deployed.deploy_dir) if name not in known
    }


def statuses(
    stack: str,
    configured: list[str],
    containers: list[dict[str, Any]],
    restarts: Mapping[str, tuple[int, int]] | None = None,
) -> list[ServiceStatus]:
    """
    Each service of a stack with its container (configured first, then containers left over).

    Args:
        stack (str):
            The stack.
        configured (list[str]):
            The services its files define.
        containers (list[dict[str, Any]]):
            `compose ps` rows.
        restarts (Mapping[str, tuple[int, int]] | None):
            Container name -> (restarts, seconds up), from parse_restarts (default: none).

    Returns:
        list[ServiceStatus]:
            By service name.
    """
    by_service = {container["Service"]: container for container in containers}
    restarts = restarts or {}

    def status(name: str, container: Mapping[str, Any], in_files: bool) -> ServiceStatus:
        state = str(container.get("State", ""))
        count, up = restarts.get(str(container.get("Name", "")), (0, 0))
        return ServiceStatus(
            stack,
            name,
            state,
            str(container.get("Status", "")),
            str(container.get("Image", "")),
            in_files,
            count,
            restart_loop(state, count, up),
        )

    found = [status(name, by_service.get(name, {}), True) for name in configured]
    found += [status(name, c, False) for name, c in by_service.items() if name not in configured]
    return sorted(found, key=lambda s: s.service)


def ps_rows(output: str) -> list[dict[str, Any]]:
    """`ps --format json` output: one object per line (current compose) or one array (older)."""
    text = output.strip()
    if text.startswith("["):
        rows: list[dict[str, Any]] = json.loads(text)
        return rows
    return [json.loads(line) for line in text.splitlines() if line.strip()]


##########################################################################

# Containers before and after: what compose recreated, created or removed

##########################################################################


@dataclass(frozen=True)
class Seen:
    """A service's containers at one moment (several when it is scaled)."""

    ids: frozenset[str]
    up: bool  # any of them running (or restarting)


Snapshot = dict[tuple[str, str], Seen]  # (project, service) -> its containers


def snapshot(output: str) -> Snapshot:
    """The compose containers in docker.snapshot's output (others have no service)."""
    found: dict[tuple[str, str], tuple[set[str], bool]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        row: dict[str, Any] = json.loads(line)
        if not row.get("service"):
            continue
        ids, up = found.setdefault((row["project"], row["service"]), (set(), False))
        ids.add(row["id"])
        if row["state"] in ("running", "restarting"):
            found[(row["project"], row["service"])] = (ids, True)
    return {key: Seen(frozenset(ids), up) for key, (ids, up) in found.items()}


def read_snapshot(remote: Remote, project: str = "") -> Snapshot:
    """The node's compose containers now (one project's, when named)."""
    result = execute(docker.snapshot(project), "/", remote)
    if result.code:
        raise ComposeError(f"{remote.host}: docker ps: {result.stderr.strip()}")
    return snapshot(result.stdout)


@dataclass(frozen=True)
class ContainerChanges:
    """What became of one project's services between two snapshots."""

    recreated: tuple[str, ...] = ()
    created: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.recreated or self.created or self.removed)

    def __str__(self) -> str:
        """``recreated dozzle; created x``; more than three names are counted."""
        parts = []
        for what, names in (
            ("recreated", self.recreated),
            ("created", self.created),
            ("removed", self.removed),
        ):
            if len(names) > 3:
                parts.append(f"{what} {len(names)} ({', '.join(names[:3])}, …)")
            elif names:
                parts.append(f"{what} {', '.join(names)}")
        return "; ".join(parts)


def container_changes(
    before: Snapshot,
    after: Snapshot,
    project: str,
    came_from: Collection[str] = (),
    staying: Collection[str] = (),
) -> ContainerChanges:
    """
    What became of ``project``'s services from ``before`` to ``after``.

    Args:
        before (Snapshot):
            The containers before.
        after (Snapshot):
            The containers now.
        project (str):
            The compose project (stack) to compare.
        came_from (Collection[str]):
            Projects a service may have moved in from: its container there counts as its old one.
        staying (Collection[str]):
            Services that left the project for another, so are not called removed.

    Returns:
        ContainerChanges: recreated (new container), created (none before), removed (none now).
    """
    now = {service: seen for (name, service), seen in after.items() if name == project}
    was = {service: seen for (name, service), seen in before.items() if name == project}
    elsewhere = {service: seen for (name, service), seen in before.items() if name in came_from}
    recreated, created = [], []
    for service, seen in sorted(now.items()):
        prior = was.get(service) or elsewhere.get(service)
        if prior is None:
            created.append(service)
        elif seen.ids != prior.ids:
            recreated.append(service)
    removed = [service for service in sorted(was) if service not in now and service not in staying]
    return ContainerChanges(tuple(recreated), tuple(created), tuple(removed))


def not_up_again(before: Snapshot, after: Snapshot) -> list[str]:
    """Services up in ``before`` that are not up in ``after`` (in any project)."""
    up_now = {service for (_, service), seen in after.items() if seen.up}
    return sorted({service for (_, service), seen in before.items() if seen.up} - up_now)
