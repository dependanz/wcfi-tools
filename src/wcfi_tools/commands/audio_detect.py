"""Passive local microphone and recording diagnostics."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Listen for tonal audio problems locally.", no_args_is_help=True)


def _error(exc):
    typer.echo(str(exc), err=True)
    raise typer.Exit(1) from exc


def _json(row):
    typer.echo(json.dumps(row, allow_nan=False))


@app.command("devices")
def devices_command(
    json_output: Annotated[bool, typer.Option("--json", help="Print device metadata as JSON.")] = False,
):
    """List local input devices without opening a microphone."""
    from ..audio.detection_input import list_input_devices

    try:
        devices = list_input_devices()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        _error(exc)
    if json_output:
        _json(devices)
        return
    if not devices:
        typer.echo("No input devices found.")
    for item in devices:
        marker = " (default)" if item.get("default") else ""
        typer.echo(f"{item['index']:>3}  {item['name']} [{item['hostapi']}]"
                   f" | {item['channels']} ch | {item['sample_rate']:g} Hz{marker}")


def _display(row):
    kind = row["type"]
    if kind == "session":
        source = row["input"]
        typer.echo(f"Analyzing {source['name']} | {source['sample_rate']:g} Hz"
                   f" | channel {source['channel']}")
        typer.echo("Flags sustained tones and feedback-like growth; musical tones can also match.")
        if source["kind"] == "microphone":
            typer.echo("Listening. Ctrl+C stops. Input levels are digital dBFS, not sound-pressure levels.")
    elif kind == "detection":
        label = "FEEDBACK-LIKE" if row["classification"] == "feedback_like" else "TONAL CANDIDATE"
        typer.echo(f"{row['end_seconds']:7.2f}s  {label:<16} {row['frequency_hz']:8.1f} Hz"
                   f" | {row['duration_seconds']:.2f}s | +{row['prominence_db']:.1f} dB prominence"
                   f" | {row['event']}")
    elif kind == "level":
        typer.echo(f"{row['time_seconds']:7.2f}s  input {row['rms_dbfs']:.1f} dBFS")
    elif kind == "gap":
        typer.echo(row["message"], err=True)
    elif kind == "summary":
        typer.echo(f"Finished {row['duration_seconds']:.2f}s: {row['event_count']} tonal event(s),"
                   f" {row['feedback_like_count']} with feedback-like growth.")
        if row["capture_gaps"] or row["clipped_samples"]:
            typer.echo(f"Input quality: {row['capture_gaps']} gap(s),"
                       f" {row['clipped_samples']} near-full-scale sample(s).")
        if not row["event_count"]:
            typer.echo("No sustained candidates met these settings.")


@app.command("howler")
def howler_command(
    audio: Annotated[Path | None, typer.Argument(
        exists=True, dir_okay=False, help="Recording to analyze; omit to listen to a microphone.")]
    = None,
    device: Annotated[str | None, typer.Option(help="Input index or name from 'detect devices'.")]
    = None,
    seconds: Annotated[float | None, typer.Option(min=.01, help="Stop after this much input audio.")]
    = None,
    channel: Annotated[int, typer.Option(min=1, help="One-based input/file channel; never downmixed.")]
    = 1,
    sample_rate: Annotated[int | None, typer.Option(min=8000, max=192000,
        help="Microphone rate; default is the device's rate.")] = None,
    min_frequency: Annotated[float, typer.Option(min=20, help="Lowest candidate frequency, Hz.")]
    = 100.,
    max_frequency: Annotated[float, typer.Option(min=20, help="Highest candidate frequency, Hz.")]
    = 10000.,
    min_duration: Annotated[float, typer.Option(min=.1, max=10,
        help="Persistence required before reporting a tone, seconds.")] = .5,
    threshold_db: Annotated[float, typer.Option(min=3, max=60,
        help="Minimum spectral prominence; lower is more sensitive.")] = 15.,
    json_output: Annotated[bool, typer.Option("--json", help="Stream JSON Lines instead of text.")]
    = False,
    output: Annotated[Path | None, typer.Option(help="Write a new JSON report (measurements only).")]
    = None,
):
    """Detect persistent narrow tones in a recording or live microphone input."""
    from ..audio.detection_input import open_audio_file, open_microphone
    from ..audio.howler_monitor import monitor

    try:
        values = [min_frequency, max_frequency, min_duration, threshold_db]
        if seconds is not None:
            values.append(seconds)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Detection options must be finite numbers")
        if min_frequency >= max_frequency:
            raise ValueError("--min-frequency must be below --max-frequency")
        if audio is not None and (device is not None or sample_rate is not None):
            raise ValueError("--device and --sample-rate apply only to microphone input")
        if output is not None:
            if output.exists():
                raise ValueError(f"Report already exists: {output}")
            if not output.parent.is_dir():
                raise ValueError(f"Report directory does not exist: {output.parent}")
        context = (open_audio_file(audio, channel=channel) if audio is not None
                   else open_microphone(device=device, channel=channel, sample_rate=sample_rate))
        with context as source:
            report = monitor(source, seconds=seconds, min_frequency=min_frequency,
                             max_frequency=max_frequency, min_duration=min_duration,
                             threshold_db=threshold_db, emit=_json if json_output else _display)
        if output is not None:
            with output.open("x", encoding="utf-8") as destination:
                json.dump(report, destination, indent=2, allow_nan=False)
                destination.write("\n")
            typer.echo(f"Report: {output.resolve()}", err=json_output)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        _error(exc)
