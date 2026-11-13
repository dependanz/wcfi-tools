"""Soundcheck measurements, optional cloud listening, and saved-set calibration."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(help="Calibrate raw rehearsal inputs and create an Ableton Live 11 set copy.",
                  no_args_is_help=True)
console = Console()


class LocalEngine(str, Enum):
    audiobox = "audiobox"
    music_flamingo = "music-flamingo"


@app.command("prepare-session-comparisons")
def prepare_session_comparisons_command(
    live_set: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New trial directory beside the set.")],
    lead_track_id: Annotated[int, typer.Option("--lead-track-id", min=0)],
    start_bar: Annotated[int, typer.Option("--start-bar", min=1)],
    bars: Annotated[int, typer.Option("--bars", min=1, max=1024)],
    step_db: Annotated[float, typer.Option("--step-db", min=.5, max=3)] = 3,
) -> None:
    """Prepare original/louder/quieter lead set copies for complete-chain Master renders."""
    from ..soundcheck.session_comparisons import prepare_session_comparisons

    try:
        prepare_session_comparisons(live_set, output, lead_track_id=lead_track_id,
                                    start_bar=start_bar, bars=bars, step_db=step_db)
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print("Prepared three experimental set copies. Render each Master in Ableton.")
    console.print("No model has selected a mix; the running session is untouched.")
    console.print(str(output.resolve() / "trial.json"), markup=False)


@app.command("prepare-comparisons")
def prepare_comparisons_command(
    profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New blind-comparison directory.")],
    ffmpeg: Annotated[Path, typer.Option("--ffmpeg", exists=True, dir_okay=False)],
) -> None:
    """Make level-matched vocal-balance, EQ and HF-event trials from declared aligned stems."""
    from ..soundcheck.source_validation import prepare_comparisons

    try:
        report = prepare_comparisons(profile, output, ffmpeg=ffmpeg)
    except (ImportError, OSError, ValueError, RuntimeError, KeyError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print(f"Prepared {report['clips']} local clips at {report['target_lufs']:.1f} LUFS.")
    console.print("Human validation is pending. No model, playback, upload or mix changes.")
    console.print(f"Blind listening page: {report['report']}", markup=False)


@app.command("listen-local")
def listen_local_command(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New listening-review directory.")],
    checkpoint: Annotated[Path, typer.Option("--checkpoint", exists=True, file_okay=False)],
    ffmpeg: Annotated[Path, typer.Option("--ffmpeg", exists=True, dir_okay=False)],
    start: Annotated[float, typer.Option("--start", min=0, max=86400)] = 0,
    seconds: Annotated[float, typer.Option("--seconds", min=10, max=60)] = 30,
    context: Annotated[str, typer.Option("--context")] = "",
    engine: Annotated[LocalEngine, typer.Option("--engine")] = LocalEngine.audiobox,
    allow_gpu: Annotated[bool, typer.Option("--allow-gpu", help="Explicit research GPU opt-in.")] = False,
) -> None:
    """Analyze a WAV locally; learned quality ratings by default, no playback or upload."""
    from ..soundcheck.local_listening import MusicFlamingoListener, listen_local

    try:
        if output.exists():
            raise ValueError("Choose a new output directory; existing results are never overwritten")
        if engine == LocalEngine.audiobox:
            from ..soundcheck.quality_listening import AudioboxListener

            listener = AudioboxListener(checkpoint)
        else:
            if not allow_gpu:
                raise ValueError("GPU research listener requires explicit --allow-gpu. "
                                 "Use --engine audiobox while the restart is investigated.")
            console.print("Loading local Music Flamingo. No playback or audio upload.")
            listener = MusicFlamingoListener(checkpoint, allow_gpu=allow_gpu)
        report = listen_local(audio, output, listener=listener, start=start, seconds=seconds,
                              context=context, ffmpeg=ffmpeg)
    except ImportError as exc:
        console.print("Use the separate .venv-listen environment; see docs/soundcheck.md.",
                      style="red")
        raise typer.Exit(1) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print(f"Status: {report['status']}", markup=False)
    console.print("Experimental local observations (unvalidated):", style="yellow")
    console.print(report["observations"]["text"], markup=False)
    console.print(f"Report: {(output / 'report.html').resolve()}", markup=False)


@app.command("listen")
def listen_command(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New listening-review directory.")],
    start: Annotated[float, typer.Option("--start", min=0, max=86400)] = 0,
    seconds: Annotated[float, typer.Option("--seconds", min=10, max=60)] = 30,
    context: Annotated[str, typer.Option("--context", help="Optional context sent with audio.")] = "",
    model: Annotated[str, typer.Option("--model")] = "gpt-audio-1.5",
    offline: Annotated[bool, typer.Option("--offline", help="Prepare locally; no API/key/upload.")] = False,
    ffmpeg: Annotated[Path | None, typer.Option("--ffmpeg", exists=True, dir_okay=False)] = None,
) -> None:
    """Send a 10–60 s excerpt to OpenAI for listening, alongside separate local measurements."""
    from ..config import get_secret, load_config
    from ..soundcheck.listening import listen

    try:
        key = None
        if not offline:
            load_config()
            key = get_secret("openai")
            if key:
                console.print(f"Sending only the {seconds:g} s excerpt starting at {start:g} s "
                              "and context to OpenAI. API usage is billable.", markup=False)
        report = listen(audio, output, start=start, seconds=seconds, context=context,
                        model=model, api_key=key, offline=offline, ffmpeg=ffmpeg)
    except ImportError as exc:
        console.print('Install audio support: pip install -e ".[soundcheck]"', style="red")
        raise typer.Exit(1) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print(f"Status: {report['status']}", markup=False)
    if report["observations"]:
        console.print("Audio-model observations (unvalidated):", style="yellow")
        console.print(report["observations"]["text"], markup=False)
    else:
        console.print("Prepared locally. No audio was uploaded and no model has listened.")
    console.print(f"Report: {(output / 'report.html').resolve()}", markup=False)


@app.command("inspect-mix")
def inspect_mix_command(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New mix-inspection directory.")],
    ffmpeg: Annotated[Path | None, typer.Option("--ffmpeg", exists=True, dir_okay=False)] = None,
) -> None:
    """Measure loudness and true peak of a rendered mix; never use it for input calibration."""
    from ..soundcheck.mix import inspect_mix

    try:
        report = inspect_mix(audio, output, ffmpeg=ffmpeg)
    except ImportError as exc:
        console.print('Install audio support: pip install -e ".[soundcheck]"', style="red")
        raise typer.Exit(1) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    metrics = report["metrics"]
    console.print(f"Integrated: {metrics['integrated_lufs']} LUFS; "
                  f"range: {metrics['loudness_range_lu']} LU; "
                  f"sample peak: {metrics['sample_peak_dbfs']} dBFS; "
                  f"true peak: {metrics['true_peak_dbtp']} dBTP", markup=False)
    for warning in report["warnings"]:
        console.print(warning, style="yellow", markup=False)
    console.print(f"Report: {(output / 'report.html').resolve()}", markup=False)


@app.command("init")
def init_profile(
    live_set: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    profile: Annotated[Path, typer.Option("--profile", help="New profile JSON path.")],
) -> None:
    """Read saved track names/routing and create an editable channel profile."""
    from ..soundcheck.ableton import inspect_set
    from ..soundcheck.profiles import write_json

    try:
        data = inspect_set(live_set)
        write_json(profile, data)
    except (OSError, ValueError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print(f"Created {profile}: {len(data['tracks'])} external audio inputs", markup=False)
    console.print("Review guessed roles and enabled tracks. Record raw inputs during full-band rehearsal.")


@app.command("calibrate")
def calibrate_command(
    profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New results directory.")],
    recordings: Annotated[Path | None, typer.Option("--recordings", exists=True, file_okay=False)] = None,
    recorded_on: Annotated[str | None, typer.Option("--recorded-on", help="YYYY-MM-DD; defaults to today.")] = None,
    latest: Annotated[bool, typer.Option("--latest", help="Explicitly choose latest matching take per track.")] = False,
    seconds: Annotated[float, typer.Option("--seconds", min=10, max=300)] = 90,
    start: Annotated[float, typer.Option("--start", min=0, max=86400)] = 0,
    write_set: Annotated[Path | None, typer.Option("--write-set", help="Also create a calibrated .als copy.")] = None,
) -> None:
    """Analyze each raw input, report problems, and calculate fixed gain/EQ settings."""
    from ..soundcheck.ableton import apply_plan
    from ..soundcheck.pipeline import calibrate

    try:
        # Fail before writing an unusable report when optional dependencies are absent.
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import soundfile  # noqa: F401

        day = date.fromisoformat(recorded_on) if recorded_on else datetime.now(UTC).astimezone().date()
        plan = calibrate(profile, output, recordings=recordings, recorded_on=day,
                         latest=latest, seconds=seconds, start=start)
        for track in plan["tracks"]:
            change = "" if track["gain_db"] is None else f" {track['gain_db']:+.2f} dB"
            console.print(f"{track['status']:8} {track['name']}{change}", markup=False)
            for reason in track["reasons"]:
                console.print(f"         {reason}", markup=False)
        console.print(f"Report: {(output / 'report.html').resolve()}", markup=False)
        if write_set:
            changed = apply_plan(plan, write_set)
            console.print(f"Created {write_set}; calibrated {len(changed)} channels", markup=False)
        if not any(t["status"] == "ready" for t in plan["tracks"]):
            raise typer.Exit(2)
    except ImportError as exc:
        console.print('Install audio support: pip install -e ".[soundcheck]"', style="red")
        raise typer.Exit(1) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc


@app.command("apply")
def apply_command(
    plan: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", help="New .als file beside the source.")],
) -> None:
    """Apply eligible settings to a new copy; leave the source and running Live unchanged."""
    from ..soundcheck.ableton import apply_plan
    from ..soundcheck.pipeline import read_plan

    try:
        changed = apply_plan(read_plan(plan), output)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc
    console.print(f"Created {output}; calibrated: {', '.join(changed)}", markup=False)
