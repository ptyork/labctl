"""Hard links between containers that compose cannot see across projects.

`depends_on` and `network_mode: service:X` stay inside one project, and one project runs on one
node, so compose itself refuses a split there. `network_mode: container:NAME` and `volumes_from:
container:NAME` name any container on the host: if the lab config runs NAME on another node, the
service cannot start. That is an error (plan blocks, validate fails): never a partial migrate.
A NAME the lab config does not run (a container started by hand) is not judged. Pure functions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from labctl.bundle import Desired


def container_names(desired: Desired) -> dict[str, str]:
    """The container name of each of the node's services: its `container_name`, else compose's
    `<project>-<service>-1`. Name -> service."""
    names = {}
    for service, info in sorted(desired.state.services.items()):
        config = desired.service_config(service)
        names[str(config.get("container_name") or f"{info.stack}-{service}-1")] = service
    return names


def links(config: Mapping[str, object]) -> list[tuple[str, str]]:
    """(container, how) for each container a resolved service names: its network namespace
    (`network_mode: container:X`) and `volumes_from: container:X[:ro]`."""
    found = []
    mode = str(config.get("network_mode") or "")
    if mode.startswith("container:"):
        found.append((mode.removeprefix("container:"), "network_mode"))
    sources = config.get("volumes_from") or []
    for entry in sources if isinstance(sources, list) else []:
        text = str(entry)
        if text.startswith("container:"):
            found.append((text.removeprefix("container:").split(":", 1)[0], "volumes_from"))
    return found


def split_links(desired: Mapping[str, Desired], nodes: Iterable[str] | None = None) -> list[str]:
    """
    The services whose linked container the lab config runs on another node.

    Args:
        desired (Mapping[str, Desired]):
            Every node's bundle (a link to a node not in it is not judged).
        nodes (Iterable[str] | None):
            The nodes whose services are checked (default: all of them).

    Returns:
        list[str]:
            One line each: "frodo: transmission uses container gluetun (network_mode), which
            the lab config runs on bilbo; both must be on one node".
    """
    where = {name: node for node, bundle in desired.items() for name in container_names(bundle)}
    problems = []
    for node in sorted(desired if nodes is None else set(nodes) & set(desired)):
        bundle = desired[node]
        for service in sorted(bundle.state.services):
            for name, how in links(bundle.service_config(service)):
                other = where.get(name)
                if other is not None and other != node:
                    problems.append(
                        f"{node}: {service} uses container {name} ({how}), which the lab config "
                        f"runs on {other}; both must be on one node"
                    )
    return problems
