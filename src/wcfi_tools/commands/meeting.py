"""`wcfi meeting …` commands."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import typer
from rich.console import Console
from tqdm import tqdm

from .. import config as cfg
from ..core import ffmpeg
from ..core.ffmpeg import ToolNotFound
from ..guards import require_configured
from ..meeting import summarize_meeting
from ..meeting.pipeline import derive_meeting_date, discover_audio
from ..meeting.speaker_id import ClusterPrompt, ClusterResolver, auto_resolver, resolve_meeting_speakers
from ..providers import ProviderError, build_diarizer, build_summarizer, build_transcriber
from ..providers.pyannote_provider import DiarizerUnavailable
from ..speakers.voiceprints import VoiceprintDB

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
    identify_speakers: bool | None = typer.Option(
        None, "--identify-speakers/--no-identify-speakers",
        help="Diarize and attribute the minutes by speaker name (default: from `wcfi setup`).",
    ),
    device: str = typer.Option("auto", "--device", help="Diarization device: auto|cpu|cuda."),
    play: bool = typer.Option(True, "--play/--no-play", help="Play snippets during speaker naming."),
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Never ask; auto-match known voices only."),
) -> None:
    """Turn a folder of meeting audio into copy-able minutes artifacts.

    With speaker identification on (enabled in `wcfi setup`, or `--identify-speakers`), it first
    diarizes and walks you through naming voices, then produces minutes attributed by name.
    """
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

    want_speakers = (
        identify_speakers if identify_speakers is not None
        else bool(config.get("speakers", {}).get("enabled", False))
    )
    diarizer = None
    resolver: ClusterResolver | None = None
    if want_speakers:
        try:
            diarizer = build_diarizer(config, device=None if device == "auto" else device)
        except ProviderError as exc:
            console.print(f"[red]{exc}[/] (or run with --no-identify-speakers)")
            raise typer.Exit(1)
        interactive = (not no_prompt) and sys.stdout.isatty()
        if not interactive:
            console.print("[dim]Speaker identification: non-interactive — auto-matching known voices only.[/]")
        resolver = _make_resolver(interactive, folder / "_work" / "speaker_snippets", play)

    speakers_cfg = config.get("speakers", {})
    emit_tuple = tuple(e.strip() for e in emit.split(",") if e.strip())
    bars = _ProgressBars()
    try:
        result = summarize_meeting(
            folder, summarizer=summarizer, transcriber=transcriber,
            prepared_by=prepared_by, location=location, meeting_date=meeting_date,
            meeting_time=meeting_time, chunk_minutes=chunk_minutes,
            reasoning_effort=reasoning_effort, emit=emit_tuple, force=force,
            on_progress=bars,
            identify_speakers=diarizer is not None,
            diarizer=diarizer, speaker_resolver=resolver,
            speaker_threshold=float(speakers_cfg.get("match_threshold", 0.65)),
            speaker_max_samples=int(speakers_cfg.get("max_samples_per_speaker", 8)),
        )
    except DiarizerUnavailable as exc:
        bars.close()
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    except (ToolNotFound, FileNotFoundError, NotADirectoryError) as exc:
        bars.close()
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    finally:
        bars.close()

    console.print(f"\n[green]Done[/] - {result.meeting_date}")
    for name, path in result.artifacts.items():
        console.print(f"  [cyan]{name}[/]  {path}")
    console.print(f"  [dim]cache: {result.work_dir}[/]")


@app.command("identify")
def identify(
    folder: Path = typer.Argument(..., help="Folder containing the meeting audio."),
    per_speaker: int = typer.Option(3, "--per-speaker", help="Snippets to preview per speaker."),
    play: bool = typer.Option(True, "--play/--no-play", help="Play snippets with ffplay if available."),
    threshold: float | None = typer.Option(None, "--threshold", help="Cosine auto-match threshold."),
    device: str = typer.Option("auto", "--device", help="Inference device: auto|cpu|cuda."),
    force: bool = typer.Option(False, "--force", help="Re-run diarization even if a cache exists."),
) -> None:
    """Pre-label a meeting's speakers without summarizing (optional).

    `wcfi meeting summarize` runs this same step for you when speaker identification is on — use this
    command only when you want to name voices ahead of time. Returning speakers are matched
    automatically from your enrolled voiceprints; you label the new or uncertain ones. Results are
    reused by `summarize`. Writes ``speakers.json`` and ``_work/diarization/``.
    """
    require_configured()
    folder = folder.resolve()
    if not folder.is_dir():
        console.print(f"[red]Folder does not exist:[/] {folder}")
        raise typer.Exit(1)

    config = cfg.load_config()
    speakers_cfg = config.get("speakers", {})
    match_threshold = threshold if threshold is not None else float(speakers_cfg.get("match_threshold", 0.65))
    max_samples = int(speakers_cfg.get("max_samples_per_speaker", 8))

    try:
        ffmpeg.require_tool("ffmpeg")
        ffmpeg.require_tool("ffprobe")
    except ToolNotFound as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    try:
        diarizer = build_diarizer(config, device=None if device == "auto" else device)
    except ProviderError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    audio_files = discover_audio(folder)
    if not audio_files:
        console.print("[yellow]No audio files found.[/]")
        raise typer.Exit(1)

    work_dir = folder / "_work"
    resolver = _make_resolver(True, work_dir / "speaker_snippets", play)
    try:
        _, names_by_file = resolve_meeting_speakers(
            audio_files, diarizer=diarizer, db=VoiceprintDB.load(), work_dir=work_dir,
            resolver=resolver, threshold=match_threshold, max_samples=max_samples,
            force=force, snippets_per_speaker=per_speaker, log=lambda m: console.print(m),
        )
    except DiarizerUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    summary = {Path(path).name: mapping for path, mapping in names_by_file.items()}
    _write_json(folder / "speakers.json", {"files": summary})
    console.print(f"\n[green]Done[/] — wrote {folder / 'speakers.json'}")
    console.print("[dim]Next: wcfi meeting summarize <folder> — the minutes will use these names.[/]")


def _make_resolver(interactive: bool, snippet_dir: Path, play: bool) -> ClusterResolver:
    """Build a cluster->name resolver. Non-interactive uses voiceprint auto-matches only."""
    if not interactive:
        return auto_resolver

    def resolver(ctx: ClusterPrompt) -> str | None:
        proposal = ctx.proposal
        console.print(f"\n[bold cyan]{ctx.cluster}[/] in {ctx.audio_name}")
        if proposal and proposal.name and proposal.confident:
            if typer.confirm(f"  Auto-matched [green]{proposal.name}[/] "
                             f"(similarity {proposal.score:.2f}). Correct?", default=True):
                return proposal.name
        _preview(ctx.cluster, ctx.turns, ctx.source_path, snippet_dir, play)
        hint = f" [dim](best guess: {proposal.name}, {proposal.score:.2f})[/]" if (
            proposal and proposal.name
        ) else ""
        console.print(f"  Who is speaking?{hint}  (Enter a name, or leave blank to skip)")
        return typer.prompt("  Name", default="", show_default=False).strip() or None

    return resolver


def _preview(cluster: str, turns: list, audio: Path, snippet_dir: Path, play: bool) -> None:
    if not turns:
        console.print("  [yellow](no representative snippet available)[/]")
        return
    snippet_dir.mkdir(parents=True, exist_ok=True)
    for idx, turn in enumerate(turns, 1):
        dest = snippet_dir / f"{audio.stem}_{cluster}_{idx}.wav"
        try:
            ffmpeg.extract_chunk(audio, dest, turn.start, turn.duration)
        except Exception as exc:  # noqa: BLE001 - snippet is a convenience, never fatal
            console.print(f"  [yellow]Could not cut snippet:[/] {exc}")
            continue
        stamp = f"{_clock(turn.start)}–{_clock(turn.end)}"
        console.print(f"  snippet {idx}: {stamp}  {dest}")
        if play and ffmpeg.has_tool("ffplay"):
            ffmpeg.play(dest)


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
