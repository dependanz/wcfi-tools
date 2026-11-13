"""Explicit local SongBench human-preference benchmark commands."""
from pathlib import Path
from typing import Annotated

import typer

from .master_benchmark import _execute

app = typer.Typer(help="Test SongBench against external MED/ODAQ human ratings, CPU only.",
                  no_args_is_help=True)


@app.command("prepare")
def prepare_command(
    assets_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option(help="New immutable benchmark directory.")],
):
    """Freeze selected saved excerpts, human answer key and prospective scoring policy."""
    def execute():
        from ..soundcheck.songbench_external import prepare

        return prepare(assets_root, output)
    _execute(execute)


@app.command("run")
def run_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    assets_root: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option(help="New immutable inference run directory.")],
):
    """Infer full excerpts offline, with one CPU core, memory limits and cooldowns."""
    def execute():
        from ..soundcheck.songbench_cpu import run

        return run(directory, output, assets_root, progress=lambda s: typer.echo(s, err=True))
    _execute(execute)


@app.command("report")
def report_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    run_directory: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    amendment: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    output: Annotated[Path | None, typer.Option(help="New amended report directory, if applicable.")] = None,
):
    """Unblind a completed run against external human ratings; never play audio."""
    def execute():
        from ..soundcheck.songbench_external import report

        return report(directory, run_directory, amendment=amendment, output=output)
    _execute(execute)
