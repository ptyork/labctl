"""State model shared by the planner and state collection.

Every node has a desired state (from the repo) and a recorded state (what labctl last applied).
Both are reduced to the same shape: the node's compose services, keyed by service name, and the
stacks (compose projects) they belong to. Service names are unique per node across its stacks.
Which file defines a service does not matter; compose's resolved output is the source of truth.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

# The target meaning every node (plan, apply, labctl node): no node may be called this.
ALL = "all"


@dataclass(frozen=True)
class Dirs:
    """A node's two configured roots: where its compose files live and where app data lives."""

    deploy_dir: str
    appdata_dir: str


@dataclass(frozen=True)
class Stack:
    """A compose project on a node: its name and the files compose runs together (-f, in order).

    Files are relative to DEPLOY_DIR. Files that share a project name always run together: run
    one at a time, `up --remove-orphans` would remove the others' containers.
    """

    name: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Volume:
    """A named volume holding node data, by its real Docker name (compose prefixes a stack's own
    volumes with the project: key ``pgdata`` in ``homelab-frodo`` is ``homelab-frodo_pgdata``)."""

    name: str
    key: str  # the compose key in its stack
    stack: str
    external: bool = False  # `external: true`: compose never creates it


@dataclass(frozen=True)
class Network:
    """A network a stack declares, by its real Docker name (``reverse_proxy``, or compose's
    ``<project>_default``). ``config``: its resolved definition as canonical JSON; compose
    recreates the network when that changes."""

    name: str
    key: str  # the compose key in its stack
    stack: str
    config: str = ""
    services: tuple[str, ...] = ()  # the stack's services attached to it


@dataclass(frozen=True)
class Service:
    """One compose service as deployed (or to be deployed) on one node.

    data_paths: bind sources under the node's APPDATA_DIR, relative to it.
    volumes: named volumes the service mounts that hold node data (real Docker names).
    fingerprint: hash of the service's resolved definition and its stack. A change on the same
        node means the service must be redeployed.
    mounts: (container path, data key) for each data path and volume: how renamed data is
        recognised (same container path, other directory or volume).
    stack: the compose project the service belongs to.
    anonymous: container paths of anonymous volumes (no stable name: never moved).
    remote: (volume name, where its data is) for volumes on remote storage (NFS, CIFS, another
        driver): not node data, never copied; each node mounts the same storage.
    config: the resolved definition as canonical JSON, for showing what an update changes. Not
        part of equality (the fingerprint is).

    A service with neither data paths nor volumes is stateless: nothing of it ever migrates.
    """

    name: str
    data_paths: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    fingerprint: str = ""
    mounts: tuple[tuple[str, str], ...] = ()
    stack: str = ""
    anonymous: tuple[str, ...] = ()
    remote: tuple[tuple[str, str], ...] = ()
    config: str = field(default="", compare=False, repr=False)

    @property
    def stateful(self) -> bool:
        return bool(self.data_paths or self.volumes)

    @property
    def data_keys(self) -> tuple[str, ...]:
        return (*self.data_paths, *(volume_key(volume) for volume in self.volumes))

    def with_keys(self, moved: Callable[[str], str]) -> Service:
        """This service with every data key passed through ``moved`` (a key stays a path or a
        volume)."""
        keys = [moved(data_key) for data_key in self.data_keys]
        return replace(
            self,
            data_paths=tuple(dict.fromkeys(key for key in keys if not is_volume(key))),
            volumes=tuple(dict.fromkeys(volume_name(key) for key in keys if is_volume(key))),
            mounts=tuple((target, moved(k)) for target, k in self.mounts),
        )


@dataclass(frozen=True)
class NodeState:
    node: str
    services: Mapping[str, Service] = field(default_factory=dict)
    stacks: Mapping[str, Stack] = field(default_factory=dict)
    volumes: Mapping[str, Volume] = field(default_factory=dict)  # by real name
    networks: Mapping[str, Network] = field(default_factory=dict, compare=False)  # by real name

    @classmethod
    def of(
        cls,
        node: str,
        *services: Service,
        stacks: Iterable[Stack] = (),
        volumes: Iterable[Volume] = (),
        networks: Iterable[Network] = (),
    ) -> NodeState:
        return cls(
            node=node,
            services={service.name: service for service in services},
            stacks={stack.name: stack for stack in stacks},
            volumes={volume.name: volume for volume in volumes},
            networks={network.name: network for network in networks},
        )

    @property
    def multi_stack(self) -> bool:
        return len(self.stacks) > 1


@dataclass(frozen=True)
class Scope:
    """Which nodes a run may contact and change.

    Skipped nodes (not named, or unreachable) are still planned against (as of their latest apply
    tag), but any operation that touches one is held.
    """

    skipped: frozenset[str] = frozenset()

    @classmethod
    def build(cls, nodes: set[str], targets: Iterable[str]) -> Scope:
        """The nodes named on the command line, or 'all' of them."""
        named = set(targets)
        if not named:
            raise ValueError(f"Name the nodes, or 'all'. Nodes: {', '.join(sorted(nodes))}")
        if ALL in nodes:
            raise ValueError(f"a node may not be called '{ALL}'")
        if ALL in named:
            if len(named) > 1:
                raise ValueError(f"'{ALL}' already means every node")
            return cls()
        unknown = named - nodes
        if unknown:
            raise ValueError(
                f"Unknown node(s): {', '.join(sorted(unknown))}. Nodes: {', '.join(sorted(nodes))}"
            )
        return cls(skipped=frozenset(nodes - named))

    def without(self, nodes: Iterable[str]) -> Scope:
        """This scope with ``nodes`` skipped too."""
        return Scope(self.skipped | frozenset(nodes))

    def in_scope(self, node: str) -> bool:
        return node not in self.skipped


#############################################################################


# Data keys


#############################################################################
# A data key is a path relative to APPDATA_DIR ("plexms/config") or a named volume by its real
# name ("volume:homelab-frodo_pgdata"). The path helpers also work on absolute paths.

VOLUME = "volume:"


def is_volume(key: str) -> bool:
    return key.startswith(VOLUME)


def volume_key(name: str) -> str:
    return VOLUME + name


def volume_name(key: str) -> str:
    return key.removeprefix(VOLUME)


def under(path: str, parent: str) -> bool:
    """``path`` is strictly inside ``parent``."""
    return path != parent and PurePosixPath(path).is_relative_to(parent)


def overlaps(a: str, b: str) -> bool:
    """Same data, or one inside the other. Volumes only overlap themselves."""
    if is_volume(a) or is_volume(b):
        return a == b
    return a == b or under(a, b) or under(b, a)
