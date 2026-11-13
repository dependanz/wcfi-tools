"""Launch the local Windows monitor viewer without meeting/API setup."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

app = typer.Typer(
    help="View and control connected displays from one window (Windows prototype).",
    no_args_is_help=True,
)


def _require_windows() -> None:
    if sys.platform != "win32":
        typer.echo("Monitor control currently requires Windows.", err=True)
        raise typer.Exit(1)


def _error(action: str, exc: Exception) -> None:
    message = f"Unable to {action}: {exc}"
    if isinstance(exc, ModuleNotFoundError) and exc.name in {"tkinter", "_tkinter"}:
        message += ". Install Python with Tcl/Tk support, then try again."
    typer.echo(message, err=True)
    raise typer.Exit(1) from exc


@app.command("launch")
def launch_command(
    practice: Annotated[
        bool,
        typer.Option(help="Open a practice window for trying control with one display."),
    ] = False,
    fps: Annotated[
        int,
        typer.Option(min=1, max=60, help="Requested preview frames per second."),
    ] = 60,
    monitor: Annotated[
        int | None,
        typer.Option(min=1, help="Initial display number from 'wcfi monitor-control list'."),
    ] = None,
) -> None:
    """Open the controller with connected displays and application windows."""
    _require_windows()
    if practice and monitor is not None:
        raise typer.BadParameter("Choose either --practice or --monitor, not both.")
    try:
        from ..monitor_control.app import launch

        launch(practice=practice, fps=fps, monitor=monitor)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        _error("launch monitor control", exc)


@app.command("list")
def list_command() -> None:
    """List connected displays, their sizes, and Windows desktop coordinates."""
    _require_windows()
    try:
        from ..monitor_control.capture import enumerate_monitors

        monitors = enumerate_monitors()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        _error("list displays", exc)
    if not monitors:
        typer.echo("No connected displays found.")
        return
    for index, display in enumerate(monitors, start=1):
        primary = " [primary]" if display.primary and "(primary)" not in display.label else ""
        typer.echo(
            f"{index}: {display.label}{primary}\n"
            f"   {display.width} x {display.height} at ({display.left}, {display.top})"
            f" | {display.device}"
        )
