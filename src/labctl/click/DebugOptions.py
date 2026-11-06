from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

import click


class DebugOptions:
    """
    -d/--debug and -q/--quiet: the logging level for the command.

    labctl reports through the console; logging is for detail: every command run on a node
    (debug), and what went wrong behind an error.

    Args:
        base_level (int):
            The level without either option (default: logging.WARNING).
        quiet_level (int):
            The level with --quiet (default: logging.ERROR).
    """

    def __init__(self, base_level: int = logging.WARNING, quiet_level: int = logging.ERROR):
        self.base_level = base_level
        self.quiet_level = quiet_level

    def options(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @click.option("-d", "--debug", is_flag=True, help="Log every command run on a node.")
        @click.option("-q", "--quiet", is_flag=True, help="Log errors only.")
        @functools.wraps(func)
        def command_wrapper(*args: Any, **kwargs: Any) -> Any:
            logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
            if kwargs.get("debug"):
                logging.getLogger().setLevel(logging.DEBUG)
            elif kwargs.get("quiet"):
                logging.getLogger().setLevel(self.quiet_level)
            else:
                logging.getLogger().setLevel(self.base_level)
            return func(*args, **kwargs)

        return command_wrapper
