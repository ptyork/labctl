"""labctl validate [NODE...|all]: check the lab config without touching a node."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from labctl.bundle import Desired, resolve_desired
from labctl.cli.common import load_settings
from labctl.cli.render import not_a_stack
from labctl.click import DebugOptions
from labctl.compose import ComposeError
from labctl.console import console
from labctl.envfile import EnvError
from labctl.links import split_links
from labctl.ports import lab_clashes
from labctl.repo import ConfigRepo, RepoError
from labctl.settings import Secrets, Settings, SettingsError, node_names
from labctl.state import ALL, Scope


@click.command(name="validate")
@click.argument("nodes", nargs=-1, metavar="[NODE...|all]")
@DebugOptions().options
def validate_cmd(nodes: tuple[str, ...], **kwargs: Any) -> None:
    """Check the lab config without touching a node. Exits 1 when a node is not valid.

    No node named means 'all'. Each node's stacks are built and resolved as plan does (the working
    tree, uncommitted changes included), with every value they need, and no two of a node's
    services may publish the same host port. Never asks, writes nothing, and leaves _HOST_IP_
    unresolved.
    """
    settings = load_settings()
    try:
        scope = Scope.build(set(settings.node_names()), nodes or (ALL,))
        repo = ConfigRepo.open(settings.lab_config)
    except (SettingsError, ValueError, RepoError) as exc:
        raise click.ClickException(str(exc)) from exc
    secrets = Secrets.load(settings.secrets_file)

    invalid = 0
    found: dict[str, Desired] = {}
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        work = Path(tmp)
        source = work / ".tree"
        try:
            repo.export_worktree(source)
        except RepoError as exc:
            raise click.ClickException(str(exc)) from exc
        for node in node_names(source / "nodes"):
            if scope.in_scope(node) and not check_node(
                settings, node, secrets, work, source, found
            ):
                invalid += 1
    # Across the nodes checked: a service linked to a container another node runs.
    for problem in split_links(found):
        console.print(f"[red]✗[/] {escape(problem)}")
        invalid += 1
    if invalid:
        sys.exit(1)


def check_node(
    settings: Settings,
    node: str,
    secrets: Secrets,
    work: Path,
    source: Path | None = None,
    found: dict[str, Desired] | None = None,
) -> bool:
    """
    Build and resolve one node's bundle offline, and say in one line whether it is valid (a yml
    that is not a stack is a warning before it).

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
        found (dict[str, Desired] | None):
            Where a valid node's bundle is kept, for the checks across nodes (default: none).

    Returns:
        bool:
            Whether the node is valid.
    """
    try:
        desired = resolve_desired(settings, node, secrets, work, None, source, offline=True)
    except (ComposeError, EnvError) as exc:
        console.print(f"[red]✗[/] {escape(str(exc))}")
        return False
    for file, why in sorted(desired.not_stacks.items()):
        console.print(f"[yellow]![/] {not_a_stack(node, file, why)}")
    missing = desired.env.placeholders
    if missing:
        console.print(f"[red]✗[/] {node}: no value for {', '.join(missing)}")
        return False
    clashes = lab_clashes(desired)
    for clash in clashes:
        console.print(f"[red]✗[/] {escape(clash)}")
    if clashes:
        return False
    if found is not None:
        found[node] = desired
    stacks, services = len(desired.configs), len(desired.state.services)
    console.print(
        f"[green]✓[/] {node}: {stacks} stack{'' if stacks == 1 else 's'}, "
        f"{services} service{'' if services == 1 else 's'}"
    )
    return True
