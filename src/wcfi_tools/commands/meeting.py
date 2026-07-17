"""`wcfi meeting …` commands."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm
from tqdm import tqdm

from .. import config as cfg
from ..core import ffmpeg
from ..core.ffmpeg import ToolNotFound
from ..guards import require_configured
from ..meeting import summarize_meeting
from ..meeting.pipeline import derive_meeting_date, discover_audio
from ..providers import ProviderError, build_diarizer, build_summarizer, build_transcriber
from ..web import run_annotator

app = typer.Typer(help="Summarize board meetings from audio.", no_args_is_help=True)
console = Console()


class _ProgressBars:
    """Turn pipeline ``on_progress(stage, current, total)`` callbacks into tqdm bars."""

    def __init__(self) -> None:
        self.bar: tqdm | None = None
        self.stage: str | None = None

    def __call__(self, stage: str, current: int, total: int) -> None:
        if stage != self.stage:
            self.close()
            self.bar = tqdm(total=total, desc=stage.capitalize(), unit="chunk", leave=True)
            self.stage = stage
        if self.bar is not None:
            self.bar.update(current - self.bar.n)

    def close(self) -> None:
        if self.bar is not None:
            self.bar.close()
            self.bar = None


@app.command("summarize")
def summarize(
    folder: Path = typer.Argument(..., help="Folder containing the meeting audio."),
    provider: str | None = typer.Option(None, "--provider", help="Summarizer: openai|anthropic."),
    summary_model: str | None = typer.Option(None, "--summary-model"),
    transcribe_model: str | None = typer.Option(None, "--transcribe-model"),
    chunk_minutes: float = typer.Option(5.0, "--chunk-minutes"),
    emit: str = typer.Option("all", "--emit", help="Comma list: md,paste,all."),
    force: bool = typer.Option(False, "--force", help="Rebuild cached chunks/transcripts/summaries."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Estimate chunks; no API calls."),
    prepared_by: str = typer.Option("Danzel Serrano", "--prepared-by"),
    location: str = typer.Option("44 Van Dyke Rd", "--location"),
    meeting_time: str = typer.Option("Not explicitly captured", "--meeting-time"),
    meeting_date: str | None = typer.Option(None, "--meeting-date"),
    reasoning_effort: str = typer.Option("low", "--reasoning-effort"),
    identify_speakers: Optional[bool] = typer.Option(
        None, "--identify-speakers/--no-identify-speakers",
        help="Attribute speakers by name (prompts if unset).",
    ),
    diarizer_backend: Optional[str] = typer.Option(
        None, "--diarizer", help="Diarization backend: pyannote|window (window = dev stub)."
    ),
    annotator_port: int = typer.Option(8765, "--annotator-port"),
) -> None:
    """Turn a folder of meeting audio into copy-able minutes artifacts."""
    require_configured()  # `wcfi setup` must be run first
    folder = folder.resolve()
    if not folder.is_dir():
        console.print(f"[red]Folder does not exist:[/] {folder}")
        raise typer.Exit(1)

    if dry_run:
        _dry_run(folder, chunk_minutes)
        raise typer.Exit(0)

    config = cfg.load_config()
    try:
        summarizer = build_summarizer(config, provider=provider, model=summary_model)
        transcriber = build_transcriber(config, model=transcribe_model)
    except ProviderError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    diarizer_obj, identify_cb = _maybe_attribution(config, identify_speakers, diarizer_backend, annotator_port)

    emit_tuple = tuple(e.strip() for e in emit.split(",") if e.strip())
    bars = _ProgressBars()
    try:
        result = summarize_meeting(
            folder, summarizer=summarizer, transcriber=transcriber,
            prepared_by=prepared_by, location=location, meeting_date=meeting_date,
            meeting_time=meeting_time, chunk_minutes=chunk_minutes,
            reasoning_effort=reasoning_effort, emit=emit_tuple, force=force,
            diarizer=diarizer_obj, identify=identify_cb,
            on_progress=bars,
        )
    except (ToolNotFound, FileNotFoundError, NotADirectoryError) as exc:
        bars.close()
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    finally:
        bars.close()

    console.print(f"\n[green]Done[/] - {result.meeting_date}")
    for name, path in result.artifacts.items():
        console.print(f"  [cyan]{name}[/]  {path}")
    if result.roster:
        named = ", ".join(f"{k} = {v}" for k, v in result.roster.items() if v)
        console.print(f"  [dim]speakers: {named or 'none labeled'}[/]")
    console.print(f"  [dim]cache: {result.work_dir}[/]")


def _maybe_attribution(config, identify_speakers, backend, port):
    """Decide whether to attribute speakers; return (diarizer, identify_callback) or (None, None)."""
    do_it = identify_speakers
    if do_it is None:
        if sys.stdin.isatty():
            console.print(
                "\n[bold]Speaker attribution[/] (optional): I detect distinct voices, then open a local\n"
                "web page where you play short clips and say who's speaking (or mark 'Unsure').\n"
                "It makes attendance and motion attributions accurate."
            )
            do_it = Confirm.ask("Set it up for this meeting?", default=False)
        else:
            do_it = False
    if not do_it:
        return None, None
    try:
        diarizer = build_diarizer(config, backend=backend)
    except ProviderError as exc:
        console.print(f"[yellow]Skipping speaker attribution:[/] {exc}")
        return None, None

    def identify(snippets, durations):
        return run_annotator(snippets, durations, port=port)

    return diarizer, identify


def _dry_run(folder: Path, chunk_minutes: float) -> None:
    try:
        ffmpeg.require_tool("ffprobe")
    except ToolNotFound as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    audio = discover_audio(folder)
    if not audio:
        console.print("[yellow]No audio files found.[/]")
        raise typer.Exit(1)
    console.print(f"[bold]Meeting:[/] {folder}")
    console.print(f"[bold]Date:[/] {derive_meeting_date(folder)}")
    total_dur = 0.0
    total_chunks = 0
    chunk_seconds = chunk_minutes * 60.0
    for a in audio:
        dur = ffmpeg.ffprobe_duration(a)
        n = math.ceil(dur / chunk_seconds)
        total_dur += dur
        total_chunks += n
        console.print(f"  - {a.name}  ({int(dur // 60)}m{int(dur % 60):02d}s, {n} chunk(s))")
    console.print(f"[bold]Total:[/] {int(total_dur // 60)}m ; ~{total_chunks} chunks at {chunk_minutes:g} min")
