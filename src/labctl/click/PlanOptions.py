from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import click

from labctl.planning import Options
from labctl.state import ALL


class PlanOptions:
    """
    The arguments and options plan and apply share.

    The command gets them as one ``options`` keyword argument (an ``Options``) instead of one
    keyword argument each.

    Args:
        all_by_default (bool):
            No node named means 'all' (plan; apply requires its nodes). Default: False.
    """

    def __init__(self, all_by_default: bool = False):
        self.all_by_default = all_by_default

    def options(self, func: Callable[..., Any]) -> Callable[..., Any]:
        metavar = "[NODE...|all]" if self.all_by_default else "NODE...|all"

        @click.argument("nodes", nargs=-1, metavar=metavar)
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
        @click.option("--verbose", "-v", is_flag=True, help="List every file that would change.")
        @click.option(
            "--ff",
            is_flag=True,
            help="Fast-forward the branch to its upstream first, without asking (a terminal is "
            "asked).",
        )
        @click.option(
            "--stop-unmanaged",
            is_flag=True,
            help="Stop the containers labctl does not manage that are in the way of what apply "
            "starts (and take down projects from elsewhere with a stack's name), without asking.",
        )
        @click.option(
            "--defer-blocked",
            is_flag=True,
            help="Don't start the services whose ports are held by something else, without "
            "asking; start them later with 'labctl node up'.",
        )
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            nodes = kwargs.pop("nodes")
            options = Options(
                nodes=nodes or ((ALL,) if self.all_by_default else ()),
                replace_stale=kwargs.pop("replace_stale"),
                no_migrate=kwargs.pop("no_migrate"),
                verbose=kwargs.pop("verbose"),
                fresh=kwargs.pop("fresh"),
                restore=kwargs.pop("restore"),
                ff=kwargs.pop("ff"),
                stop_unmanaged=kwargs.pop("stop_unmanaged"),
                defer_blocked=kwargs.pop("defer_blocked"),
            )
            return func(*args, options=options, **kwargs)

        return wrapper
