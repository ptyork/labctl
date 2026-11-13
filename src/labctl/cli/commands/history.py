"""labctl history list|show|restore|diff: the lab config's applies, and going back to one.

An apply is the tags one run made (one per node it recorded, all with its stamp) on the commit it
applied. A REF names one: `last`, `previous`, its stamp (20261105-143012), a tag, or a commit.
Without a REF, a terminal picks from the newest applies.

Going back is an edit of the whole lab config, planned and applied like any other: `restore`
only puts the files back, uncommitted.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import click
from rich.markup import escape

from labctl.cli import menu
from labctl.cli.common import is_terminal, load_settings
from labctl.click import AliasedGroup, DebugOptions
from labctl.console import console
from labctl.repo import Apply, ConfigRepo, RepoError
from labctl.settings import Settings, SettingsError
from labctl.text import plural

# How many applies the picker offers.
PICK = 20

REFS = "last, previous, an apply's timestamp, a tag or a commit"


@click.group(name="history", cls=AliasedGroup)
def history_cmd() -> None:
    """The lab config's applies, and going back to one.

    Help for a command: labctl history COMMAND --help
    """


@history_cmd.command(name="list")
@click.argument("nodes", nargs=-1, metavar="[NODE...]")
@DebugOptions().options
def list_cmd(nodes: tuple[str, ...], **kwargs: Any) -> None:
    """Every apply, newest first (only those that tagged the nodes named, if any)."""
    _, repo, applies = _open()
    unknown = sorted(set(nodes) - {node for apply in applies for node in apply.nodes})
    if unknown:
        raise click.ClickException(f"No apply tagged {', '.join(unknown)}")
    shown = [apply for apply in applies if not nodes or set(nodes) & set(apply.nodes)]
    if not shown:
        console.print("No applies yet")
        return
    for line in _rows(shown):
        console.print(escape(line), highlight=False, soft_wrap=True)


@history_cmd.command(name="show")
@click.argument("ref", required=False, metavar="[REF]")
@DebugOptions().options
def show_cmd(ref: str | None, **kwargs: Any) -> None:
    """What an apply was: when, by whom, its nodes and what ran after it, the files it applied.

    REF: last, previous, an apply's timestamp, a tag or a commit (default: pick, in a terminal).
    """
    _, repo, applies = _open()
    commit, apply = _pick(repo, applies, ref)
    if apply is None:
        console.print(f"[bold]Commit {commit[:7]}[/] (no apply)")
        _files(repo.commit_files(commit))
        return
    info = apply.info
    console.print(f"[bold]Apply {apply.stamp}[/]  commit {apply.commit[:7]}")
    if info is not None:
        when = info.applied_at.replace("T", " ")
        console.print(f"  by       {escape(info.applied_by)}, {when}", highlight=False)
    console.print(f"  subject  {escape(apply.subject)}", highlight=False)
    for applied in apply.tags:
        node = applied.tag.split("/")[1]
        if applied.info is None:
            up = "?"
        elif applied.info.retired:
            up = "[yellow]retired[/]"
        else:
            up = ", ".join(applied.info.stacks_up) or "nothing up"
        console.print(f"  {escape(node):<8} {up}", highlight=False)
    older, paths = repo.apply_files(apply, applies)
    if older is None:
        _files(paths)
    elif older.commit == apply.commit:
        console.print("No lab config changes since the apply before it")
    else:
        _files(paths, f"Changed since {older.stamp}")


@history_cmd.command(name="restore")
@click.argument("ref", required=False, metavar="[REF]")
@DebugOptions().options
def restore_cmd(ref: str | None, **kwargs: Any) -> None:
    """Put the lab config's files back as they were at an apply. Nothing is committed and no node
    changes: plan shows what going back does, apply does it, discard undoes the restore.

    REF: last, previous, an apply's timestamp, a tag or a commit (default: pick, in a terminal).
    Refused while the lab config has changes no apply committed.
    """
    _, repo, applies = _open()
    try:
        repo.check_nothing_pending()
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    commit, apply = _pick(repo, applies, ref)
    where = f"{apply.stamp} ({commit[:7]})" if apply else commit[:7]
    try:
        changed = repo.go_back(commit)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    if not changed:
        console.print(f"The lab config is already as at {where}")
        return
    console.print(
        f"[green]✓[/] the lab config is as at {where}: {plural(len(changed), 'file')} differ "
        "from the last commit; nothing applied"
    )


@history_cmd.command(name="diff")
@click.argument("args", nargs=-1, metavar="[REF] [NODE...]")
@DebugOptions().options
def diff_cmd(args: tuple[str, ...], **kwargs: Any) -> None:
    """What changed in the lab config since an apply, to now (uncommitted changes included).

    REF: last, previous, an apply's timestamp, a tag or a commit (default: pick, in a terminal).
    NODE: only the files that build its bundle (its folder, nodes/_shared and everything outside
    nodes/). The first argument is a NODE when it is a node's name.
    """
    settings, repo, applies = _open()
    known = {node for apply in applies for node in apply.nodes} | set(_defined(settings))
    ref, nodes = (
        (None, args) if args and args[0] in known else (args[0] if args else None, args[1:])
    )
    unknown = sorted(set(nodes) - known)
    if unknown:
        raise click.ClickException(f"Unknown node(s): {', '.join(unknown)}")
    commit, _ = _pick(repo, applies, ref)
    sys.exit(repo.diff(commit, repo.node_pathspecs(commit, settings.nodes_dir, nodes)))


#############################################################################


def _open() -> tuple[Settings, ConfigRepo, list[Apply]]:
    settings = load_settings()
    try:
        repo = ConfigRepo.open(settings.lab_config)
        return settings, repo, repo.applies()
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc


def _defined(settings: Settings) -> list[str]:
    try:
        return settings.node_names()
    except SettingsError:
        return []


def _pick(repo: ConfigRepo, applies: Sequence[Apply], ref: str | None) -> tuple[str, Apply | None]:
    """The commit (and apply) ``ref`` names; without one, picked in a terminal."""
    if ref is not None:
        try:
            return repo.resolve(ref, applies)
        except RepoError as exc:
            raise click.ClickException(str(exc)) from exc
    if not is_terminal():
        raise click.ClickException(f"Name the apply: {REFS}")
    if not applies:
        raise click.ClickException("No applies yet")
    newest = list(applies[:PICK])
    index = menu.choose("Which apply?", _rows(newest))
    if index is None:
        raise click.ClickException("Nothing picked")
    return newest[index].commit, newest[index]


def _rows(applies: Sequence[Apply]) -> list[str]:
    """One line per apply: when, commit, nodes (retired marked), by whom, subject."""
    cells = []
    for apply in applies:
        info = apply.info
        when = info.applied_at.replace("T", " ") if info else apply.stamp
        nodes = ", ".join(
            f"{node} (retired)" if node in apply.retired else node for node in apply.nodes
        )
        by = info.applied_by if info else ""
        subject = "no lab config changes" if apply.again else apply.subject
        cells.append([when, apply.commit[:7], nodes, by, subject])
    widths = [max(len(row[col]) for row in cells) for col in range(4)]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths, strict=True))
        + "  "
        + row[4]
        for row in cells
    ]


def _files(paths: Sequence[str], title: str = "Files its commit changed") -> None:
    if not paths:
        return
    console.print(f"[bold]{title}[/]")
    for path in paths:
        console.print(f"  {escape(path)}", highlight=False)
