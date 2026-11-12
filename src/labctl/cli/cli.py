from importlib.metadata import PackageNotFoundError, version

import click

from labctl.click import AliasedGroup

from .commands import (
    apply_cmd,
    archive_cmd,
    init_cmd,
    node_cmd,
    plan_cmd,
    secrets_cmd,
    settings_cmd,
    status_cmd,
)

MODULE_NAME = "labctl"
try:
    VERSION = version(MODULE_NAME)
except PackageNotFoundError:
    VERSION = "undetermined"


@click.version_option(VERSION)
@click.group(cls=AliasedGroup)
def main() -> None:
    """labctl CLI for homelab Docker management."""


main.add_command(status_cmd)
main.add_command(init_cmd)
main.add_command(settings_cmd)
main.add_command(secrets_cmd)
main.add_command(plan_cmd)
main.add_command(apply_cmd)
main.add_command(node_cmd)
main.add_command(archive_cmd)


if __name__ == "__main__":
    main()
