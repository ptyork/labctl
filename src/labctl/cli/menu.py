"""Interactive building blocks (get_rich controls). Esc cancels everywhere and returns None.

Used as get_rich's demos use them: the question is the control's own prompt ("Enter commit
message"), a title only names what a chooser chooses, and a header adds context only when needed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from get_rich import Chooser, Confirm, FileChooser, Input
from rich.markup import escape

from labctl.console import console

Validator = Callable[[str], str | None]

# The controls' key hints, in the terminal's own white rather than get_rich's grey.
_STYLES = {"footer_style": "white", "border_style": "white"}


def rows(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Two aligned columns as chooser options: the name in bold, then its value."""
    width = max((len(name) for name, _ in pairs), default=0)
    return [f"[bold]{escape(name.ljust(width))}[/]  {value}" for name, value in pairs]


def choose(title: str, options: Sequence[str], selected: int = 0, header: str = "") -> int | None:
    """Index of the chosen option (options are Rich markup)."""
    control = Chooser(
        choices=list(options),
        selected_index=selected,
        console=console,
        title_text=title,
        header_text=header or None,
        styles=_STYLES,
    )
    _, index = control.run()
    return None if index is None else int(index)


def confirm(prompt: str, *, default: bool = False, header: str = "") -> bool | None:
    """Yes or No; None when cancelled."""
    control = Confirm(
        prompt=prompt,
        choices=["Yes", "No"],
        default_index=0 if default else 1,
        console=console,
        header_text=header or None,
        styles=_STYLES,
    )
    _, index = control.run()
    return None if index is None else index == 0


def edit_text(
    prompt: str,
    value: str = "",
    *,
    header: str = "",
    validators: Sequence[Validator] = (),
) -> str | None:
    """The answer to ``prompt``, starting from ``value``."""
    control = Input(
        prompt=prompt,
        default_value=value,
        validators=list(validators),
        console=console,
        header_text=header or None,
        styles=_STYLES,
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
        header_text="Enter opens a directory; choose SELECT to use it.",
        styles=_STYLES,
    )
    chosen = control.run()
    return None if chosen is None else Path(chosen)
