import click


class AliasedGroup(click.Group):
    def get_command(
        self,
        ctx: click.Context,
        cmd_name: str,
    ) -> click.Command | None:
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv

        matches = [x for x in self.list_commands(ctx) if x.startswith(cmd_name)]
        if not matches:
            return None
        if len(matches) == 1:
            return click.Group.get_command(self, ctx, matches[0])

        ctx.fail(f"Too many matches: {', '.join(sorted(matches))}")

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Always return the full command name.
        _, cmd, args = super().resolve_command(ctx, args)
        if cmd is None:
            return None, None, args
        return cmd.name, cmd, args
