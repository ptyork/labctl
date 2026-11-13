"""labctl add NODE: add a node to the lab config, taking in the stacks it already runs."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from labctl.adopt import (
    APPDATA,
    DEPLOY,
    Added,
    Host,
    add_node,
    dir_problem,
    last_applied_folder,
    read_host,
    suggest_appdata_dir,
    suggest_deploy_dir,
    valid_name,
)
from labctl.cli import menu
from labctl.cli.common import is_terminal, load_settings
from labctl.cli.render import render_checked
from labctl.click import DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.remote import Remote, RemoteError
from labctl.repo import Applied, ConfigRepo, RepoError
from labctl.settings import Settings, SettingsError
from labctl.validate import check_new_node


@click.command(name="add")
@click.argument("name")
@click.option("--deploy-dir", metavar="DIR", help="DEPLOY_DIR: where the node's stacks go.")
@click.option(
    "--appdata-dir",
    metavar="DIR",
    help="APPDATA_DIR: the services' data, migrated with them (may start with $DEPLOY_DIR).",
)
@click.option("--no-import", is_flag=True, help="Start the node empty, though it runs stacks.")
@click.option(
    "--no-restore",
    is_flag=True,
    help="Add the node anew, though the lab config's history has its folder.",
)
@DebugOptions().options
def add_cmd(
    name: str,
    deploy_dir: str | None,
    appdata_dir: str | None,
    no_import: bool,
    no_restore: bool,
    **kwargs: Any,
) -> None:
    """Add a node to the lab config, taking in the stacks it already runs.

    NAME is the node's ssh host. A node the lab config had before (removed, or retired) gets its
    folder back as its last apply had it (asked in a terminal; --no-restore adds it anew).
    Otherwise, in a terminal, asks for DEPLOY_DIR and APPDATA_DIR, offering what the node runs
    suggests; with no terminal, pass both (nothing is guessed). The stack files at the top of
    DEPLOY_DIR are copied into nodes/NAME, with what they refer to, and their .env becomes
    node.env (a terminal is asked which values are secrets). Nothing is committed: the first apply
    does that.
    """
    settings = load_settings()
    if not valid_name(name):
        raise click.ClickException(f"'{name}' cannot be a node name")
    nodes_dir = settings.nodes_dir
    folder = nodes_dir / name
    if folder.exists() and any(folder.iterdir()):
        raise click.ClickException(f"{folder} already exists")
    try:
        others = settings.node_names()
        repo = ConfigRepo.open(settings.lab_config)
        past = None if no_restore else last_applied_folder(repo, nodes_dir, name)
    except (SettingsError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    if past is not None and _restore(repo, past, folder, deploy_dir or appdata_dir):
        _validate(settings, name)
        return
    if not is_terminal() and not (deploy_dir and appdata_dir):
        raise click.ClickException(
            "No terminal to ask in: pass --deploy-dir and --appdata-dir (labctl does not guess "
            "where the stacks or the data go)"
        )

    try:
        host = _check_node(name)
        usable = host is not None and host.docker and bool(host.compose)
        if not usable and is_terminal():
            # Nothing to offer or import: usually a typo, or a node not set up yet.
            answer = menu.confirm(f"Add {name} anyway?", default=False)
            if not answer:
                raise click.ClickException("Not added")
        if host is not None and host.projects and not (deploy_dir and appdata_dir):
            console.print(f"{escape(name)} runs:")
            for project in host.projects:
                where = project.working_dir or "?"
                console.print(f"  - {escape(project.name)} ({escape(where)})")
        deploy = deploy_dir or _ask(
            name,
            DEPLOY,
            suggest_deploy_dir(nodes_dir, others, host),
            "Where labctl puts its stacks.",
        )
        appdata = appdata_dir or _ask(
            name,
            APPDATA,
            suggest_appdata_dir(nodes_dir, others, host, deploy),
            "The services' data: what labctl migrates and archives.",
        )
        if not no_import and host is None:
            console.print(f"[yellow]![/] {escape(name)}: nothing imported")
        try:
            added = add_node(
                settings,
                name,
                deploy,
                appdata,
                None if no_import else host,
                others,
                _ask_secrets if is_terminal() else None,
            )
        except (RemoteError, ComposeError) as exc:
            raise click.ClickException(str(exc)) from exc
        _report(name, added)
    finally:
        Remote.close_all([name])
    _validate(settings, name)


def _validate(settings: Settings, name: str) -> None:
    """Check the node as ``labctl validate`` does; exits 1 when it does not pass."""
    checked = check_new_node(settings, name)
    render_checked(checked)
    if not checked.valid:
        sys.exit(1)


def _restore(
    repo: ConfigRepo, past: tuple[Applied, str], folder: Path, dirs_given: str | None
) -> bool:
    """Bring the node's folder back from ``past`` (asked in a terminal); False: add it anew."""
    applied, path = past
    if is_terminal():
        answer = menu.confirm(
            f"{folder.name} was in the lab config at {applied.tag}. Restore it from there?",
            default=True,
        )
        if answer is None:
            raise click.ClickException("Not added")
        if not answer:
            return False
    if dirs_given:
        raise click.ClickException(
            f"{path} comes back from {applied.tag}, with its own DEPLOY_DIR and APPDATA_DIR; pass "
            "--no-restore to add the node anew"
        )
    try:
        repo.restore_path(applied.commit, path)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"[green]✓[/] restored {escape(path)} from {escape(applied.tag)}")
    return True


def _ask_secrets(looks: Sequence[str]) -> list[str]:
    """Of the .env keys that look secret, those kept in secrets.toml (asked one by one)."""
    kept = []
    for key in looks:
        answer = menu.confirm(f"{key} looks like a secret. Keep it in secrets.toml?", default=True)
        if answer is None:
            raise click.ClickException("Not added")
        if answer:
            kept.append(key)
    return kept


def _report(name: str, added: Added) -> None:
    imported = added.imported
    if imported is not None and imported.stacks:
        n = len(imported.stacks)
        console.print(
            f"[green]✓[/] added {escape(str(added.folder))}: {n} stack{'' if n == 1 else 's'} "
            f"imported ({len(imported.files)} files)"
        )
    else:
        console.print(f"[green]✓[/] added {escape(str(added.folder))}")
    if imported is not None:
        for note in imported.skipped:
            console.print(f"[yellow]![/] not imported: {escape(note)}")
        if imported.others:
            console.print(
                f"[yellow]![/] {escape(name)}: the first apply archives "
                f"{escape(', '.join(imported.others))} from DEPLOY_DIR (not in the lab config)"
            )
    if added.plain:
        console.print(
            f"[yellow]![/] {escape(name)}: {', '.join(added.plain)} look like secrets; kept in "
            "node.env as they are"
        )


def _ask(node: str, var: str, suggestion: str, what: str) -> str:
    answer = menu.edit_text(
        f"{var} for {node}",
        suggestion,
        header=f"{what} Saved to nodes/{node}/node.env.",
        validators=[lambda v: dir_problem(var, v)],
    )
    if answer is None:
        raise click.ClickException("Not added")
    return answer


def _check_node(node: str) -> Host | None:
    """What the node runs, with a warning when it cannot be reached (None), has no docker
    compose, or its ssh user may not run docker (a terminal is then asked whether to add it
    anyway; without one it is added, both dirs being given)."""
    try:
        host = read_host(node)
    except RemoteError as exc:
        console.print(f"[yellow]![/] {escape(node)}: not reachable ({escape(str(exc))})")
        return None
    if not host.docker:
        console.print(f"[yellow]![/] {escape(node)}: the ssh user is not in the docker group")
    elif not host.compose:
        console.print(f"[yellow]![/] {escape(node)}: no docker compose there")
    return host
