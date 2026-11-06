"""A node's stacks as they are on the node, for the node commands.

DEPLOY_DIR is where the node's ~/.labctl-home says the last apply put the files, else node.env's
value (before the first apply). The stacks are found on the node with the same rule plan uses on
the bundle: the node's own files decide, so this is right before the first apply, after `down`,
and after edits made on the node.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any

from labctl.bundle import Location, discover_stacks, stack_args
from labctl.collect import HOME_POINTER, projects_in
from labctl.compose import ComposeError
from labctl.envfile import preview_env
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


def deployed(settings: Settings, node: str, remote: Remote) -> Deployed:
    pointer = execute(["sh", "-c", f'cat "$HOME/{HOME_POINTER}"'], "/", remote)
    home = pointer.stdout.strip() if pointer.code == 0 else ""
    home = home or preview_env(settings.nodes_dir, node).get("DEPLOY_DIR", "")
    if not home:
        raise SettingsError(f"{node}: DEPLOY_DIR is not set (run 'labctl plan {node}' to set it)")
    location = Location(home, remote)
    stacks, not_stacks = discover_stacks(location)
    if not stacks:
        why = "".join(f"\n  {name}: {error.splitlines()[-1]}" for name, error in not_stacks.items())
        raise SettingsError(f"{node}: no stack files in {home}{why}")
    return Deployed(node, location, tuple(stacks), not_stacks)


def compose_command(deploy_dir: str, stack: Stack, *args: str) -> str:
    """Shell command running docker compose on one stack (for output streamed to the terminal)."""
    missing = shlex.quote(f"DEPLOY_DIR {deploy_dir} does not exist")
    return f"cd {shlex.quote(deploy_dir)} 2>/dev/null || {{ echo {missing} >&2; exit 3; }}; " + (
        shlex.join(["docker", "compose", *stack_args(stack), *args])
    )


@dataclass(frozen=True)
class ServiceStatus:
    stack: str
    service: str
    state: str  # running, exited, ...; "" when the service has no container
    status: str  # e.g. "Up 3 hours (healthy)"
    image: str
    configured: bool  # False: a container left from a service no longer in the compose files


def stack_services(location: Location, stack: Stack) -> list[str]:
    """The stack's services, as its files define them."""
    return location.compose(*stack_args(stack), "config", "--services").split()


def read_services(location: Location, stack: Stack) -> list[ServiceStatus]:
    """The stack's services and their containers."""
    ps = location.compose(*stack_args(stack), "ps", "--all", "--format", "json")
    return statuses(stack.name, stack_services(location, stack), ps_rows(ps))


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
    stack: str, configured: list[str], containers: list[dict[str, Any]]
) -> list[ServiceStatus]:
    by_service = {container["Service"]: container for container in containers}
    found = [
        ServiceStatus(
            stack,
            name,
            by_service.get(name, {}).get("State", ""),
            by_service.get(name, {}).get("Status", ""),
            by_service.get(name, {}).get("Image", ""),
            True,
        )
        for name in configured
    ]
    found += [
        ServiceStatus(
            stack, name, c.get("State", ""), c.get("Status", ""), c.get("Image", ""), False
        )
        for name, c in by_service.items()
        if name not in configured
    ]
    return sorted(found, key=lambda s: s.service)


def ps_rows(output: str) -> list[dict[str, Any]]:
    """`ps --format json` output: one object per line (current compose) or one array (older)."""
    text = output.strip()
    if text.startswith("["):
        rows: list[dict[str, Any]] = json.loads(text)
        return rows
    return [json.loads(line) for line in text.splitlines() if line.strip()]
