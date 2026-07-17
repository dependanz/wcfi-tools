"""wcfi CLI entry point."""

from __future__ import annotations

import typer

from . import __version__
from .commands import meeting as meeting_cmd
from .commands.setup import setup as setup_command

app = typer.Typer(
    help="wcfi-tools - a toolkit for Word Christian Fellowship International.",
    no_args_is_help=True,
    add_completion=False,
)

app.command("setup")(setup_command)
app.add_typer(meeting_cmd.app, name="meeting")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wcfi-tools {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Run `wcfi setup` first, then `wcfi meeting summarize <folder>`."""


if __name__ == "__main__":
    app()
