"""Bring a node that already runs compose into the lab config (labctl add): reading what it
runs, what to offer for its dirs, and writing its folder. Nothing here prints or asks.

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
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from labctl import docker
from labctl.bundle import (
    NO_SERVICES,
    REQUIRED_VARS,
    Location,
    check_dirs,
    find_stacks,
    include_paths,
    stack_candidates,
)
from labctl.changes import SECRET_NAME
from labctl.collect import read_node
from labctl.compose import ComposeError
from labctl.envfile import SECRET, node_env_file, parse_env, read_sources
from labctl.layout import HOME_POINTER
from labctl.remote import MARK, Remote, node_remote, sections
from labctl.repo import Applied, ConfigRepo
from labctl.runtime import Container, parse_containers
from labctl.settings import Secrets, Settings
from labctl.state import ALL, Stack

DEPLOY, APPDATA = REQUIRED_VARS
_NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

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


def read_host(node: str) -> Host:
    """
    Read what the node runs (read-only).

    Args:
        node (str):
            The node.

    Raises:
        RemoteError:
            The node cannot be reached.
    """
    return parse_host(node_remote(node).run_script(_READ_HOST))


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


#############################################################################
# Adding the node to the lab config
#############################################################################


def valid_name(name: str) -> bool:
    """Whether a node may be called this (an ssh host that is also a folder name; not 'all')."""
    return name != ALL and _NODE_NAME.fullmatch(name) is not None


def last_applied_folder(repo: ConfigRepo, nodes_dir: Path, name: str) -> tuple[Applied, str] | None:
    """The node's latest apply tag whose commit has its folder, and the folder's repo path (None:
    the lab config never had it)."""
    path = f"{nodes_dir.relative_to(repo.root).as_posix()}/{name}"
    for applied in repo.applied_tags(name):
        if repo.has_path(applied.commit, path):
            return applied, path
    return None


def suggest_deploy_dir(nodes_dir: Path, others: Sequence[str], host: Host | None) -> str:
    """The DEPLOY_DIR to offer: where the node's stacks run from, else what most nodes use."""
    return (offer_deploy_dir(host) if host else "") or _usual(nodes_dir, others, DEPLOY)


def suggest_appdata_dir(
    nodes_dir: Path, others: Sequence[str], host: Host | None, deploy_dir: str
) -> str:
    """The APPDATA_DIR to offer: where the node's containers keep their data, else what most
    nodes use, else appdata in DEPLOY_DIR."""
    return (
        (offer_appdata_dir(host.containers, deploy_dir) if host else "")
        or _usual(nodes_dir, others, APPDATA)
        or f"${DEPLOY}/appdata"
    )


def dir_problem(var: str, value: str) -> str | None:
    """Why ``value`` cannot be the node's DEPLOY_DIR or APPDATA_DIR (``var``), or None."""
    allow_deploy = var == APPDATA
    if value.startswith("/") or (allow_deploy and value.startswith(f"${DEPLOY}/")):
        return None
    return "must be an absolute path on the node" + (
        f" (or start with ${DEPLOY}/)" if allow_deploy else ""
    )


@dataclass(frozen=True)
class Added:
    """
    What adding a node came to.

    Attributes:
        folder (Path):
            The node's folder in the lab config, written.
        imported (Imported | None):
            What was read from the node; None when nothing was (not asked for, or not reachable).
        plain (tuple[str, ...]):
            The .env keys that look like secrets and were kept in node.env as they are.
    """

    folder: Path
    imported: Imported | None = None
    plain: tuple[str, ...] = ()


def add_node(
    settings: Settings,
    name: str,
    deploy_dir: str,
    appdata_dir: str,
    host: Host | None,
    others: Sequence[str],
    ask_secrets: Callable[[Sequence[str]], list[str]] | None = None,
) -> Added:
    """
    Write nodes/NAME: node.env with its dirs, and the stacks the node runs (else an empty compose
    file whose project name follows the other nodes'). Nothing is committed.

    Args:
        settings (Settings):
            labctl's settings.
        name (str):
            The node.
        deploy_dir (str):
            Its DEPLOY_DIR.
        appdata_dir (str):
            Its APPDATA_DIR, as it goes into node.env (may start with $DEPLOY_DIR/).
        host (Host | None):
            What the node runs, to import its stacks; None to start it empty.
        others (Sequence[str]):
            The lab config's other nodes.
        ask_secrets (Callable[[Sequence[str]], list[str]] | None):
            Given the imported .env keys that look like secrets, returns those to keep in
            secrets.toml; None keeps them all in node.env.

    Returns:
        Added:
            What was written, and what was left out.

    Raises:
        ComposeError:
            The dirs cannot be used, or the node's stacks cannot be resolved.
        RemoteError:
            The node could not be read.
    """
    for var, value in ((DEPLOY, deploy_dir), (APPDATA, appdata_dir)):
        problem = dir_problem(var, value)
        if problem:
            raise ComposeError(f"{var}: {problem}")
    prefix = f"${DEPLOY}/"
    appdata_path = (
        deploy_dir.rstrip("/") + "/" + appdata_dir[len(prefix) :]
        if appdata_dir.startswith(prefix)
        else appdata_dir
    )
    check_dirs(name, deploy_dir, appdata_path)
    nodes_dir = settings.nodes_dir

    with tempfile.TemporaryDirectory(prefix="labctl-add-") as tmp:
        imported = None
        if host is not None:
            imported = read_stacks(
                settings,
                node_remote(name),
                host,
                deploy_dir,
                _data_names(deploy_dir, appdata_path),
                Path(tmp) / "node",
            )
        if imported is not None:
            _leave_to_the_root(settings.lab_config, imported)
        stacks = imported is not None and bool(imported.stacks)
        env = dict(imported.env) if imported is not None and stacks else {}
        looks = [key for key, value in env.items() if value and SECRET_NAME.search(key)]
        secret = ask_secrets(looks) if ask_secrets else []
        _write_folder(
            nodes_dir, others, name, deploy_dir, appdata_dir, imported if stacks else None
        )
    shared, _ = read_sources(nodes_dir, name)
    known = {key: value for values in shared for key, value in values.items()}
    lines = env_lines(env, known, secret, REQUIRED_VARS)
    if lines:
        with node_env_file(nodes_dir, name).open("a", encoding="utf-8") as out:
            out.write("\n" + "".join(f"{line}\n" for line in lines))
    if secret:
        secrets = Secrets.load(settings.secrets_file)
        secrets.table(name).update({key: env[key] for key in secret})
        secrets.save(settings.secrets_file)
    return Added(nodes_dir / name, imported, tuple(key for key in looks if key not in secret))


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


def _write_folder(
    nodes_dir: Path,
    others: Sequence[str],
    name: str,
    deploy_dir: str,
    appdata_dir: str,
    imported: Imported | None,
) -> None:
    """nodes/NAME: node.env with the dirs, and the stack files imported (else a compose file
    whose project name follows the other nodes')."""
    folder = nodes_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    node_env_file(nodes_dir, name).write_text(
        f"{DEPLOY}={deploy_dir}\n{APPDATA}={appdata_dir}\n", encoding="utf-8"
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


def _data_names(deploy_dir: str, appdata_dir: str) -> list[str]:
    """APPDATA_DIR's top-level name in DEPLOY_DIR, when it is inside it."""
    home, path = PurePosixPath(deploy_dir), PurePosixPath(appdata_dir)
    return [path.relative_to(home).parts[0]] if path != home and path.is_relative_to(home) else []


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _usual(nodes_dir: Path, nodes: Sequence[str], var: str) -> str:
    """The value most nodes give ``var`` in their node.env, as written ($DEPLOY_DIR kept)."""
    raw = (parse_env(_read(node_env_file(nodes_dir, node))) for node in nodes)
    values = Counter(env[var] for env in raw if env.get(var))
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
