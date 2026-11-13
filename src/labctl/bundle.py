"""Desired state: build a node's staging bundle locally and resolve it with compose; and finding
stacks in any folder of compose files (a bundle, or a node's DEPLOY_DIR).

The bundle is laid out exactly as it will be in the node's DEPLOY_DIR:

    <bundle>/...      <- everything in the lab config, verbatim (except nodes/ and dotfiles)
    <bundle>/...      <- everything in nodes/<node>/, verbatim (except node.env and dotfiles)
    <bundle>/.env     <- compiled env

nodes/ is the only convention. Stacks are found, not declared: each top-level yml that compose can
resolve on its own and that defines services is a stack file, unless another top-level file
includes it. Its project name (`name:`, else the folder's name) says which stack it belongs to.
How the rest is organised (apps/, base/, includes, extends) is up to the compose files; labctl only
sees compose's resolved output.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from labctl import docker
from labctl.compose import ComposeError, reduce_stacks, relocate
from labctl.envfile import (
    HOST_IP,
    NODE_ENV,
    PLACEHOLDER_PREFIX,
    CompiledEnv,
    Prompt,
    append_node_vars,
    compile_env,
    node_env_file,
    read_sources,
    render_env,
)
from labctl.hostip import host_ip
from labctl.remote import Remote, RemoteError, Result, execute, node_remote
from labctl.settings import Secrets, Settings
from labctl.state import Dirs, NodeState, Stack

# Variables labctl itself needs, whether or not a compose file references them.
REQUIRED_VARS = ("DEPLOY_DIR", "APPDATA_DIR")

# Prompt defaults for missing variables: offer another variable's value (not used silently).
SUGGESTIONS = {"DEPLOY_DIR": "APPDATA_DIR"}

# Why a top-level yml that resolves is not a stack.
NO_SERVICES = "defines no services"

# Host environment passed to a local docker CLI. Everything else is dropped so the controller's
# shell (TZ, PUID, ...) cannot leak into interpolation: compose prefers the shell over .env.
_PASS_ENV = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "XDG_RUNTIME_DIR",
    "WSLENV",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
)


@dataclass(frozen=True)
class Desired:
    node: str
    bundle: Path
    env: CompiledEnv
    configs: dict[str, dict[str, Any]]  # stack name -> resolved config (paths as on the node)
    state: NodeState
    files: dict[str, str]  # bundle-relative path -> sha256, .env included
    not_stacks: dict[str, str] = field(default_factory=dict)  # node yml -> why it is not a stack

    @property
    def dirs(self) -> Dirs:
        return Dirs(self.env.values["DEPLOY_DIR"], self.env.values["APPDATA_DIR"])

    @property
    def top_level(self) -> tuple[str, ...]:
        """The bundle's names at the top of DEPLOY_DIR. Apply archives anything else there, except
        .labctl and data (see collect.data_names)."""
        return top_level(self.files)

    def service_config(self, service: str) -> dict[str, Any]:
        stack = self.state.services[service].stack
        definition: dict[str, Any] = (self.configs[stack].get("services") or {})[service]
        return definition


def top_level(files: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({path.split("/", 1)[0] for path in files}))


def retired(node: str, dirs: Dirs, workdir: Path, values: Mapping[str, str]) -> Desired:
    """
    A node being retired (its folder is gone from nodes/): nothing on it, its env as last applied
    (so the folders it points at, LOGS_DIR and the like, stay data).

    Args:
        node (str):
            The node.
        dirs (Dirs):
            DEPLOY_DIR and APPDATA_DIR, from its latest apply tag.
        workdir (Path):
            Where its (empty) bundle folder is made.
        values (Mapping[str, str]):
            Its env at its latest apply (no secrets needed: only paths count).
    """
    bundle = workdir / node / (PurePosixPath(dirs.deploy_dir).name or "docker")
    bundle.mkdir(parents=True)
    env = CompiledEnv(
        values={**values, "DEPLOY_DIR": dirs.deploy_dir, "APPDATA_DIR": dirs.appdata_dir}
    )
    return Desired(node, bundle, env, {}, NodeState.of(node), {})


@dataclass(frozen=True)
class Location:
    """A folder of compose files: on this machine, or on a node when ``remote`` is set."""

    root: str
    remote: Remote | None = None
    docker_cli: str = "docker"  # for local runs; nodes run `docker`

    def run(self, *args: str) -> Result:
        env = None if self.remote else {k: v for k, v in os.environ.items() if k in _PASS_ENV}
        return execute(args, self.root, self.remote, env)

    def compose(self, *args: str) -> str:
        cli = "docker" if self.remote else self.docker_cli
        result = self.run(*docker.compose(*args, cli=cli))
        if result.code:
            raise ComposeError(f"docker compose {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout

    def names(self) -> list[str]:
        """The folder's top-level names."""
        if self.remote is None:
            return sorted(entry.name for entry in Path(self.root).iterdir())
        result = self.run("ls", "-A")
        if result.code:
            raise RemoteError(f"{self.remote.host}: {self.root}: {result.stderr.strip()}")
        return sorted(result.stdout.splitlines())

    def read(self, path: str) -> str | None:
        """A file's text, or None if it cannot be read."""
        if self.remote is None:
            try:
                return (Path(self.root) / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
        result = self.run("cat", "--", path)
        return result.stdout if result.code == 0 else None


def resolve_desired(
    settings: Settings,
    node: str,
    secrets: Secrets,
    workdir: Path,
    prompt: Prompt | None,
    source: Path | None = None,
    baseline: bool = False,
    address: str = "",
    offline: bool = False,
) -> Desired:
    """
    The node's bundle, built from a copy of the lab config, and resolved.

    Answers given at the prompt are written to the lab config's node.env and secrets.toml.

    Args:
        settings (Settings):
            labctl's settings (the lab config, the docker CLI).
        node (str):
            The node's name.
        secrets (Secrets):
            The values for ``_SECRET_`` variables.
        workdir (Path):
            Where the bundle is built.
        prompt (Prompt | None):
            Asks for missing values (None: placeholders).
        source (Path | None):
            The copy of the lab config: the working tree's, or a commit's (default: the lab config
            itself).
        baseline (bool):
            Rebuilding what an earlier apply shipped: no prompt, nothing written, and `_HOST_IP_`
            is not looked up (default: False).
        address (str):
            With ``baseline``, the `_HOST_IP_` the earlier apply used (default: a placeholder).
        offline (bool):
            `_HOST_IP_` is not looked up, but left a placeholder (validate) (default: False).

    Returns:
        Desired:
            The bundle's folder, its compiled env, each stack's resolved config, the state the
            repo wants and the bundle's files.

    Raises:
        ComposeError:
            The node's files do not resolve, or DEPLOY_DIR and APPDATA_DIR are not valid.
    """
    source = source or settings.lab_config
    nodes_dir = source / "nodes"
    staging = workdir / node / "bundle"
    node_files = _build_bundle(source, nodes_dir / node, staging)
    cli = settings.docker_cli

    not_stacks: dict[str, str] = {}
    staged = Location(str(staging), docker_cli=cli)
    candidates = stack_candidates(staged, node_files)
    referenced: set[str] = set()
    for name in list(candidates):
        try:
            referenced.update(compose_variables(staged, name))
        except ComposeError as exc:
            not_stacks[name] = str(exc)
            candidates.remove(name)

    shared, node_vars = read_sources(nodes_dir, node)
    env = compile_env(
        node,
        shared,
        node_vars,
        secrets,
        [*sorted(referenced), *REQUIRED_VARS],
        None if baseline else prompt,
        SUGGESTIONS,
        host_ip=(lambda: address or PLACEHOLDER_PREFIX + HOST_IP)
        if baseline or offline
        else lambda: host_ip(node_remote(node)),
    )
    # One-time local writes of answers given at the prompt.
    if env.new_node_vars:
        append_node_vars(node_env_file(settings.nodes_dir, node), env.new_node_vars)
    if env.new_secrets:
        for key, (value, is_global) in env.new_secrets.items():
            secrets.table(None if is_global else node)[key] = value
        secrets.save(settings.secrets_file)

    for name in REQUIRED_VARS:
        if name not in env.placeholders and not env.values[name].startswith("/"):
            raise ComposeError(f"{node}: {name} must be an absolute path, got '{env.values[name]}'")
    check_dirs(node, env.values["DEPLOY_DIR"], env.values["APPDATA_DIR"])

    # A stack file without `name:` takes its folder's name: on the node that is DEPLOY_DIR's, so
    # the bundle folder gets the same name.
    home = env.values["DEPLOY_DIR"]
    bundle = workdir / node / (PurePosixPath(home).name or "docker")
    staging.rename(bundle)
    env_file = bundle / ".env"
    env_file.write_text(render_env(env.values, node), encoding="utf-8")
    env_file.chmod(0o600)

    location = Location(str(bundle), docker_cli=cli)
    stacks, failed = find_stacks(location, candidates)
    not_stacks |= failed
    if env.placeholders and not_stacks and not baseline:  # a baseline has no secrets: expected
        details = "\n".join(f"{name}: {error}" for name, error in not_stacks.items())
        raise ComposeError(
            f"{node}: no value for {', '.join(env.placeholders)} (run plan in a terminal to be "
            f"prompted, or add them to node.env); compose cannot resolve without them:\n{details}"
        )
    resolved = resolve_stacks(location, stacks, home)
    state = reduce_stacks(node, resolved, env.values["APPDATA_DIR"])
    configs = {stack.name: config for stack, config in resolved}
    return Desired(node, bundle, env, configs, state, manifest(bundle), not_stacks)


def check_dirs(node: str, deploy_dir: str, appdata_dir: str) -> None:
    """APPDATA_DIR must be independent of DEPLOY_DIR or inside it. The other way round, DEPLOY_DIR
    would hold data among the files apply replaces and archives."""
    home, appdata = PurePosixPath(deploy_dir), PurePosixPath(appdata_dir)
    if appdata.is_absolute() and home.is_relative_to(appdata):
        where = "is" if home == appdata else "is inside"
        raise ComposeError(
            f"{node}: DEPLOY_DIR ({deploy_dir}) {where} APPDATA_DIR ({appdata_dir}); APPDATA_DIR "
            "must be independent of DEPLOY_DIR or a subdirectory of it (e.g. "
            "APPDATA_DIR=$DEPLOY_DIR/app_data)"
        )


def discover_stacks(location: Location) -> tuple[list[Stack], dict[str, str]]:
    """The stacks in a folder's top-level yml files, and why the other ymls are not stacks."""
    return find_stacks(location, stack_candidates(location, location.names()))


def find_stacks(
    location: Location, candidates: Iterable[str]
) -> tuple[list[Stack], dict[str, str]]:
    """Group the candidates that resolve (and define services) by project name."""
    not_stacks: dict[str, str] = {}
    files_by_name: dict[str, list[str]] = {}
    for name in candidates:
        try:
            config = json.loads(location.compose("-f", name, "config", "--format", "json"))
        except ComposeError as exc:
            not_stacks[name] = str(exc)
            continue
        if not config.get("services"):
            not_stacks[name] = NO_SERVICES
            continue
        files_by_name.setdefault(str(config["name"]), []).append(name)
    stacks = [Stack(name, tuple(files)) for name, files in sorted(files_by_name.items())]
    return stacks, not_stacks


def resolve_stacks(
    location: Location, stacks: Sequence[Stack], deploy_dir: str
) -> list[tuple[Stack, dict[str, Any]]]:
    """Each stack's resolved config, in a bundle or on the node.

    Paths under the folder in compose's output are rewritten to ``deploy_dir``, so the result is
    what compose resolves on the node, wherever the folder is.
    """
    resolved = []
    for stack in stacks:
        output = location.compose(*docker.stack_args(stack), "config", "--format", "json")
        resolved.append((stack, relocate(json.loads(output), location.root, deploy_dir)))
    return resolved


def stack_candidates(location: Location, names: Iterable[str]) -> list[str]:
    """Top-level yml files, except disabled ones and those another top-level file includes
    (directly or through other top-level files)."""
    texts: dict[str, str] = {}
    for name in sorted(names):
        if name.endswith((".yml", ".yaml")) and not name.endswith(
            (".disabled.yml", ".disabled.yaml")
        ):
            text = location.read(name)
            if text is not None:
                texts[name] = text
    included = _included(texts)
    return [name for name in texts if name not in included]


def _included(texts: Mapping[str, str]) -> set[str]:
    """Which of these top-level files are reached through `include:` from the others."""
    includes = {name: include_paths(text) for name, text in texts.items()}
    found: set[str] = set()
    pending = list(texts)
    while pending:
        for entry in includes.get(pending.pop(), []):
            target = posixpath.normpath(entry)
            if target in texts and target not in found:
                found.add(target)
                pending.append(target)
    return found


def include_paths(text: str) -> list[str]:
    """Paths in a file's `include:` list: short syntax (a string) or long syntax (`path:` string
    or list)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    include = data.get("include") if isinstance(data, dict) else None
    paths: list[str] = []
    for entry in include if isinstance(include, list) else []:
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict):
            value = entry.get("path")
            paths += (
                [value]
                if isinstance(value, str)
                else [path for path in value or [] if isinstance(path, str)]
            )
    return paths


def manifest(root: Path) -> dict[str, str]:
    """Every file under root: relative POSIX path -> sha256."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def compose_variables(location: Location, file: str) -> list[str]:
    """Variables a compose file needs a value for.

    A variable written with a non-empty default (``${X:-foo}``) is left out: compose supplies the
    value. An empty default (``${X:-}``) is indistinguishable from none in compose's output, so it
    is still treated as needed.
    """
    output = location.compose("-f", file, "config", "--variables", "--format", "json")
    data = json.loads(output) if output.strip() else {}
    return sorted(name for name, info in data.items() if not info.get("DefaultValue"))


def _build_bundle(lab_config: Path, node_dir: Path, bundle: Path) -> list[str]:
    """Copy the lab config (except nodes/) and the node folder (except node.env) into the bundle,
    dotfiles skipped. Returns the node folder's top-level names."""
    bundle.mkdir(parents=True)
    bundle.chmod(0o700)
    sources = {
        entry.name: entry
        for entry in sorted(lab_config.iterdir())
        if entry.name != "nodes" and not entry.name.startswith(".")
    }
    node_names = []
    for entry in sorted(node_dir.iterdir()):
        if entry.name == NODE_ENV or entry.name.startswith("."):
            continue
        if entry.name in sources:
            raise ComposeError(
                f"{node_dir / entry.name} and {lab_config / entry.name} would both be "
                f"DEPLOY_DIR/{entry.name} on the node; rename or move one of them"
            )
        sources[entry.name] = entry
        node_names.append(entry.name)
    for name, entry in sources.items():
        if entry.is_dir():
            shutil.copytree(entry, bundle / name)
        else:
            shutil.copy2(entry, bundle / name)
    return node_names
