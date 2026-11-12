"""labctl add NODE: add a node to the lab config, taking in the stacks it already runs."""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import click
import yaml
from rich.markup import escape

from labctl.adopt import (
    Host,
    Imported,
    env_lines,
    offer_appdata_dir,
    offer_deploy_dir,
    read_host,
    read_stacks,
)
from labctl.bundle import REQUIRED_VARS, check_dirs
from labctl.changes import SECRET_NAME
from labctl.cli import menu
from labctl.cli.commands.settings import load_settings
from labctl.cli.commands.validate import check_node
from labctl.click import DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import node_env_file, parse_env, read_sources
from labctl.remote import Remote, RemoteError, node_remote
from labctl.repo import Applied, ConfigRepo, RepoError
from labctl.settings import Secrets, SettingsError
from labctl.state import ALL

_NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DEPLOY, APPDATA = REQUIRED_VARS


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
    if name == ALL or not _NODE_NAME.fullmatch(name):
        raise click.ClickException(f"'{name}' cannot be a node name")
    folder = settings.nodes_dir / name
    if folder.exists() and any(folder.iterdir()):
        raise click.ClickException(f"{folder} already exists")
    try:
        others = settings.node_names()
        repo = ConfigRepo.open(settings.lab_config)
        past = None if no_restore else _past(repo, settings.nodes_dir, name)
    except (SettingsError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    if past is not None and _restore(repo, past, folder, deploy_dir or appdata_dir):
        with tempfile.TemporaryDirectory(prefix="labctl-add-") as tmp:
            if not check_node(settings, name, Secrets.load(settings.secrets_file), Path(tmp)):
                sys.exit(1)
        return
    if not _tty() and not (deploy_dir and appdata_dir):
        raise click.ClickException(
            "No terminal to ask in: pass --deploy-dir and --appdata-dir (labctl does not guess "
            "where the stacks or the data go)"
        )
    raw = {other: parse_env(_read(node_env_file(settings.nodes_dir, other))) for other in others}

    remote = node_remote(name)
    try:
        host = _check_node(name, remote)
        usable = host is not None and host.docker and bool(host.compose)
        if not usable and _tty():
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
            (offer_deploy_dir(host) if host else "") or _usual(raw, DEPLOY),
            "Where labctl puts its stacks.",
        )
        appdata = appdata_dir or _ask(
            name,
            APPDATA,
            (offer_appdata_dir(host.containers, deploy) if host else "")
            or _usual(raw, APPDATA)
            or f"${DEPLOY}/appdata",
            "The services' data: what labctl migrates and archives.",
        )
        for var, value in ((DEPLOY, deploy), (APPDATA, appdata)):
            problem = _node_path(value, allow_deploy=var == APPDATA)
            if problem:
                raise click.ClickException(f"{var}: {problem}")
        appdata_path = _expand(appdata, deploy)
        try:
            check_dirs(name, deploy, appdata_path)
        except ComposeError as exc:
            raise click.ClickException(str(exc)) from exc

        with tempfile.TemporaryDirectory(prefix="labctl-add-") as tmp:
            imported = None
            if not no_import:
                if host is None:
                    console.print(f"[yellow]![/] {escape(name)}: nothing imported")
                else:
                    try:
                        imported = read_stacks(
                            settings,
                            remote,
                            host,
                            deploy,
                            _data_names(deploy, appdata_path),
                            Path(tmp) / "node",
                        )
                    except (RemoteError, ComposeError) as exc:
                        raise click.ClickException(str(exc)) from exc
            stacks = imported is not None and bool(imported.stacks)
            if imported is not None:
                _leave_to_the_root(settings.lab_config, imported)
            env = dict(imported.env) if imported is not None and stacks else {}
            looks = [key for key, value in env.items() if value and SECRET_NAME.search(key)]
            secret = _ask_secrets(looks) if _tty() else []
            _write(settings.nodes_dir, others, name, deploy, appdata, imported if stacks else None)
            lines = env_lines(env, _shared(settings.nodes_dir, name), secret, REQUIRED_VARS)
            if lines:
                with node_env_file(settings.nodes_dir, name).open("a", encoding="utf-8") as out:
                    out.write("\n" + "".join(f"{line}\n" for line in lines))
            if secret:
                secrets = Secrets.load(settings.secrets_file)
                secrets.table(name).update({key: env[key] for key in secret})
                secrets.save(settings.secrets_file)
            _report(name, folder, imported)
            plain = [key for key in looks if key not in secret]
            if plain:
                console.print(
                    f"[yellow]![/] {escape(name)}: {', '.join(plain)} look like secrets; kept in "
                    "node.env as they are"
                )
    finally:
        Remote.close_all([name])
    with tempfile.TemporaryDirectory(prefix="labctl-add-") as tmp:
        if not check_node(settings, name, Secrets.load(settings.secrets_file), Path(tmp)):
            sys.exit(1)


def _past(repo: ConfigRepo, nodes_dir: Path, name: str) -> tuple[Applied, str] | None:
    """The node's latest apply tag whose commit has its folder, and the folder's repo path (None:
    the lab config never had it)."""
    path = f"{nodes_dir.relative_to(repo.root).as_posix()}/{name}"
    for applied in repo.applied_tags(name):
        if repo.has_path(applied.commit, path):
            return applied, path
    return None


def _restore(
    repo: ConfigRepo, past: tuple[Applied, str], folder: Path, dirs_given: str | None
) -> bool:
    """Bring the node's folder back from ``past`` (asked in a terminal); False: add it anew."""
    applied, path = past
    if _tty():
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


def _leave_to_the_root(lab_config: Path, imported: Imported) -> None:
    """Imported paths under a top-level name the lab config has itself (a node labctl applied
    from another config, say) are not copied: the bundle takes the name from the root, and a node
    folder may not have it too. One the root has otherwise, or not at all, is named."""
    kept = []
    for path in imported.files:
        top = path.split("/", 1)[0]
        if not (lab_config / top).exists():
            kept.append(path)
            continue
        ours = lab_config / path
        if not ours.is_file():
            imported.skipped.append(f"{path}: the lab config's {top} has no such file")
        elif ours.read_bytes() != (imported.folder / path).read_bytes():
            imported.skipped.append(f"{path}: the lab config's {top} has it, different")
    imported.files = tuple(kept)


def _write(
    nodes_dir: Path,
    others: Sequence[str],
    name: str,
    deploy: str,
    appdata: str,
    imported: Imported | None,
) -> None:
    """nodes/NAME: node.env with the dirs, and the stack files imported (else a compose file
    whose project name follows the other nodes')."""
    folder = nodes_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    node_env_file(nodes_dir, name).write_text(
        f"{DEPLOY}={deploy}\n{APPDATA}={appdata}\n", encoding="utf-8"
    )
    if imported is not None:
        for path in imported.files:
            target = folder / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(imported.folder / path, target)
        return
    project = _project_prefix(nodes_dir, others) + name
    (folder / "docker-compose.yml").write_text(
        f"name: {project}\n\n# {name}'s services: include files of the lab config, or define "
        "them here.\n",
        encoding="utf-8",
    )


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


def _report(name: str, folder: Path, imported: Imported | None) -> None:
    if imported is not None and imported.stacks:
        n = len(imported.stacks)
        console.print(
            f"[green]✓[/] added {escape(str(folder))}: {n} stack{'' if n == 1 else 's'} "
            f"imported ({len(imported.files)} files)"
        )
    else:
        console.print(f"[green]✓[/] added {escape(str(folder))}")
    if imported is None:
        return
    for note in imported.skipped:
        console.print(f"[yellow]![/] not imported: {escape(note)}")
    if imported.others:
        console.print(
            f"[yellow]![/] {escape(name)}: the first apply archives "
            f"{escape(', '.join(imported.others))} from DEPLOY_DIR (not in the lab config)"
        )


def _expand(appdata: str, deploy: str) -> str:
    prefix = f"${DEPLOY}/"
    return (
        deploy.rstrip("/") + "/" + appdata[len(prefix) :] if appdata.startswith(prefix) else appdata
    )


def _data_names(deploy: str, appdata: str) -> list[str]:
    """APPDATA_DIR's top-level name in DEPLOY_DIR, when it is inside it."""
    home, path = PurePosixPath(deploy), PurePosixPath(appdata)
    return [path.relative_to(home).parts[0]] if path != home and path.is_relative_to(home) else []


def _shared(nodes_dir: Path, node: str) -> dict[str, str]:
    shared, _ = read_sources(nodes_dir, node)
    return {key: value for values in shared for key, value in values.items()}


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
    answer = menu.edit_text(
        f"{var} for {node}",
        suggestion,
        header=f"{what} Saved to nodes/{node}/node.env.",
        validators=[lambda v: _node_path(v, allow_deploy=var == APPDATA)],
    )
    if answer is None:
        raise click.ClickException("Not added")
    return answer


def _check_node(node: str, remote: Remote) -> Host | None:
    """What the node runs, with a warning when it cannot be reached (None), has no docker
    compose, or its ssh user may not run docker (a terminal is then asked whether to add it
    anyway; without one it is added, both dirs being given)."""
    try:
        host = read_host(remote)
    except RemoteError as exc:
        console.print(f"[yellow]![/] {escape(node)}: not reachable ({escape(str(exc))})")
        return None
    if not host.docker:
        console.print(f"[yellow]![/] {escape(node)}: the ssh user is not in the docker group")
    elif not host.compose:
        console.print(f"[yellow]![/] {escape(node)}: no docker compose there")
    return host


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
