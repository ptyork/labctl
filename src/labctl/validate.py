"""Check the lab config without touching a node (labctl validate, and labctl add's last step):
each node's stacks are built and resolved offline, as plan does. Nothing here prints."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from labctl.bundle import Desired, resolve_desired
from labctl.compose import ComposeError
from labctl.envfile import EnvError
from labctl.ports import lab_clashes
from labctl.repo import ConfigRepo
from labctl.settings import Secrets, Settings, node_names
from labctl.state import Scope


@dataclass(frozen=True)
class Checked:
    """
    What checking one node's part of the lab config came to.

    Attributes:
        node (str):
            The node.
        desired (Desired | None):
            Its bundle, when it could be built (it may still have errors).
        errors (tuple[str, ...]):
            Why the node is not valid; empty when it is.
    """

    node: str
    desired: Desired | None = None
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def check_node(
    settings: Settings,
    node: str,
    secrets: Secrets,
    work: Path,
    source: Path | None = None,
) -> Checked:
    """
    Build and resolve one node's bundle offline.

    Args:
        settings (Settings):
            labctl's settings.
        node (str):
            The node.
        secrets (Secrets):
            The values for `_SECRET_` variables.
        work (Path):
            Where the bundle is built.
        source (Path | None):
            The copy of the lab config (default: the lab config itself).

    Returns:
        Checked:
            The bundle, and what is wrong with it: stacks that do not resolve, variables without
            a value, two services publishing the same host port.
    """
    try:
        desired = resolve_desired(settings, node, secrets, work, None, source, offline=True)
    except (ComposeError, EnvError) as exc:
        return Checked(node, errors=(str(exc),))
    missing = desired.env.placeholders
    if missing:
        return Checked(node, desired, (f"{node}: no value for {', '.join(missing)}",))
    return Checked(node, desired, tuple(lab_clashes(desired)))


def check_new_node(settings: Settings, node: str) -> Checked:
    """Check one node of the lab config as it is on disk (a node just added)."""
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        return check_node(settings, node, Secrets.load(settings.secrets_file), Path(tmp))


def check_lab(
    settings: Settings, repo: ConfigRepo, scope: Scope, secrets: Secrets
) -> Iterator[Checked]:
    """
    Check the nodes in scope against a copy of the working tree (uncommitted changes included),
    one by one.

    Raises:
        RepoError:
            The working tree could not be copied.
    """
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        work = Path(tmp)
        source = work / ".tree"
        repo.export_worktree(source)
        for node in node_names(source / "nodes"):
            if scope.in_scope(node):
                yield check_node(settings, node, secrets, work, source)
