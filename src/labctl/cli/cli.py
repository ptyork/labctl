import click

from labctl import __version__
from labctl.click import AliasedGroup

from .commands import (
    add_cmd,
    apply_cmd,
    archive_cmd,
    discard_cmd,
    history_cmd,
    init_cmd,
    node_cmd,
    plan_cmd,
    ports_cmd,
    remove_cmd,
    secrets_cmd,
    settings_cmd,
    status_cmd,
    unlock_cmd,
    validate_cmd,
)


@click.version_option(__version__)
@click.group(cls=AliasedGroup)
def main() -> None:
    """labctl CLI for homelab Docker management."""


main.add_command(status_cmd)
main.add_command(init_cmd)
main.add_command(settings_cmd)
main.add_command(secrets_cmd)
main.add_command(plan_cmd)
main.add_command(validate_cmd)
main.add_command(apply_cmd)
main.add_command(discard_cmd)
main.add_command(add_cmd)
main.add_command(remove_cmd)
main.add_command(node_cmd)
main.add_command(archive_cmd)
main.add_command(ports_cmd)
main.add_command(history_cmd)
main.add_command(unlock_cmd)


if __name__ == "__main__":
    main()
