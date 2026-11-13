"""Offline October scalar controller prerequisite; no host or audio connection."""
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Replay the fixed offline October gain-controller prerequisite.",
                  no_args_is_help=True)


@app.command("run")
def run_command(output: Annotated[Path, typer.Option(help="New local artifact directory.")]):
    """Run frozen scalar fixtures once. Never connects to a DAW or reads audio."""
    from ..audio.gain_replay import run
    from .role_analysis import execute

    result = execute(lambda: run(output))
    if result["status"] != "passed":
        raise typer.Exit(2)
