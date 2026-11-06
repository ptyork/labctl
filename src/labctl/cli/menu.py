"""Interactive building blocks (get_rich controls). Esc cancels everywhere and returns None."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from get_rich import Chooser, FileChooser, Input
from rich.markup import escape

from labctl.console import console

Validator = Callable[[str], str | None]


def rows(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Two aligned columns as chooser options: name in bold, value dimmed when unset."""
    width = max((len(name) for name, _ in pairs), default=0)
    return [f"[bold]{escape(name.ljust(width))}[/]  {value}" for name, value in pairs]


def choose(title: str, header: str, options: Sequence[str], selected: int = 0) -> int | None:
    """Index of the chosen option (options are Rich markup)."""
    control = Chooser(
        choices=list(options),
        selected_index=selected,
        console=console,
        title_text=title,
        header_text=header,
    )
    _, index = control.run()
    return None if index is None else int(index)


def edit_text(
    title: str,
    prompt: str,
    value: str = "",
    *,
    header: str = "Enter to accept, Esc to cancel.",
    validators: Sequence[Validator] = (),
) -> str | None:
    control = Input(
        prompt=prompt,
        default_value=value,
        validators=list(validators),
        console=console,
        title_text=title,
        header_text=header,
    )
    answer = control.run()
    return None if answer is None else str(answer)


def choose_dir(title: str, initial: str) -> Path | None:
    start = Path(initial).expanduser() if initial else Path.home()
    while not start.is_dir() and start != start.parent:
        start = start.parent
    control = FileChooser(
        initial_path=start,
        choose_dirs=True,
        exclude_hidden=True,
        console=console,
        title_text=title,
        header_text="Enter opens a directory; choose SELECT to use it. Esc to cancel.",
    )
    chosen = control.run()
    return None if chosen is None else Path(chosen)
