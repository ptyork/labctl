from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import click


@dataclass(frozen=True)
class Options:
    """What plan and apply share: the scope, and the choices the plan is made with."""

    nodes: tuple[str, ...]
    replace_stale: bool
    no_migrate: tuple[str, ...]
    start: bool
    verbose: bool
    fresh: tuple[str, ...] = ()
    restore: tuple[str, ...] = ()  # SERVICE or SERVICE@STAMP
    ff: bool = False


class PlanOptions:
    """
    The arguments and options plan and apply share.

    The command gets them as one ``options`` keyword argument (an ``Options``) instead of one
    keyword argument each.
    """

    def options(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @click.argument("nodes", nargs=-1, metavar="NODE...|all")
        @click.option(
            "--replace-stale",
            is_flag=True,
            help="Allow migrates onto existing data (it is archived first, never deleted).",
        )
        @click.option(
            "--no-migrate",
            "no_migrate",
            multiple=True,
            metavar="SERVICE",
            help="Start this service fresh on its new node instead of moving its data "
            "(repeatable).",
        )
        @click.option(
            "--fresh",
            multiple=True,
            metavar="SERVICE",
            help="Start this added service empty, though archived copies of its data exist "
            "(repeatable; by default the newest is restored).",
        )
        @click.option(
            "--restore",
            multiple=True,
            metavar="SERVICE[@STAMP]",
            help="Restore this added service's archived data without asking: the newest copy, "
            "or the one archived at STAMP (repeatable).",
        )
        @click.option(
            "--start",
            is_flag=True,
            help="Start every stack apply writes, also those down or new (by default they stay "
            "down).",
        )
        @click.option("--verbose", "-v", is_flag=True, help="List every file that would change.")
        @click.option(
            "--ff",
            is_flag=True,
            help="Fast-forward the branch to its upstream first, without asking (a terminal is "
            "asked).",
        )
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            options = Options(
                nodes=kwargs.pop("nodes"),
                replace_stale=kwargs.pop("replace_stale"),
                no_migrate=kwargs.pop("no_migrate"),
                start=kwargs.pop("start"),
                verbose=kwargs.pop("verbose"),
                fresh=kwargs.pop("fresh"),
                restore=kwargs.pop("restore"),
                ff=kwargs.pop("ff"),
            )
            return func(*args, options=options, **kwargs)

        return wrapper
