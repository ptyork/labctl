"""Bring a node that already runs compose into the lab config (labctl add).

What the node runs is read once (read-only): every container's compose labels and bind mounts,
where a labctl apply put DEPLOY_DIR (~/.labctl-home), and whether docker compose is there for the
ssh user. From that come the offers for DEPLOY_DIR and APPDATA_DIR, and the import: the stack files
at the top of DEPLOY_DIR (the rule plan uses), with what they refer to, and their `.env`.

Only what fits labctl's layout is imported; the rest is named with why. labctl's stacks are the
top-level files of DEPLOY_DIR, and moving compose files breaks relative paths, so nothing is
restructured.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from labctl import docker
from labctl.bundle import NO_SERVICES, Location, find_stacks, include_paths, stack_candidates
from labctl.collect import read_node
from labctl.envfile import SECRET, parse_env
from labctl.layout import HOME_POINTER
from labctl.remote import MARK, Remote, sections
from labctl.runtime import Container, parse_containers
from labctl.settings import Settings
from labctl.state import Stack

# Bind sources that are the host's, not a service's data: never offered as APPDATA_DIR.
SYSTEM_PATHS = ("/var/run", "/run", "/etc", "/dev", "/proc", "/sys", "/usr", "/lib", "/var/lib")

_READ_HOST = f"""\
echo "{MARK} pointer"; cat "$HOME/{HOME_POINTER}" 2>/dev/null
echo "{MARK} compose"; {docker.COMPOSE_VERSION} </dev/null 2>/dev/null
echo "{MARK} user"; id -u; id -nG
echo "{MARK} containers"
{docker.INSPECT_CONTAINERS}
"""


#############################################################################


# What the node runs


#############################################################################


@dataclass(frozen=True)
class Project:
    """A compose project with containers on the node: where it runs from, and its files."""

    name: str
    working_dir: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Host:
    """What labctl add reads from a node before asking anything."""

    pointer: str = ""  # where a labctl apply put DEPLOY_DIR ("": none)
    compose: str = ""  # docker compose's version ("": not there)
    docker: bool = False  # the ssh user may run docker (root, or in the docker group)
    containers: tuple[Container, ...] = ()

    @property
    def projects(self) -> tuple[Project, ...]:
        """The compose projects, by name."""
        found: dict[str, Project] = {}
        for container in self.containers:
            if container.project and container.project not in found:
                found[container.project] = Project(
                    container.project, container.working_dir, container.files
                )
        return tuple(found[name] for name in sorted(found))


def read_host(remote: Remote) -> Host:
    """
    Read what the node runs (read-only).

    Args:
        remote (Remote):
            The node's shell.

    Raises:
        RemoteError:
            The node cannot be reached.
    """
    return parse_host(remote.run_script(_READ_HOST))


def parse_host(output: str) -> Host:
    parts = sections(output)
    user = [line for line in parts.get("user", []) if line.strip()]
    root = bool(user) and user[0].strip() == "0"
    groups = user[1].split() if len(user) > 1 else []
    return Host(
        pointer="".join(parts.get("pointer", [])).strip(),
        compose="".join(parts.get("compose", [])).strip(),
        docker=root or docker.GROUP in groups,
        containers=parse_containers(parts.get("containers", []), ""),
    )


def offer_deploy_dir(host: Host) -> str:
    """Where a labctl apply put DEPLOY_DIR, else the folder most compose projects run from ("":
    neither)."""
    if host.pointer:
        return host.pointer
    dirs = Counter(project.working_dir for project in host.projects if project.working_dir)
    if not dirs:
        return ""
    most = max(dirs.values())
    return min(folder for folder, count in dirs.items() if count == most)


def offer_appdata_dir(containers: Iterable[Container], deploy_dir: str) -> str:
    """
    The folder whose direct children the services bind-mount the most ("": no such folder).

    ``/srv/docker/app_data/{heimdall,npm}/...`` gives ``/srv/docker/app_data``. Host paths (the
    docker socket, /etc, /dev, ...) do not count. Inside DEPLOY_DIR it is written
    ``$DEPLOY_DIR/...``.

    Args:
        containers (Iterable[Container]):
            The node's containers.
        deploy_dir (str):
            The DEPLOY_DIR chosen.
    """
    children: dict[PurePosixPath, set[str]] = {}
    for container in containers:
        for _, source in container.binds:
            path = PurePosixPath(source)
            if not path.is_absolute() or _system(source):
                continue
            for depth in range(1, len(path.parts) - 1):
                parent = PurePosixPath(*path.parts[: depth + 1])
                children.setdefault(parent, set()).add(path.parts[depth + 1])
    # Most children; a tie goes to the deeper folder (more specific).
    ranked = [(len(names), len(folder.parts), folder) for folder, names in children.items()]
    if not ranked:
        return ""
    count, _, best = max(ranked)
    if count < 2:
        return ""
    home = PurePosixPath(deploy_dir)
    if deploy_dir and best != home and best.is_relative_to(home):
        return f"$DEPLOY_DIR/{best.relative_to(home)}"
    return str(best)


def _system(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in SYSTEM_PATHS)


#############################################################################


# The import


#############################################################################


@dataclass
class Imported:
    """What labctl add takes from the node's DEPLOY_DIR."""

    folder: Path  # the local copy the files come from
    stacks: tuple[Stack, ...] = ()
    files: tuple[str, ...] = ()  # DEPLOY_DIR-relative paths to copy into nodes/NODE (files)
    env: dict[str, str] = field(default_factory=dict)  # the stacks' .env
    skipped: list[str] = field(default_factory=list)  # what is not imported, and why
    others: tuple[str, ...] = ()  # other top-level names, not data: the first apply archives them


def read_stacks(
    settings: Settings,
    remote: Remote,
    host: Host,
    deploy_dir: str,
    data: Sequence[str],
    folder: Path,
) -> Imported | None:
    """
    Read the node's stacks in DEPLOY_DIR, and what they refer to, into ``folder`` (read-only on
    the node).

    Args:
        settings (Settings):
            labctl's settings (the docker CLI that resolves the stacks here).
        remote (Remote):
            The node's shell.
        host (Host):
            What the node runs (stacks run from elsewhere are named, not imported).
        deploy_dir (str):
            The DEPLOY_DIR chosen.
        data (Sequence[str]):
            Top-level names of DEPLOY_DIR that hold data (APPDATA_DIR's, when inside it).
        folder (Path):
            A new local folder for the copy.

    Returns:
        Imported | None:
            None when DEPLOY_DIR does not exist on the node.

    Raises:
        RemoteError:
            The node cannot be read.
    """
    on_node = read_node(remote, deploy_dir, (), data)
    if not on_node.exists:
        return None
    kinds = {name: kind for name, kind in on_node.names.items() if kind != "data"}
    folder.mkdir(parents=True)
    fetched: set[str] = set()

    def fetch(names: Iterable[str]) -> None:
        wanted = sorted(set(names) & kinds.keys() - fetched)
        if wanted:
            remote.download(deploy_dir, wanted, folder)
            fetched.update(wanted)

    fetch(
        name
        for name, kind in kinds.items()
        if kind == "file" and (name.startswith(".") or name.endswith((".yml", ".yaml", ".env")))
    )
    skipped: list[str] = []
    # What the top-level compose files refer to, until nothing new is reached.
    pending = [name for name in sorted(fetched) if name.endswith((".yml", ".yaml"))]
    seen: set[str] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        refs, notes = references(folder, path)
        skipped += notes
        fetch(ref.split("/", 1)[0] for ref in refs)
        pending += [ref for ref in refs if ref.endswith((".yml", ".yaml"))]

    location = Location(str(folder), docker_cli=settings.docker_cli)
    stacks, not_stacks = find_stacks(location, stack_candidates(location, location.names()))
    files: set[str] = set()
    for stack in stacks:
        files |= closure(folder, stack.files)
    # A node's dot names are never shipped; .env is the one labctl writes from node.env.
    for path in sorted(path for path in files if path.startswith(".")):
        files.discard(path)
        if path != ".env":
            skipped.append(f"{path}: labctl does not ship dot names at the top of a node")
    for name, why in sorted(not_stacks.items()):
        if why != NO_SERVICES:
            skipped.append(f"{name}: does not resolve ({why.strip().splitlines()[-1]})")

    home = deploy_dir.rstrip("/") + "/"
    for project in host.projects:
        if project.files and not all(file.startswith(home) for file in project.files):
            where = project.working_dir or posixpath.dirname(project.files[0])
            skipped.append(
                f"stack {project.name} runs from {where}: labctl's stacks are top-level files of "
                "DEPLOY_DIR"
            )
    used = {path.split("/", 1)[0] for path in files} | {".env"}
    env_text = (folder / ".env").read_text(encoding="utf-8") if (folder / ".env").is_file() else ""
    return Imported(
        folder,
        tuple(stacks),
        tuple(sorted(files)),
        parse_env(env_text),
        sorted(set(skipped)),
        tuple(sorted(name for name in kinds if name not in used and name != ".labctl")),
    )


def closure(folder: Path, start: Iterable[str]) -> set[str]:
    """``start`` and every file they refer to, transitively (paths relative to ``folder``)."""
    found: set[str] = set()
    pending = list(start)
    while pending:
        path = pending.pop()
        if path in found or not (folder / path).exists():
            continue
        if (folder / path).is_dir():
            found |= {
                entry.relative_to(folder).as_posix()
                for entry in (folder / path).rglob("*")
                if entry.is_file()
            }
            continue
        found.add(path)
        if path.endswith((".yml", ".yaml")):
            pending += references(folder, path)[0]
    return found


def references(folder: Path, path: str) -> tuple[list[str], list[str]]:
    """
    What a compose file refers to by a relative path: includes, `extends`, `env_file`, build
    contexts and dockerfiles, config and secret files.

    Returns:
        tuple[list[str], list[str]]:
            The paths, relative to ``folder``; and notes on what cannot be imported (outside the
            folder, a variable in the path, a relative bind mount).
    """
    try:
        text = (folder / path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    base = posixpath.dirname(path)
    raw: list[str] = include_paths(text)
    notes: list[str] = []
    services = data.get("services")
    for service_name, service in (services if isinstance(services, dict) else {}).items():
        if not isinstance(service, dict):
            continue
        raw += _paths(service.get("env_file"))
        extends = service.get("extends")
        if isinstance(extends, dict) and isinstance(extends.get("file"), str):
            raw.append(extends["file"])
        build = service.get("build")
        if isinstance(build, str):
            raw.append(build)
        elif isinstance(build, dict):
            given = build.get("context")
            context = given if isinstance(given, str) else "."
            raw.append(context)
            if isinstance(build.get("dockerfile"), str):
                raw.append(posixpath.join(context, build["dockerfile"]))
        for volume in service.get("volumes") or []:
            source = volume.split(":", 1)[0] if isinstance(volume, str) else ""
            if isinstance(volume, dict) and volume.get("type") == "bind":
                source = str(volume.get("source", ""))
            if source.startswith(("./", "../")) or source == ".":
                notes.append(
                    f"{path}: {service_name} bind-mounts {source} (a relative path: not imported)"
                )
    for section in ("configs", "secrets"):
        entries = data.get(section)
        for entry in (entries if isinstance(entries, dict) else {}).values():
            if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                raw.append(entry["file"])
    found: list[str] = []
    for entry in raw:
        if "$" in entry:
            notes.append(f"{path}: {entry} has a variable in its path (not imported)")
            continue
        target = posixpath.normpath(posixpath.join(base, entry))
        if entry.startswith("/") or target == ".." or target.startswith("../"):
            notes.append(f"{path}: {entry} is outside DEPLOY_DIR (not imported)")
            continue
        if target != ".":
            found.append(target)
    return found, notes


def _paths(value: Any) -> list[str]:
    """A short (string) or long (list of strings or of {path: ...}) compose path entry."""
    if isinstance(value, str):
        return [value]
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            found.append(item["path"])
    return found


def env_lines(
    env: Mapping[str, str], shared: Mapping[str, str], secret: Iterable[str], skip: Iterable[str]
) -> list[str]:
    """
    The stacks' `.env` as node.env lines: values `_shared/*.env` gives alike are left out, a
    secret is `KEY=_SECRET_` (its value goes to secrets.toml).

    Args:
        env (Mapping[str, str]):
            The `.env` values.
        shared (Mapping[str, str]):
            What `_shared/*.env` gives.
        secret (Iterable[str]):
            The keys kept in secrets.toml.
        skip (Iterable[str]):
            Keys written otherwise (DEPLOY_DIR, APPDATA_DIR).
    """
    secret, skip = set(secret), set(skip)
    lines = []
    for key, value in env.items():
        if key in skip or (key in shared and shared[key] == value and key not in secret):
            continue
        lines.append(f"{key}={SECRET}" if key in secret else f"{key}={value}")
    return lines
