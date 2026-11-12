"""What plan and apply ask in a terminal: missing node.env values and secrets, and which
archived copy to restore."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.markup import escape

from labctl.bundle import REQUIRED_VARS
from labctl.cli import menu
from labctl.console import console
from labctl.restore import Copy


def ask_restore(service: str, node: str, copies: Sequence[Copy]) -> Copy | None:
    """In a terminal: which archived copy to restore, or none (Esc: the newest)."""
    options = [f"Restore {escape(copy.describe())}" for copy in copies] + ["Start it empty"]
    index = menu.choose(
        f"{escape(service)} is added to {escape(node)}: restore its data?",
        options,
        header="Archived copies of its data exist. Esc restores the newest.",
    )
    if index is None:
        return copies[0]
    return None if index == len(copies) else copies[index]


def prompt_value(node: str, name: str, secret: bool, suggestion: str) -> str | None:
    """Ask for a missing variable. Esc skips it (the plan then reports it as missing)."""
    where = "secrets.toml" if secret else f"nodes/{node}/node.env"
    validators: list[Callable[[str], str | None]] = [_required]
    if _is_path_var(name):
        validators.append(_absolute_path)
    answer = menu.edit_text(
        f"Value for {name} on {node}",
        suggestion,
        header=f"{node}'s compose files use it and nothing sets it. Saved to {where}; Esc skips.",
        validators=validators,
    )
    if answer is None:
        console.print(f"[yellow]![/] {node}: {name} skipped")
        return None
    console.print(f"[green]✓[/] {node}: {name} = {escape(answer)}, saved to {where}")
    return answer


def _is_path_var(name: str) -> bool:
    return name in REQUIRED_VARS or name.endswith(("_DIR", "_HOME", "_PATH"))


def _required(value: str) -> str | None:
    return None if value.strip() else "A value is required (Esc to skip)"


def _absolute_path(value: str) -> str | None:
    return None if value.startswith("/") else "Must be an absolute path on the node"
