"""Reproducible Master evaluation challenges, without opening a sound device."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Prepare and score a local CPU-only Master evaluator benchmark.",
                  no_args_is_help=True)


def _execute(function):
    try:
        from .role_analysis import limit_resources

        limit_resources()
        value = function()
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


@app.command("prepare")
def prepare_command(
    profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="New benchmark directory.")],
    ffmpeg: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    seed: int = 20261022,
):
    """Create blinded, loudness-matched controls from saved Master excerpts."""
    def execute():
        from ..soundcheck.master_benchmark import prepare

        return prepare(profile, output, ffmpeg=ffmpeg, seed=seed)
    _execute(execute)


@app.command("run")
def run_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option(help="New immutable inference run directory.")],
    checkpoint: Annotated[Path, typer.Option(exists=True, file_okay=False)],
):
    """Run Audiobox on one logical CPU; no GPU, networking, playback or retries."""
    def execute():
        from ..soundcheck.master_benchmark_cpu import run

        result = run(directory, output, checkpoint, progress=lambda s: typer.echo(s, err=True))
        return {k: v for k, v in result.items() if k != "predictions"}
    _execute(execute)


@app.command("report")
def report_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    run_directory: Annotated[Path, typer.Option(exists=True, file_okay=False)],
):
    """Unblind a completed run, write HTML/JSON and report coarse-control results."""
    def execute():
        from ..soundcheck.master_benchmark import summarize

        result = summarize(directory, run_directory)
        return {k: v for k, v in result.items()
                if k not in ("rows", "checks", "runtime", "session_sources")} | {
                    "report_html": str(run_directory.resolve()/"report.html"),
                    "report_json": str(run_directory.resolve()/"results.json")}
    _execute(execute)
