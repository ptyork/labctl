"""See and prune labctl's archives on the nodes: labctl archive list|prune NODE...|all."""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

import click
from rich.markup import escape

from labctl.apply import ApplyError, Node
from labctl.archive import (
    FAILED,
    FILES,
    Entry,
    Group,
    NodeArchive,
    archive_dirs,
    delete_entries,
    group_entries,
    parse_age,
    read_archive,
    select,
)
from labctl.checks import human_size
from labctl.cli import menu
from labctl.cli.commands.node import services_option, target_nodes
from labctl.cli.commands.settings import load_settings
from labctl.click import AliasedGroup, DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.remote import Remote, RemoteError, node_remote
from labctl.settings import SettingsError

_FAILURES = (ApplyError, ComposeError, RemoteError, SettingsError)

targets_argument = click.argument("targets", nargs=-1, required=True, metavar="NODE...|all")


@click.group(name="archive", cls=AliasedGroup)
def archive_cmd() -> None:
    """See and prune what apply archived on the nodes.

    Help for a command: labctl archive COMMAND --help
    """


@archive_cmd.command(name="list")
@targets_argument
@services_option
@DebugOptions().options
def list_cmd(targets: tuple[str, ...], services: tuple[str, ...], **kwargs: Any) -> None:
    """Show what the nodes' archives hold (-s: only these services' data)."""
    nodes, archives, failed = _read(targets)
    try:
        shown = {archive.node: _shown(archive, services) for archive in archives}
        _check_services(services, shown)
        _print_archives(shown)
        entries = {
            entry for groups in shown.values() for group in groups for entry in group.entries
        }
        holding = sum(1 for groups in shown.values() if groups)
        console.print(f"[bold]{_count(entries)}, {_size(entries)} on {_nodes(holding)}[/]")
    finally:
        Remote.close_all([*nodes, *failed])
    _finish(failed)


@archive_cmd.command(name="prune")
@targets_argument
@services_option
@click.option(
    "--keep",
    type=click.IntRange(min=0),
    default=1,
    show_default=True,
    help="Copies to keep of each service's container path, and of each file.",
)
@click.option(
    "--older-than",
    metavar="AGE",
    callback=lambda ctx, param, value: _age(value),
    help="Of those not kept, delete only entries older than this (30d, 12h).",
)
@click.option("--dry-run", is_flag=True, help="Only show what would be deleted.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask before deleting.")
@DebugOptions().options
def prune_cmd(
    targets: tuple[str, ...],
    services: tuple[str, ...],
    keep: int,
    older_than: timedelta | None,
    dry_run: bool,
    yes: bool,
    **kwargs: Any,
) -> None:
    """Delete archived copies, keeping the newest of each. Failed copies always go.

    This cannot be undone. It shows what it will delete and asks first.
    """
    nodes, archives, failed = _read(targets)
    try:
        shown = {archive.node: _shown(archive, services) for archive in archives}
        _check_services(services, shown)
        doomed = {
            archive.node: select(
                group_entries(archive.entries), keep=keep, older_than=older_than, services=services
            )
            for archive in archives
        }
        _print_archives(shown, doomed)
        going = {entry for entries in doomed.values() for entry in entries}
        if not going:
            console.print("[bold]Nothing to delete[/]")
        else:
            on_nodes = sum(1 for entries in doomed.values() if entries)
            all_entries = {
                entry for groups in shown.values() for group in groups for entry in group.entries
            }
            console.print(
                f"[bold]Delete {_count(going)} ({_size(going)}) on {_nodes(on_nodes)}; "
                f"keep {len(all_entries - going)}[/]"
            )
            if not dry_run:
                _confirm(yes)
                failed += _delete(nodes, archives, doomed)
    finally:
        Remote.close_all([*nodes, *failed])
    _finish(failed)


def _age(value: str | None) -> timedelta | None:
    if value is None:
        return None
    try:
        return parse_age(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def _read(targets: Sequence[str]) -> tuple[dict[str, Node], list[NodeArchive], list[str]]:
    """Each named node, what its archives hold, and the nodes that could not be read (reported)."""
    settings = load_settings()
    try:
        chosen = target_nodes(targets, settings.node_names())
    except SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    if any(selectors != ("",) for selectors in chosen.values()):
        raise click.ClickException("Archives belong to a node: give NODE, not NODE/STACK")
    nodes: dict[str, Node] = {}
    archives: list[NodeArchive] = []
    failed: list[str] = []
    try:
        for name in chosen:
            try:
                remote = node_remote(name)
                node = Node(remote)
                data_dir, files_dir = archive_dirs(settings, name, remote)
                archives.append(read_archive(name, node, data_dir, files_dir))
                nodes[name] = node
            except _FAILURES as exc:
                console.print(f"[red]{escape(str(exc))}[/]")
                failed.append(name)
    except BaseException:
        Remote.close_all(chosen)
        raise
    if not archives:  # every node failed, and each has said why
        Remote.close_all(chosen)
        _finish(failed)
    return nodes, archives, failed


def _shown(archive: NodeArchive, services: Collection[str]) -> list[Group]:
    groups = group_entries(archive.entries)
    return [group for group in groups if not services or group.service in services]


def _check_services(services: Iterable[str], shown: dict[str, list[Group]]) -> None:
    found = {group.service for groups in shown.values() for group in groups}
    unknown = sorted(set(services) - found)
    if unknown and shown:
        raise click.ClickException(f"Nothing archived for {', '.join(unknown)}")


def _print_archives(
    shown: dict[str, list[Group]], doomed: dict[str, frozenset[Entry]] | None = None
) -> None:
    """Each node's groups; with ``doomed`` (prune), keep or delete on each line."""
    for node, groups in shown.items():
        console.print(f"[bold]{escape(node)}[/]")
        if not groups:
            console.print("  nothing archived")
        sections: dict[str, list[tuple[Entry, list[str]]]] = {}
        for group in groups:
            title = "files" if group.kind == FILES else group.title
            sections.setdefault(title, []).extend(
                (entry, _row(group, entry)) for entry in group.entries
            )
        for title, rows in sections.items():
            console.print(f"  [bold]{escape(title)}[/]")
            widths = [max(len(cells[col]) for _, cells in rows) for col in range(3)]
            for entry, cells in rows:
                padded = (cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
                line = "  ".join(padded).rstrip()
                mark = ""
                if doomed is not None:
                    going = entry in doomed.get(node, frozenset())
                    mark = "[red]delete[/]  " if going else "[green]keep[/]    "
                console.print(f"    {mark}{escape(line)}", highlight=False, soft_wrap=True)
        console.print()


def _row(group: Group, entry: Entry) -> list[str]:
    size = human_size(entry.size) if entry.size is not None else "?"
    if group.kind == FAILED:
        return [entry.path, "", size]
    if group.kind == FILES:
        return [entry.name, entry.stamp, size]
    return [entry.stamp, entry.reason, size]


def _confirm(yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise click.ClickException("Confirm in a terminal, or pass --yes")
    console.print()
    if not menu.confirm("Delete these entries?"):
        raise click.ClickException("Not deleted")


def _delete(
    nodes: dict[str, Node], archives: Sequence[NodeArchive], doomed: dict[str, frozenset[Entry]]
) -> list[str]:
    """Delete on each node that has something to delete; returns the nodes that failed."""
    failed: list[str] = []
    owner = f"labctl archive prune {datetime.now().isoformat(timespec='seconds')}"
    for archive in archives:
        going = doomed[archive.node]
        if not going and not archive.orphans:
            continue
        try:
            delete_entries(nodes[archive.node], archive, going, owner)
        except _FAILURES as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            failed.append(archive.node)
            continue
        if going:
            console.print(
                f"[green]✓[/] {escape(archive.node)}: deleted {_count(going)} ({_size(going)})"
            )
    return failed


def _count(entries: Collection[Entry]) -> str:
    return f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"


def _size(entries: Iterable[Entry]) -> str:
    return human_size(sum(entry.size or 0 for entry in entries))


def _nodes(count: int) -> str:
    return f"{count} node{'' if count == 1 else 's'}"


def _finish(failed: Sequence[str]) -> None:
    if failed:
        if len(failed) > 1:
            console.print(f"[red]Failed on: {', '.join(failed)}[/]")
        sys.exit(1)
