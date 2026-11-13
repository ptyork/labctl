"""Every docker command line labctl runs, and the output formats it asks docker for.

Command lines only: nothing here runs anything. A function returns an argv (run with
``remote.execute`` or ``Node.run``); a constant in capitals is a shell snippet for a script that
runs on the node. The parsers of what these print stay with the types they build (runtime.py,
stack.py, checks.py).

Docker only, and no interface yet: this is where the runtime seam for Podman starts (TODO.md).
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from labctl.state import Stack

# The group whose members may run docker.
GROUP = "docker"
# What holds a published port for Docker itself (as `ss` names the process).
PROCESSES = frozenset({"docker-proxy", "rootlesskit", "rootlessport"})

# The labels compose puts on what it creates.
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
VOLUME_LABEL = "com.docker.compose.volume"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"


#############################################################################

# Compose

#############################################################################


def compose(*args: str, cli: str = "docker") -> list[str]:
    """`docker compose ARGS`; ``cli`` is the docker CLI for a local run ($LABCTL_DOCKER)."""
    return [cli, "compose", *args]


def stack_args(stack: Stack) -> list[str]:
    """`-p NAME -f FILE...` for a stack; nothing for compose's own default stack."""
    project = ["-p", stack.name] if stack.name else []
    return [*project, *(arg for file in stack.files for arg in ("-f", file))]


def compose_command(deploy_dir: str, stack: Stack, *args: str, progress: str = "") -> str:
    """Shell command running docker compose on one stack (for output streamed to the terminal);
    ``progress`` is compose's --progress (plain: a line per event, for output read by labctl)."""
    missing = shlex.quote(f"DEPLOY_DIR {deploy_dir} does not exist")
    flags = ["--progress", progress] if progress else []
    return f"cd {shlex.quote(deploy_dir)} 2>/dev/null || {{ echo {missing} >&2; exit 3; }}; " + (
        shlex.join(compose(*flags, *stack_args(stack), *args))
    )


# Shell: compose's version ("" when there is no compose), and its projects as JSON.
COMPOSE_VERSION = "docker compose version --short"
COMPOSE_LS = "docker compose ls --all --format json"


#############################################################################

# Containers

#############################################################################

# `docker inspect --format`: one JSON object per container, only what labctl uses (no env: it can
# hold secrets). Read by runtime.parse_containers.
CONTAINER_FORMAT = (
    '{"name":{{json .Name}},"state":{{json .State.Status}},"image":{{json .Config.Image}},'
    f'"project":{{{{json (index .Config.Labels "{PROJECT_LABEL}")}}}},'
    f'"service":{{{{json (index .Config.Labels "{SERVICE_LABEL}")}}}},'
    f'"files":{{{{json (index .Config.Labels "{CONFIG_FILES_LABEL}")}}}},'
    f'"dir":{{{{json (index .Config.Labels "{WORKING_DIR_LABEL}")}}}},'
    '"ports":{{json .HostConfig.PortBindings}},"restart":{{json .HostConfig.RestartPolicy}},'
    '"exit":{{json .State.ExitCode}},"restarts":{{json .RestartCount}},'
    '"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}}}'
)
# Shell: a CONTAINER_FORMAT line for every container on the node, running or not.
INSPECT_CONTAINERS = (
    "docker ps -aq </dev/null 2>/dev/null |\n"
    f"  xargs -r docker inspect --format '{CONTAINER_FORMAT}' 2>/dev/null"
)

_BIND_SOURCES = '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}'
# Shell: the host path of every bind mount of every container, one per line.
BIND_SOURCES = (
    f"docker ps -aq </dev/null | xargs -r docker inspect --format '{_BIND_SOURCES}' 2>/dev/null"
)
# Shell: the compose files each container was started from (comma-separated per container).
PROJECT_FILES = (
    f"docker ps -a --format '{{{{.Label \"{CONFIG_FILES_LABEL}\"}}}}' </dev/null 2>/dev/null"
)

# `docker ps -a --format`: one JSON object per container, with its compose labels. Read by
# stack.snapshot.
SNAPSHOT_FORMAT = (
    f'{{"id":{{{{json .ID}}}},"project":{{{{json (.Label "{PROJECT_LABEL}")}}}},'
    f'"service":{{{{json (.Label "{SERVICE_LABEL}")}}}},"state":{{{{json .State}}}}}}'
)


def snapshot(project: str = "") -> list[str]:
    """Every container as a SNAPSHOT_FORMAT line (one compose project's, when named)."""
    only = ["--filter", f"label={PROJECT_LABEL}={project}"] if project else []
    return ["docker", "ps", "--all", "--no-trunc", "--format", SNAPSHOT_FORMAT, *only]


# sh -c script, the container names as arguments: the node's clock, then per container its name,
# RestartCount and when it last started. Read by stack.parse_restarts.
RESTARTS = (
    "date -u +%s; [ $# -gt 0 ] && docker inspect --format "
    "'{{.Name}} {{.RestartCount}} {{.State.StartedAt}}' \"$@\" 2>/dev/null; true"
)


def set_restart(container: str, policy: str) -> list[str]:
    return ["docker", "update", f"--restart={policy}", container]


def stop(container: str) -> list[str]:
    return ["docker", "stop", container]


def start(container: str) -> list[str]:
    return ["docker", "start", container]


def rename(container: str, new: str) -> list[str]:
    return ["docker", "rename", container, new]


# Shell: remove dangling images, node-wide.
PRUNE_IMAGES = "docker image prune --force"


#############################################################################

# Volumes

#############################################################################

# Shell: the volumes that exist, then "--", then every volume's size as JSON. Read-only: no
# container is run (mounting a missing volume would create it). Read by checks.parse_volumes.
VOLUMES = 'docker volume ls -q && echo "--" && docker system df -v --format json'
# Shell: Docker's root dir, where volumes live.
ROOT_DIR = "docker info --format '{{.DockerRootDir}}'"


def volume_inspect(name: str) -> list[str]:
    """Fails when there is no such volume."""
    return ["docker", "volume", "inspect", name]


def volume_labels(name: str) -> list[str]:
    """The volume's labels, as JSON (``null`` when it has none)."""
    return ["docker", "volume", "inspect", "--format", "{{json .Labels}}", name]


def volume_create(name: str, labels: Mapping[str, str]) -> list[str]:
    flags = [arg for key, value in sorted(labels.items()) for arg in ("--label", f"{key}={value}")]
    return ["docker", "volume", "create", *flags, name]


def volume_remove(name: str) -> list[str]:
    return ["docker", "volume", "rm", name]


def volume_users(name: str) -> list[str]:
    """The names of the containers (any state) that mount the volume."""
    return ["docker", "ps", "--all", "--filter", f"volume={name}", "--format", "{{.Names}}"]


def compose_volume_labels(project: str, key: str) -> dict[str, str]:
    """The labels compose looks for on a volume it is to use as its own."""
    return {PROJECT_LABEL: project, VOLUME_LABEL: key}


#############################################################################

# The helper container

#############################################################################

# Runs file work on a node as root (GNU tar and coreutils). Pinned by digest (multi-arch index).
HELPER = (
    "debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3"
)

# Ownership and attributes kept on both ends of a copy.
_TAR = ["--numeric-owner", "--xattrs", "--acls", "--sparse"]


def helper(spec: str, script: str, *args: str, read_only: bool = False) -> list[str]:
    """
    A script in the helper container (as root).

    Args:
        spec (str):
            What is mounted at /x: a host directory, or a volume's name.
        script (str):
            The sh script.
        *args (str):
            Its positional arguments.
        read_only (bool):
            Mount it read-only (default: False).

    Returns:
        list[str]:
            The command.
    """
    mount = f"{spec}:/x{':ro' if read_only else ''}"
    return ["docker", "run", "--rm", "-v", mount, HELPER, "sh", "-c", script, "sh", *args]


def copy(source: str, target: str) -> list[str]:
    """Copy the contents of ``source`` into ``target`` (each a host directory or a volume's
    name) on one node, as root, ownership kept."""
    tar = " ".join(_TAR)
    return [
        "docker", "run", "--rm", "-v", f"{source}:/s:ro", "-v", f"{target}:/d", HELPER,
        "sh", "-c", f"tar -C /s {tar} -cpf - . | tar -C /d {tar} -xpf -",
    ]  # fmt: skip


def tar_out(spec: str, *paths: str) -> list[str]:
    """
    Write a tar of data to stdout, in the helper (the sending end of a relay).

    Args:
        spec (str):
            What is read, mounted read-only: a host directory, or a volume's name.
        *paths (str):
            The paths under it to take (default: everything in it).

    Returns:
        list[str]:
            The command.
    """
    names = ["--", *paths] if paths else ["."]
    return [
        "docker", "run", "--rm", "-v", f"{spec}:/src:ro", HELPER,
        "tar", "-C", "/src", *_TAR, "-cpf", "-", *names,
    ]  # fmt: skip


def tar_in(spec: str, folder: str = "") -> list[str]:
    """
    Extract a tar from stdin, in the helper (the receiving end of a relay).

    Args:
        spec (str):
            Where it goes: a host directory, or a volume's name.
        folder (str):
            A folder under it to extract into, made if needed (default: ``spec`` itself).

    Returns:
        list[str]:
            The command.
    """
    run = ["docker", "run", "--rm", "-i", "-v", f"{spec}:/dst", HELPER]
    if not folder:
        return [*run, "tar", "-C", "/dst", *_TAR, "-xpf", "-"]
    extract = 'mkdir -p "/dst/$1" && tar -C "/dst/$1" ' + " ".join(_TAR) + " -xpf -"
    return [*run, "sh", "-c", extract, "sh", folder]
