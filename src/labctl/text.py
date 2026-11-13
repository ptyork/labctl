"""How labctl words things that several modules show."""

from __future__ import annotations


def plural(count: int, noun: str, many: str | None = None) -> str:
    """A count with its noun: ``1 stack``, ``3 stacks`` (``many`` when adding an s is wrong)."""
    return f"{count} {noun if count == 1 else many or noun + 's'}"


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
