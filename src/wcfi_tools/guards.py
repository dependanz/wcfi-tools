"""Preconditions shared by CLI commands."""

from __future__ import annotations

import typer
from rich.console import Console

from . import config as cfg

_console = Console()


def require_configured() -> None:
    """Stop unless `wcfi setup` has been completed. Setup is required before any other command."""
    if not cfg.is_configured():
        _console.print(
            "[red]wcfi is not set up yet.[/] Run [bold]wcfi setup[/] first (one-time), "
            "then re-run this command."
        )
        raise typer.Exit(1)
