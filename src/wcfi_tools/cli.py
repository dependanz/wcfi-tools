"""wcfi CLI entry point."""

from __future__ import annotations

import typer

from . import __version__
from .commands import fbmsg as fbmsg_cmd
from .commands import inbox as inbox_cmd
from .commands import meeting as meeting_cmd
from .commands import wix as wix_cmd
from .commands import setup as setup_cmd
from .commands import create as create_cmd

app = typer.Typer(
    help="wcfi-tools - a toolkit for Word Christian Fellowship International.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)

app.add_typer(setup_cmd.app, name="setup")
app.add_typer(create_cmd.app, name="create")
app.add_typer(meeting_cmd.app, name="meeting")
app.add_typer(wix_cmd.app, name="wix")
app.add_typer(fbmsg_cmd.app, name="fbmsg")
app.add_typer(inbox_cmd.app, name="inbox")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wcfi-tools {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Run `wcfi setup` first, then `wcfi meeting summarize`."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
