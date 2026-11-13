"""Read the host ports in use on the nodes and set them against those the lab config wants
(labctl ports). Read-only; nothing here prints. What a port use is, and what clashes, is in
ports.py."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from labctl.bundle import resolve_desired
from labctl.compose import ComposeError
from labctl.envfile import EnvError, preview_env
from labctl.layout import HOME_POINTER
from labctl.ports import Declared, PortUse, declared_ports, port_map
from labctl.remote import MARK, Remote, RemoteError, node_remote, sections
from labctl.repo import ConfigRepo
from labctl.runtime import READ_RUNTIME, parse_containers, parse_listeners
from labctl.settings import Secrets, Settings, SettingsError, node_names
from labctl.state import ALL, Scope

_READ = f"""\
echo "{MARK} pointer"; cat "$HOME/{HOME_POINTER}" 2>/dev/null
{READ_RUNTIME}
"""


@dataclass
class Survey:
    """
    The ports of the nodes surveyed.

    Attributes:
        nodes (list[str]):
            The nodes surveyed.
        uses (list[PortUse]):
            Every port in use or wanted on the nodes that could be read.
        warnings (list[str]):
            What is not shown, and why (a lab config that does not resolve for a node, a node
            without ``ss``).
        unread (list[str]):
            Why a node could not be read, one line per node.
    """

    nodes: list[str] = field(default_factory=list)
    uses: list[PortUse] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)


def survey_ports(
    settings: Settings, repo: ConfigRepo, secrets: Secrets, named: Sequence[str]
) -> Survey:
    """
    Read the nodes named (none: all), in parallel, and resolve the lab config's working tree for
    each.

    Raises:
        RepoError:
            The working tree could not be copied.
        ValueError:
            A node named is not in the lab config.
    """
    survey = Survey()
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        work = Path(tmp)
        source = work / ".tree"
        repo.export_worktree(source)
        known = node_names(source / "nodes")
        scope = Scope.build(set(known), named or (ALL,))
        survey.nodes = [node for node in known if scope.in_scope(node)]
        try:
            with ThreadPoolExecutor(max_workers=max(len(survey.nodes), 1)) as pool:
                reads = {
                    node: pool.submit(node_remote(node).run_script, _READ) for node in survey.nodes
                }
                for node in survey.nodes:
                    _node_ports(survey, settings, node, secrets, work, source, reads[node])
        finally:
            Remote.close_all(survey.nodes)
    return survey


def _node_ports(
    survey: Survey,
    settings: Settings,
    node: str,
    secrets: Secrets,
    work: Path,
    source: Path,
    read: Future[str],
) -> None:
    """Add one node's port uses. A lab config that does not resolve for the node is a warning;
    what runs there is still shown."""
    declared: Declared | None
    deploy_dir = ""
    try:
        desired = resolve_desired(settings, node, secrets, work, None, source)
        declared, deploy_dir = declared_ports(desired), desired.dirs.deploy_dir
    except (ComposeError, EnvError, SettingsError) as exc:
        first = str(exc).splitlines()[0]
        survey.warnings.append(f"{first}: the lab config's ports are not shown for {node}")
        declared = None
    try:
        parts = sections(read.result())
    except RemoteError as exc:
        survey.unread.append(str(exc))
        return
    deploy_dir = (
        "".join(parts.get("pointer", [])).strip()
        or deploy_dir
        or preview_env(source / "nodes", node).get("DEPLOY_DIR", "")
    )
    listeners = parse_listeners(parts.get("listeners", []))
    if listeners is None:
        survey.warnings.append(f"{node}: no ss there: ports held outside Docker are not shown")
    containers = parse_containers(parts.get("containers", []), deploy_dir or "/nonexistent")
    survey.uses += port_map(node, declared, containers, listeners or ())
