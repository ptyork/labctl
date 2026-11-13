"""Day-to-day commands on the nodes' deployed stacks: labctl node COMMAND TARGET... [-s SERVICE]...

A target is NODE (all of the node's stacks), NODE/STACK (one of them), or 'all' (every node in
turn). -s narrows a command to some services: each runs in its own stack, on the targets that
have it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Any

import click
from craftable import get_table
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from labctl.cli.common import is_terminal, load_settings, services_option, target_nodes
from labctl.click import AliasedGroup, DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.remote import Remote, RemoteError
from labctl.settings import SettingsError
from labctl.stack import (
    PULL,
    UP,
    Runs,
    ServiceStatus,
    Target,
    follow_logs,
    log_streams,
    prune_images,
    run_compose,
    select_target,
    show_logs,
    target_services,
)

targets_argument = click.argument("targets", nargs=-1, required=True, metavar="NODE[/STACK]...|all")


@click.group(name="node", cls=AliasedGroup)
def node_cmd() -> None:
    """Run compose commands on the nodes' deployed stacks.

    Targets: NODE, NODE/STACK, or 'all' (every node in turn). Help for a command:
    labctl node COMMAND --help
    """


@node_cmd.command(name="up")
@targets_argument
@services_option
@DebugOptions().options
def up_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Start the stacks (or some services), removing containers no longer in them."""
    _compose(targets, services, *UP, report=True)


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
    _compose(targets, services, *PULL)


@node_cmd.command(name="prune")
@targets_argument
@DebugOptions().options
def prune_cmd(targets: tuple[str, ...], **kwargs: Any) -> None:
    """Remove dangling images on the nodes (all of docker, not just the stacks)."""

    def prune(t: Target, runs: Runs) -> int:
        if not t.whole:
            raise SettingsError("prune is node-wide; give the node without a stack")
        return prune_images(t)

    _each_node(targets, prune)


@node_cmd.command(name="update")
@targets_argument
@services_option
@DebugOptions().options
def update_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Pull, up, then prune, node by node (a node whose pull or up fails goes no further). Prune is
    node-wide, also for NODE/STACK or -s."""

    def update(t: Target, runs: Runs) -> int:
        failed = _run_stacks(t, runs, *PULL)
        failed = failed or _run_stacks(t, runs, *UP, report=True)
        if failed:
            return failed
        return prune_images(t)

    _each_node(targets, update, services=services)


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
    resolved, failed = _resolve(targets, services)
    streams = [stream for t, runs in resolved for stream in log_streams(t, runs, tail, follow)]
    try:
        if follow and len(streams) > 1:
            width = max(len(stream.label) for stream in streams)

            def emit(label: str, line: str) -> None:
                # The line's own colors kept, as the node's terminal would show them.
                prefix = Text(label.ljust(width), style="cyan") + " | "
                console.print(
                    prefix + Text.from_ansi(line.rstrip()), soft_wrap=True, highlight=False
                )

            failed += [label for label, code in follow_logs(streams, emit) if code]
        else:
            for stream in streams:
                if len(streams) > 1:
                    console.rule(f"[bold]{escape(stream.label)}[/]")
                if show_logs(stream, is_terminal()):
                    failed.append(stream.label)
    finally:
        Remote.close_all([*(t.node for t, _ in resolved), *failed])
    _finish(failed, len(streams) > 1)


@node_cmd.command(name="status")
@targets_argument
@DebugOptions().options
def status_cmd(targets: tuple[str, ...], **kwargs: Any) -> None:
    """Show each service's container state."""

    def show(t: Target, runs: Runs) -> int:
        found, unbacked = target_services(t)
        rows = [[status.stack, *_status_row(status)] for status in found]
        rows += [[name, _NO_FILE, "", status, ""] for name, status in unbacked.items()]
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
        for status in found:
            if status.looping:
                how = "restarting" if status.state == "restarting" else "up less than 5 minutes"
                console.print(
                    f"[yellow]![/] {t.deployed.node}: {escape(status.service)} may be in a restart "
                    f"loop ({how}, {status.restarts} restarts)"
                )
        return 0

    _each_node(targets, show, labelled=False)


_ORPHAN = " (not in compose files)"
_NO_FILE = "(running; no stack file in DEPLOY_DIR)"
_NOT_CREATED = "not created"


def _status_row(status: ServiceStatus) -> list[str]:
    name = status.service + ("" if status.configured else _ORPHAN)
    restarts = f" ({status.restarts} restarts)" if status.restarts else ""
    return [name, status.state or _NOT_CREATED, status.status + restarts, status.image]


def _flagged(value: str) -> str:
    return "yellow" if value.endswith(_ORPHAN) or value == _NO_FILE else ""


def _state_color(value: str) -> str:
    return {"running": "green", _NOT_CREATED: "yellow", "": ""}.get(value, "red")


def _colored(color_of: Callable[[str], str]) -> Callable[[Any, str, list[Any], int], str]:
    """craftable postprocessor: escape the sized cell text and color it by the cell's value."""

    def apply(value: Any, text: str, row: list[Any], col_idx: int) -> str:
        color = color_of(str(value))
        return f"[{color}]{escape(text)}[/]" if color else escape(text)

    return apply


_plain = _colored(lambda value: "")


def _compose(
    targets: tuple[str, ...],
    services: tuple[str, ...],
    *args: str,
    reverse: bool = False,
    report: bool = False,
) -> None:
    def run(t: Target, runs: Runs) -> int:
        return _run_stacks(t, runs, *args, reverse=reverse, report=report)

    _each_node(targets, run, services=services)


def _run_stacks(
    t: Target, runs: Runs, *args: str, reverse: bool = False, report: bool = False
) -> int:
    """One compose command on each of a target's stacks; non-zero if any failed. With ``report``,
    a line per stack after it says which containers the command recreated, created or removed."""
    failed = 0
    for stack, names in reversed(runs) if reverse else runs:
        label = t.deployed.label(stack)
        if len(runs) > 1:
            console.rule(f"[bold]{escape(label)}[/]")
        code, changes = run_compose(t, stack, names, *args, terminal=is_terminal(), report=report)
        failed |= code
        if report and not code:
            said = str(changes) if changes else "no containers changed"
            console.print(f"[green]✓[/] {escape(label)}: {escape(said)}")
    return failed


def _resolve(
    targets: Sequence[str], services: tuple[str, ...] = ()
) -> tuple[list[tuple[Target, Runs]], list[str]]:
    """Each target node's stacks and what runs on them, and the nodes that could not be read
    (already reported). With services, nodes that have none of them drop out; a service no target
    has is an error, before anything runs."""
    settings = load_settings()
    try:
        chosen = target_nodes(targets, settings.node_names())
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    resolved: list[tuple[Target, Runs]] = []
    failed: list[str] = []
    try:
        for name, selectors in chosen.items():
            try:
                resolved.append(select_target(settings, name, selectors, services))
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
        Remote.close_all([*(t.node for t, _ in resolved), *failed])
    _finish(failed, len(resolved) + len(failed) > 1)


def _finish(failed: list[str], several: bool) -> None:
    if failed:
        if several:
            console.print(f"[red]Failed on: {', '.join(failed)}[/]")
        sys.exit(1)
