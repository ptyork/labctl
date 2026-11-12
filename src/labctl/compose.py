"""Turn `docker compose config` output into planner state.

Pure functions only: callers run compose. Compose does all interpolation, includes, merging and
`extends`; labctl reads only its resolved output, so how the repo's files are organised does not
matter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from labctl.state import Network, NodeState, Service, Stack, Volume, volume_key

# Under APPDATA_DIR, reserved for labctl: copies on their way in, and the archive (data of removed
# services and migrate sources). Everything in the archive is labctl's.
LABCTL_DIR = ".labctl"
ARCHIVE_DIR = f"{LABCTL_DIR}/archive"


class ComposeError(Exception):
    pass


def reduce_stacks(
    node: str,
    resolved: Sequence[tuple[Stack, Mapping[str, Any]]],
    appdata_dir: str,
    running: Collection[str] | None = None,
) -> NodeState:
    """
    Reduce each stack's resolved compose JSON to the planner's view of one node.

    Args:
        node (str):
            The node's name.
        resolved (Sequence[tuple[Stack, Mapping[str, Any]]]):
            Each stack, with its resolved config.
        appdata_dir (str):
            The node's APPDATA_DIR: bind mounts under it are data.
        running (Collection[str] | None):
            On a node, the projects that have containers there: a service two stacks define
            belongs to the one that runs, when only one does (the other is a file left behind)
            (default: None).

    Returns:
        NodeState:
            The node's services, stacks, volumes and networks.

    Raises:
        ComposeError:
            Two stacks define the same service (service names must be unique on a node).
    """
    services: dict[str, Service] = {}
    volumes: list[Volume] = []
    networks: list[Network] = []
    for stack, config in resolved:
        one = reduce_config(node, config, appdata_dir, stack)
        volumes += one.volumes.values()
        networks += one.networks.values()
        for service in one.services.values():
            other = services.get(service.name)
            if other is None:
                services[service.name] = service
                continue
            if running is not None and (other.stack in running) != (stack.name in running):
                if stack.name in running:
                    services[service.name] = service
                continue
            raise ComposeError(
                f"{node}: service '{service.name}' is in two stacks, '{other.stack}' and "
                f"'{stack.name}'; service names must be unique on a node"
            )
    return NodeState.of(
        node,
        *services.values(),
        stacks=[stack for stack, _ in resolved],
        volumes=volumes,
        networks=networks,
    )


def reduce_config(
    node: str, config: Mapping[str, Any], appdata_dir: str, stack: Stack | None = None
) -> NodeState:
    """
    Reduce one stack's resolved compose JSON.

    Named volumes, from the top-level ``volumes:``:
        - local (the usual): node data, by real name;
        - local with ``o: bind`` and a ``device``: a bind mount of the device path;
        - another driver, or nfs/cifs ``driver_opts``: the data lives elsewhere; never copied, but
          listed (``remote``) so the plan can say so;
        - anonymous (no source): no stable name; listed, never moved.

    Args:
        node (str):
            The node's name.
        config (Mapping[str, Any]):
            The stack's resolved config (``docker compose config --format json``).
        appdata_dir (str):
            The node's APPDATA_DIR: bind mounts under it are data.
        stack (Stack | None):
            The stack (default: one named like the config, no files recorded).

    Returns:
        NodeState:
            The stack's services, volumes and networks.
    """
    stack = stack or Stack(str(config.get("name", "")), ())
    resolved: Mapping[str, Any] = config.get("services") or {}
    root = PurePosixPath(appdata_dir)
    declared: Mapping[str, Any] = config.get("volumes") or {}

    services: list[Service] = []
    used: dict[str, Volume] = {}
    for name in sorted(resolved):
        data_paths: list[str] = []
        volumes: list[str] = []
        mounts: list[tuple[str, str]] = []
        anonymous: list[str] = []
        remote: list[tuple[str, str]] = []
        for mount in resolved[name].get("volumes") or []:
            kind, source, target = mount.get("type"), mount.get("source"), mount.get("target")
            if kind == "volume" and not source:
                anonymous.append(str(target or ""))
                continue
            if kind == "volume":
                info = declared.get(source) or {}
                device = _bind_device(info)
                where = _remote(info)
                if device is None and where:
                    remote.append((str(info.get("name") or source), where))
                    continue
                if device is None:
                    volume = Volume(
                        name=str(info.get("name") or source),
                        key=str(source),
                        stack=stack.name,
                        external=bool(info.get("external")),
                    )
                    used[volume.name] = volume
                    if volume.name not in volumes:
                        volumes.append(volume.name)
                    if target:
                        mounts.append((str(target), volume_key(volume.name)))
                    continue
                kind, source = "bind", device
            if kind == "bind":
                relative = _data_path(PurePosixPath(source), root, f"{node}: {name}")
                if relative is None:
                    continue
                if relative not in data_paths:
                    data_paths.append(relative)
                if target:
                    mounts.append((str(target), relative))
        services.append(
            Service(
                name=name,
                data_paths=tuple(data_paths),
                volumes=tuple(volumes),
                # The stack is part of it: a service moved to another stack is recreated there.
                fingerprint=fingerprint({"stack": stack.name, "service": resolved[name]}),
                mounts=tuple(mounts),
                stack=stack.name,
                anonymous=tuple(anonymous),
                remote=tuple(dict.fromkeys(remote)),
                config=canonical(resolved[name]),
            )
        )
    return NodeState.of(
        node,
        *services,
        stacks=[stack],
        volumes=used.values(),
        networks=_networks(config, stack.name),
    )


def _networks(config: Mapping[str, Any], stack: str) -> list[Network]:
    """The stack's top-level networks (compose lists only those a service uses), by real name."""
    services: Mapping[str, Any] = config.get("services") or {}
    found = []
    for key, info in sorted((config.get("networks") or {}).items()):
        info = info or {}
        attached = tuple(
            name for name in sorted(services) if key in (services[name].get("networks") or {})
        )
        found.append(
            Network(
                name=str(info.get("name") or f"{stack}_{key}"),
                key=str(key),
                stack=stack,
                config=canonical({k: v for k, v in info.items() if k != "name"}),
                services=attached,
            )
        )
    return found


def _bind_device(info: Mapping[str, Any]) -> str | None:
    """The host path of a local volume that is a bind mount in disguise (o: bind, device: P)."""
    opts = info.get("driver_opts") or {}
    if (info.get("driver") or "local") != "local" or not opts.get("device"):
        return None
    if "bind" not in str(opts.get("o", "")).split(","):
        return None
    return str(opts["device"])


def _remote(info: Mapping[str, Any]) -> str:
    """Where a volume's data is when it is not on the node (another driver, or a network
    filesystem): "nfs 10.0.0.5:/export", "driver rclone"; "" for node data."""
    driver = str(info.get("driver") or "local")
    if driver != "local":
        return f"driver {driver}"
    opts = info.get("driver_opts") or {}
    kind = str(opts.get("type", "")).lower()
    if not kind.startswith(("nfs", "cifs", "smb")):
        return ""
    device = str(opts.get("device", ""))
    if device.startswith("//"):  # CIFS names the server itself: //nas/share
        return f"{kind} {device}"
    options = {
        k: v for k, _, v in (option.partition("=") for option in str(opts.get("o", "")).split(","))
    }
    return f"{kind} {options.get('addr', '')}{device}"  # NFS: addr=10.0.0.5, device :/export


def relocate(value: Any, local: str, remote: str) -> Any:
    """``value`` with every path string under ``local`` rewritten to be under ``remote``."""
    if isinstance(value, str):
        if value == local or value.startswith(local + "/"):
            return remote + value[len(local) :]
        return value
    if isinstance(value, dict):
        return {key: relocate(item, local, remote) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item, local, remote) for item in value]
    return value


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _data_path(source: PurePosixPath, root: PurePosixPath, label: str) -> str | None:
    """A bind source as a data path relative to APPDATA_DIR, or None when outside it. ``label``
    (NODE: SERVICE) starts the error."""
    if source == root:
        raise ComposeError(
            f"{label}: mounts APPDATA_DIR '{root}' itself; moving it would copy every "
            "service's data"
        )
    if not source.is_relative_to(root):
        return None
    relative = source.relative_to(root)
    if relative.parts[0] == LABCTL_DIR:
        raise ComposeError(f"{label}: mounts {source}, but {root}/{LABCTL_DIR}/ is labctl's")
    return str(relative)
