"""Apply's progress: one line per node, each step marked once it is done.

In a terminal the lines update in place, and the running step says what it is doing or waiting
for, and for how long:

    bilbo  ✓ down docker  ✓ files  ✓ up homelab-bilbo: 5/5 running
    frodo  ✓ down docker  ✓ files  … up homelab-frodo: 15/16 running, waiting for plex (0:24)

Elsewhere (no terminal, and in tests) each finished step is a line of its own. Notes (anything
else apply has to say) are printed above the lines.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.text import Text


@dataclass
class Step:
    """A step running: what it says while it runs (``detail``) and once done (``result``)."""

    label: str
    detail: str = ""
    result: str = ""
    started: float = field(default_factory=time.monotonic)

    def update(self, detail: str) -> None:
        self.detail = detail


@dataclass
class _Line:
    node: str
    done: list[tuple[bool, str]] = field(default_factory=list)  # (succeeded, label and result)
    current: Step | None = None

    def render(self, width: int) -> Text:
        text = Text()
        text.append(self.node.ljust(width), style="bold")
        for ok, what in self.done:
            text.append("  ")
            text.append("✓" if ok else "✗", style="bold green" if ok else "bold red")
            text.append(f" {what}")
        if self.current is not None:
            step = self.current
            elapsed = int(time.monotonic() - step.started)
            text.append(f"  … {step.label}")
            if step.detail:
                text.append(f": {step.detail}")
            text.append(f" ({elapsed // 60}:{elapsed % 60:02d})")
        return text


class Progress:
    """The lines, live in a terminal (``console`` given and a terminal), printed otherwise."""

    def __init__(self, say: Callable[[str], None], console: Console | None = None) -> None:
        self.say = say
        self.console = console
        self.lines: dict[str, _Line] = {}
        self.live: Live | None = None

    def __enter__(self) -> Progress:
        if self.console is not None and self.console.is_terminal:
            self.live = Live(
                _Lines(self), console=self.console, refresh_per_second=4, transient=False
            )
            self.live.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    def note(self, text: str) -> None:
        """Something apply says besides its steps (printed above the lines)."""
        self.say(text)

    @contextmanager
    def step(self, node: str, label: str) -> Iterator[Step]:
        """A step on ``node``: marked ✓ when the block ends, ✗ when it raises."""
        line = self.lines.setdefault(node, _Line(node))
        step = Step(label)
        line.current = step
        try:
            yield step
        except BaseException:
            line.current = None
            line.done.append((False, label))
            if self.live is None:
                self.say(f"[bold red]✗[/] {node}: {escape(label)}")
            raise
        line.current = None
        what = f"{label}: {step.result}" if step.result else label
        line.done.append((True, what))
        if self.live is None:
            self.say(f"[green]✓[/] {node}: {escape(what)}")


class _Lines:
    """The live part: every node's line, as it is now."""

    def __init__(self, progress: Progress) -> None:
        self.progress = progress

    def __rich__(self) -> RenderableType:
        lines = list(self.progress.lines.values())
        width = max((len(line.node) for line in lines), default=0)
        return Group(*(line.render(width) for line in lines))
