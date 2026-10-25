"""Role-aware local analysis and proposals; no commands here write to a DAW."""
from __future__ import annotations

import ctypes as ct
import json
import os
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Role-aware RED/KIT observations, replay and processing proposals.", no_args_is_help=True)


def limit_resources():
    """Limit this CLI process only; never touch Live, drivers or system power settings."""
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    if os.name == "nt":
        kernel = ct.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ct.c_void_p
        kernel.SetPriorityClass.argtypes = (ct.c_void_p, ct.c_ulong)
        kernel.GetProcessAffinityMask.argtypes = (ct.c_void_p, ct.POINTER(ct.c_size_t), ct.POINTER(ct.c_size_t))
        kernel.SetProcessAffinityMask.argtypes = (ct.c_void_p, ct.c_size_t)
        handle = kernel.GetCurrentProcess()
        mask, system = ct.c_size_t(), ct.c_size_t()
        if (not kernel.SetPriorityClass(handle, 0x40)
                or not kernel.GetProcessAffinityMask(handle, ct.byref(mask), ct.byref(system))
                or not kernel.SetProcessAffinityMask(handle, mask.value & -mask.value)):
            raise RuntimeError("Cannot apply this CLI worker's one-CPU idle-priority budget")


def execute(function):
    try:
        limit_resources()
        result = function()
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, allow_nan=False))
    return result


@app.command("profiles")
def profiles_command():
    """Show authoritative source intent, enabled analyses and policy limits."""
    from ..audio.roles import PROFILES

    typer.echo(json.dumps({name: profile.describe() for name, profile in PROFILES.items()}, indent=2))


@app.command("file")
def file_command(
    before: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="New report directory.")],
    profile: Annotated[str, typer.Option(help="red or kit")],
    after: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    start: Annotated[float, typer.Option(min=0, max=86400)] = 0,
    seconds: Annotated[float, typer.Option(min=.5, max=30)] = 20,
    aligned: Annotated[bool, typer.Option(help="Declare supplied pre/post files already time-aligned.")] = False,
    chain_state: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    previews: Annotated[bool, typer.Option(help="Render local DSP auditions; never play them.")] = False,
):
    """Analyse saved pre/post captures or a raw bypass reference; no sound device access."""
    def run():
        from ..audio.role_replay import analyze_files

        state = None
        if chain_state:
            if chain_state.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Chain snapshot is too large")
            state = json.loads(chain_state.read_text(encoding="utf-8-sig"))
        return analyze_files(before, output, profile, after=after, start=start,
                             seconds=seconds, aligned=aligned, chain_state=state, previews=previews)
    execute(run)


@app.command("take")
def take_command(
    take: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option(help="New report directory.")],
    start: Annotated[float, typer.Option(min=0, max=86400)] = 29,
    seconds: Annotated[float, typer.Option(min=.5, max=30)] = 20,
    previews: Annotated[bool, typer.Option(help="Render local DSP auditions; never play them.")] = False,
):
    """Replay synchronized RED/KIT originals through the shared analysis engine."""
    def run():
        from ..audio.role_replay import analyze_take

        return analyze_take(take, output, start=start, seconds=seconds, previews=previews)
    execute(run)


@app.command("validate-take")
def validation_command(
    take: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option(help="New controlled-test directory.")],
    start: Annotated[float, typer.Option(min=0, max=86400)] = 29,
    seconds: Annotated[float, typer.Option(min=8, max=20)] = 10,
):
    """Test known faults and nulls on real stems. Musical listening remains separate."""
    def run():
        from ..audio.role_validation import validate_take

        return validate_take(take, output, start=start, seconds=seconds)
    result = execute(run)
    if result["passed"] != result["total"]:
        raise typer.Exit(2)


@app.command("watch")
def watch_command(
    output: Annotated[Path, typer.Option(help="New observation directory; saves JSON only.")],
    seconds: Annotated[float, typer.Option(min=1, max=3600)] = 30,
    window: Annotated[float, typer.Option(min=.5, max=30)] = 10,
    interval: Annotated[float, typer.Option(min=.1, max=30)] = 5,
):
    """Passively watch existing RED/KIT nodes. No source start, transport, recording or writes."""
    def run():
        from ..ableton.client import connect
        from ..audio.role_watch import watch_nodes

        try:
            client = connect()
        except RuntimeError:
            client = None
        return watch_nodes(output, seconds=seconds, window_seconds=window,
                           interval=interval, client=client)
    execute(run)
