"""Day-to-day commands on the nodes' deployed stacks: labctl node COMMAND TARGET... [-s SERVICE]...

A target is NODE (all of the node's stacks), NODE/STACK (one of them), or 'all' (every node in
turn). -s narrows a command to some services: each runs in its own stack, on the targets that
have it.
"""

from __future__ import annotations

import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml
from craftable import get_table
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from labctl.bundle import REQUIRED_VARS
from labctl.cli import menu
from labctl.cli.commands.settings import load_settings
from labctl.click import AliasedGroup, DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import node_env_file, parse_env
from labctl.remote import Remote, RemoteError, node_remote
from labctl.settings import SettingsError
from labctl.stack import (
    Deployed,
    ServiceStatus,
    compose_command,
    deployed,
    read_services,
    stack_services,
    unbacked_projects,
)
from labctl.state import ALL, Stack

targets_argument = click.argument("targets", nargs=-1, required=True, metavar="NODE[/STACK]...|all")
services_option = click.option(
    "--service",
    "-s",
    "services",
    multiple=True,
    metavar="SERVICE",
    help="Only this service (repeatable).",
)

# The stacks a command runs on, each with the services named in it (none named: all of them).
Runs = list[tuple[Stack, tuple[str, ...]]]


@dataclass(frozen=True)
class Target:
    """One node's part of a command: where its stacks are, and the ones selected."""

    deployed: Deployed
    stacks: tuple[Stack, ...]
    remote: Remote
    whole: bool  # the node was named without a stack


@click.group(name="node", cls=AliasedGroup)
def node_cmd() -> None:
    """Run compose commands on the nodes' deployed stacks; add a node to the lab config.

    Targets: NODE, NODE/STACK, or 'all' (every node in turn). Help for a command:
    labctl node COMMAND --help
    """


@node_cmd.command(name="up")
@targets_argument
@services_option
@DebugOptions().options
def up_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Start the stacks (or some services), removing containers no longer in them."""
    _compose(targets, services, "up", "-d", "--remove-orphans")


@node_cmd.command(name="down")
@targets_argument
@services_option
@DebugOptions().options
def down_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Stop and remove the stacks' containers (or some services'), last stack first."""
    _compose(targets, services, "down", reverse=True)


@node_cmd.command(name="restart")
@targets_argument
@services_option
@DebugOptions().options
def restart_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Restart the stacks' containers (or some services') without recreating them."""
    _compose(targets, services, "restart")


@node_cmd.command(name="pull")
@targets_argument
@services_option
@DebugOptions().options
def pull_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Pull the stacks' images (or some services'). Run 'up' afterwards to use them."""
    _compose(targets, services, "pull", "--ignore-pull-failures")


@node_cmd.command(name="prune")
@targets_argument
@DebugOptions().options
def prune_cmd(targets: tuple[str, ...], **kwargs: Any) -> None:
    """Remove dangling images on the nodes (all of docker, not just the stacks)."""

    def prune(t: Target, runs: Runs) -> int:
        if not t.whole:
            raise SettingsError("prune is node-wide; give the node without a stack")
        return t.remote.run("docker image prune --force")

    _each_node(targets, prune)


@node_cmd.command(name="logs")
@targets_argument
@services_option
@click.option("--follow", "-f", is_flag=True, help="Keep streaming new output.")
@click.option("--tail", "-n", default="100", show_default=True, help="Lines per container.")
@DebugOptions().options
def logs_cmd(
    targets: tuple[str, ...], services: tuple[str, ...], follow: bool, tail: str, **kwargs: Any
) -> None:
    """Show the stacks' logs (or some services'). Following several stacks, each line starts
    with the stack it comes from."""
    args = ["logs", "--tail", tail, *(["--follow"] if follow else [])]
    resolved, failed = _resolve(targets, services)
    streams = [
        (
            t.deployed.label(stack),
            t.remote,
            compose_command(t.deployed.deploy_dir, stack, *args, *names),
        )
        for t, runs in resolved
        for stack, names in runs
    ]
    try:
        if follow and len(streams) > 1:
            failed += [label for label, code in _follow(streams) if code]
        else:
            for label, remote, command in streams:
                if len(streams) > 1:
                    console.rule(f"[bold]{escape(label)}[/]")
                if remote.run(command, _tty()):
                    failed.append(label)
    finally:
        Remote.close_all([*(t.deployed.node for t, _ in resolved), *failed])
    _finish(failed, len(streams) > 1)


@node_cmd.command(name="status")
@targets_argument
@DebugOptions().options
def status_cmd(targets: tuple[str, ...], **kwargs: Any) -> None:
    """Show each service's container state."""

    def show(t: Target, runs: Runs) -> int:
        rows = [
            [status.stack, *_status_row(status)]
            for stack in t.stacks
            for status in read_services(t.deployed.location, stack)
        ]
        if t.whole:
            rows += [
                [name, _NO_FILE, "", status, ""]
                for name, status in unbacked_projects(t.deployed).items()
            ]
        header = ["Stack", "Service", "State", "Status", "Image"]
        colors = [_colored(_flagged), _colored(_flagged), _colored(_state_color), _plain, _plain]
        if len({row[0] for row in rows}) <= 1:  # one stack: its name is in the title
            rows, header, colors = [row[1:] for row in rows], header[1:], colors[1:]

        def table(col_defs: list[str]) -> str:
            return str(
                get_table(
                    rows or [["(no services)", *[""] * (len(header) - 1)]],
                    header_row=header,
                    col_defs=col_defs,
                    table_width=max(console.width - 4, 50),
                    # Markup goes on after sizing, so it never counts toward column widths.
                    postprocessors=colors,
                )
            )

        try:  # columns sized to their data, the image takes the rest
            body = table([*[""] * (len(header) - 1), "A"])
        except ValueError:  # too narrow a terminal for that: every column shares the width
            body = table(["A"] * len(header))
        title = t.deployed.label(t.stacks[0]) if len(t.stacks) == 1 else t.deployed.node
        console.print(Panel(body, title=title, border_style="cyan"))
        return 0

    _each_node(targets, show, labelled=False)


_ORPHAN = " (not in compose files)"
_NO_FILE = "(running; no stack file in DEPLOY_DIR)"
_NOT_CREATED = "not created"


def _status_row(status: ServiceStatus) -> list[str]:
    name = status.service + ("" if status.configured else _ORPHAN)
    return [name, status.state or _NOT_CREATED, status.status, status.image]


def _flagged(value: str) -> str:
    return "yellow" if value.endswith(_ORPHAN) or value == _NO_FILE else ""


def _state_color(value: str) -> str:
    return {"running": "green", _NOT_CREATED: "dim", "": ""}.get(value, "red")


def _colored(color_of: Callable[[str], str]) -> Callable[[Any, str, list[Any], int], str]:
    """craftable postprocessor: escape the sized cell text and color it by the cell's value."""

    def apply(value: Any, text: str, row: list[Any], col_idx: int) -> str:
        color = color_of(str(value))
        return f"[{color}]{escape(text)}[/]" if color else escape(text)

    return apply


_plain = _colored(lambda value: "")


def _compose(
    targets: tuple[str, ...], services: tuple[str, ...], *args: str, reverse: bool = False
) -> None:
    def run(t: Target, runs: Runs) -> int:
        failed = 0
        for stack, names in reversed(runs) if reverse else runs:
            if len(runs) > 1:
                console.rule(f"[bold]{escape(t.deployed.label(stack))}[/]")
            command = compose_command(t.deployed.deploy_dir, stack, *args, *names)
            failed |= t.remote.run(command, _tty())
        return failed

    _each_node(targets, run, services=services)


def _with_services(t: Target, services: tuple[str, ...]) -> Runs:
    """(stack, the named services in it) for each of the target's stacks that has any."""
    if not services:
        return [(stack, ()) for stack in t.stacks]
    by_stack = {
        stack: tuple(
            service for service in stack_services(t.deployed.location, stack) if service in services
        )
        for stack in t.stacks
    }
    return [(stack, names) for stack, names in by_stack.items() if names]


def _nodes(targets: Sequence[str], names: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Node -> the stacks named on it ("" for all of them), in the order given."""
    if ALL in names:
        raise SettingsError(f"a node may not be called '{ALL}'")
    if ALL in targets:
        if len(targets) > 1:
            raise SettingsError(f"'{ALL}' already means every node")
        return {name: ("",) for name in names}
    chosen: dict[str, tuple[str, ...]] = {}
    for target in targets:
        node, _, selector = target.partition("/")
        if node == ALL:
            raise SettingsError("a stack can only be named with its node")
        if node not in names:
            raise SettingsError(f"Unknown node '{node}'. Nodes: {', '.join(names)}")
        if selector not in chosen.get(node, ()):
            chosen[node] = (*chosen.get(node, ()), selector)
    return chosen


def _resolve(
    targets: Sequence[str], services: tuple[str, ...] = ()
) -> tuple[list[tuple[Target, Runs]], list[str]]:
    """Each target node's stacks and what runs on them, and the nodes that could not be read
    (already reported). With services, nodes that have none of them drop out; a service no target
    has is an error, before anything runs."""
    settings = load_settings()
    try:
        chosen = _nodes(targets, settings.node_names())
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    resolved: list[tuple[Target, Runs]] = []
    failed: list[str] = []
    try:
        for name, selectors in chosen.items():
            try:
                remote = node_remote(name)
                where = deployed(settings, name, remote)
                whole = "" in selectors
                picked = {
                    stack for selector in selectors if selector for stack in where.select(selector)
                }
                stacks = tuple(stack for stack in where.stacks if whole or stack in picked)
                target = Target(where, stacks, remote, whole)
                resolved.append((target, _with_services(target, services)))
            except (RemoteError, SettingsError, ComposeError) as exc:
                console.print(f"[red]{escape(str(exc))}[/]")
                failed.append(name)
        found = {name for _, runs in resolved for _, names in runs for name in names}
        unknown = sorted(set(services) - found)
        if unknown and resolved:
            scope = resolved[0][0].deployed.node if len(chosen) == 1 else "the targets"
            raise click.ClickException(f"{scope}: no stack has {', '.join(unknown)}")
    except BaseException:
        Remote.close_all(chosen)
        raise
    return [(t, runs) for t, runs in resolved if runs], failed


def _each_node(
    targets: Sequence[str],
    action: Callable[[Target, Runs], int],
    labelled: bool = True,
    services: tuple[str, ...] = (),
) -> None:
    """Run ``action`` for each target node in turn; exit 1 if any failed."""
    resolved, failed = _resolve(targets, services)
    try:
        for target, runs in resolved:
            name = target.deployed.node
            if labelled and len(resolved) > 1:
                console.rule(f"[bold]{name}[/]")
            try:
                status = action(target, runs)
            except (RemoteError, SettingsError, ComposeError) as exc:
                console.print(f"[red]{escape(str(exc))}[/]")
                status = 1
            if status:
                failed.append(name)
    finally:
        Remote.close_all([*(t.deployed.node for t, _ in resolved), *failed])
    _finish(failed, len(resolved) + len(failed) > 1)


def _finish(failed: list[str], several: bool) -> None:
    if failed:
        if several:
            console.print(f"[red]Failed on: {', '.join(failed)}[/]")
        sys.exit(1)


def _follow(streams: Sequence[tuple[str, Remote, str]]) -> list[tuple[str, int]]:
    """Run the commands at once, each output line printed after its label, until all end (or
    Ctrl-C). Returns each label's exit status."""
    width = max(len(label) for label, _, _ in streams)
    lock = threading.Lock()
    procs = [(label, remote.stream(command)) for label, remote, command in streams]

    def pump(label: str, out: Any) -> None:
        prefix = Text(label.ljust(width), style="cyan") + " | "
        for line in out:
            with lock:  # the line's own colors kept, as the node's terminal would show them
                console.print(
                    prefix + Text.from_ansi(line.rstrip()), soft_wrap=True, highlight=False
                )

    threads = [
        threading.Thread(target=pump, args=(label, proc.stdout), daemon=True)
        for label, proc in procs
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:  # stopping them is not a failure
        for _, proc in procs:
            proc.terminate()
        for _, proc in procs:
            proc.wait()
        raise
    return [(label, proc.wait()) for label, proc in procs]


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


#############################################################################


# labctl node add


#############################################################################

_NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DEPLOY, APPDATA = REQUIRED_VARS


@node_cmd.command(name="add")
@click.argument("name")
@click.option("--deploy-dir", metavar="DIR", help="DEPLOY_DIR: where the node's stacks go.")
@click.option(
    "--appdata-dir",
    metavar="DIR",
    help="APPDATA_DIR: the services' data, migrated with them (may start with $DEPLOY_DIR).",
)
@DebugOptions().options
def add_cmd(name: str, deploy_dir: str | None, appdata_dir: str | None, **kwargs: Any) -> None:
    """Add a node to the lab config: nodes/NAME/node.env and a compose file (not committed:
    apply -m does that, with the rest of the change).

    NAME is the node's ssh host. In a terminal, asks for DEPLOY_DIR and APPDATA_DIR (offering
    what the other nodes use); with no terminal, pass both. There is no default: a wrong one
    would put the stacks or the data where you did not choose. Checks the node over ssh (a
    warning when it cannot be reached yet).
    """
    settings = load_settings()
    if name == ALL or not _NODE_NAME.fullmatch(name):
        raise click.ClickException(f"'{name}' cannot be a node name")
    folder = settings.nodes_dir / name
    if folder.exists() and any(folder.iterdir()):
        raise click.ClickException(f"{folder} already exists")
    try:
        others = settings.node_names()
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    raw = {other: parse_env(_read(node_env_file(settings.nodes_dir, other))) for other in others}

    deploy = deploy_dir or _ask(name, DEPLOY, _usual(raw, DEPLOY), "Where labctl puts its stacks.")
    appdata = appdata_dir or _ask(
        name,
        APPDATA,
        _usual(raw, APPDATA) or f"${DEPLOY}/appdata",
        "The services' data: what labctl migrates and archives.",
    )
    for var, value in ((DEPLOY, deploy), (APPDATA, appdata)):
        problem = _node_path(value, allow_deploy=var == APPDATA)
        if problem:
            raise click.ClickException(f"{var}: {problem}")

    folder.mkdir(parents=True, exist_ok=True)
    env_file = node_env_file(settings.nodes_dir, name)
    env_file.write_text(f"{DEPLOY}={deploy}\n{APPDATA}={appdata}\n", encoding="utf-8")
    compose_file = folder / "docker-compose.yml"
    project = _project_prefix(settings.nodes_dir, others) + name
    compose_file.write_text(
        f"name: {project}\n\n# {name}'s services: include files of the lab config, or define "
        "them here.\n",
        encoding="utf-8",
    )
    console.print(f"[green]✓[/] added {escape(str(folder))}")
    _check_node(name)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _usual(raw: Mapping[str, Mapping[str, str]], var: str) -> str:
    """The value most nodes give ``var`` in their node.env, as written ($DEPLOY_DIR kept)."""
    values = Counter(env[var] for env in raw.values() if env.get(var))
    return values.most_common(1)[0][0] if values else ""


def _project_prefix(nodes_dir: Path, nodes: Sequence[str]) -> str:
    """What the other nodes put before their name in their project name (homelab-), if they
    agree on it."""
    prefixes = set()
    for node in nodes:
        try:
            config = yaml.safe_load(_read(nodes_dir / node / "docker-compose.yml")) or {}
        except yaml.YAMLError:
            continue
        project = str(config.get("name", "")) if isinstance(config, dict) else ""
        if project.endswith(node):
            prefixes.add(project.removesuffix(node))
    return prefixes.pop() if len(prefixes) == 1 else ""


def _node_path(value: str, allow_deploy: bool = False) -> str | None:
    if value.startswith("/") or (allow_deploy and value.startswith(f"${DEPLOY}/")):
        return None
    return "must be an absolute path on the node" + (
        f" (or start with ${DEPLOY}/)" if allow_deploy else ""
    )


def _ask(node: str, var: str, suggestion: str, what: str) -> str:
    if not _tty():
        raise click.ClickException(
            "No terminal to ask in: pass --deploy-dir and --appdata-dir (labctl does not guess "
            "where the stacks or the data go)"
        )
    answer = menu.edit_text(
        f"[bold cyan]{escape(node)}[/]: {var}",
        var,
        suggestion,
        header=f"{what} Saved to nodes/{node}/node.env. Esc to cancel.",
        validators=[lambda v: _node_path(v, allow_deploy=var == APPDATA)],
    )
    if answer is None:
        raise click.ClickException("Not added")
    return answer


def _check_node(node: str) -> None:
    """Warn when labctl cannot reach the node, or it has no docker compose (yet)."""
    try:
        result = node_remote(node).execute(["docker", "compose", "version", "--short"], "/")
    except RemoteError as exc:
        console.print(f"[yellow]![/] {node}: not reachable ({escape(str(exc))})")
        return
    if result.code:
        console.print(f"[yellow]![/] {node}: no docker compose there")
