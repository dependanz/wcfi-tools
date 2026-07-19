"""`wcfi meeting …` commands."""

from __future__ import annotations

import json
import math
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
from ..providers import ProviderError, build_diarizer, build_summarizer, build_transcriber
from ..providers.base import DiarizationResult
from ..providers.pyannote_provider import DiarizerUnavailable
from ..speakers import (
    attribute_turns,
    enroll_confirmed,
    propose_from_voiceprints,
    select_representative_turns,
)
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

    emit_tuple = tuple(e.strip() for e in emit.split(",") if e.strip())
    bars = _ProgressBars()
    try:
        result = summarize_meeting(
            folder, summarizer=summarizer, transcriber=transcriber,
            prepared_by=prepared_by, location=location, meeting_date=meeting_date,
            meeting_time=meeting_time, chunk_minutes=chunk_minutes,
            reasoning_effort=reasoning_effort, emit=emit_tuple, force=force,
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
    """Diarize a meeting and walk you through naming each speaker (local, pyannote).

    Returning speakers are matched automatically from your enrolled voiceprints; you only label the
    ones that are new or uncertain. Confirmed voices are saved for next time. Writes
    ``speakers.json`` plus per-file diarization into ``_work/diarization/``.
    """
    require_configured()
    folder = folder.resolve()
    if not folder.is_dir():
        console.print(f"[red]Folder does not exist:[/] {folder}")
        raise typer.Exit(1)

    config = cfg.load_config()
    match_threshold = threshold if threshold is not None else float(
        config.get("speakers", {}).get("match_threshold", 0.65)
    )
    max_samples = int(config.get("speakers", {}).get("max_samples_per_speaker", 8))

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
    snippet_dir = work_dir / "speaker_snippets"
    diar_dir = work_dir / "diarization"
    diar_dir.mkdir(parents=True, exist_ok=True)

    db = VoiceprintDB.load()
    summary: dict[str, dict[str, str]] = {}

    for audio in audio_files:
        raw_path = diar_dir / f"{audio.stem}.raw.json"
        if raw_path.exists() and not force:
            console.print(f"\n[bold]Diarization cached[/] for {audio.name} [dim](--force to redo)[/]")
            diar = DiarizationResult.from_dict(json.loads(raw_path.read_text(encoding="utf-8")))
        else:
            console.print(f"\n[bold]Diarizing[/] {audio.name} … [dim](one-time; slow on CPU)[/]")
            try:
                diar = diarizer.diarize(audio)
            except DiarizerUnavailable as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(1)
            _write_json(raw_path, diar.to_dict())
        if not diar.turns:
            console.print("  [yellow]No speech detected; skipping.[/]")
            continue

        proposals = propose_from_voiceprints(diar, db, match_threshold)
        snippets = select_representative_turns(diar.turns, per_speaker=per_speaker)
        mapping = _walk_through(audio, diar, proposals, snippets, snippet_dir, play)

        enrolled = enroll_confirmed(db, diar, mapping, max_samples=max_samples)
        db.save()

        attributed = attribute_turns(diar.turns, mapping)
        _write_json(
            diar_dir / f"{audio.stem}.json",
            {
                "source": audio.name,
                "clusters": mapping,
                "turns": [{"start": s, "end": e, "speaker": name} for s, e, name in attributed],
            },
        )
        summary[audio.name] = mapping
        console.print(f"  [green]Saved[/] {enrolled} voiceprint(s); {len(mapping)} cluster(s) named.")

    _write_json(folder / "speakers.json", {"files": summary})
    console.print(f"\n[green]Done[/] — wrote {folder / 'speakers.json'}")
    console.print("[dim]Next: wcfi meeting summarize <folder> (speaker-attributed minutes are on the roadmap).[/]")


def _walk_through(
    audio: Path,
    diar: DiarizationResult,
    proposals: dict,
    snippets: dict,
    snippet_dir: Path,
    play: bool,
) -> dict[str, str]:
    """Ask the human to confirm/label each cluster. Returns cluster-label -> confirmed name."""
    mapping: dict[str, str] = {}
    for cluster in diar.labels():
        proposal = proposals.get(cluster)
        console.print(f"\n[bold cyan]{cluster}[/] in {audio.name}")
        if proposal and proposal.name and proposal.confident:
            if typer.confirm(f"  Auto-matched [green]{proposal.name}[/] "
                             f"(similarity {proposal.score:.2f}). Correct?", default=True):
                mapping[cluster] = proposal.name
                continue

        _preview(cluster, snippets.get(cluster, []), audio, snippet_dir, play)
        hint = ""
        if proposal and proposal.name:
            hint = f" [dim](best guess: {proposal.name}, {proposal.score:.2f})[/]"
        console.print(f"  Who is speaking?{hint}  (Enter a name, or leave blank to skip)")
        answer = typer.prompt("  Name", default="", show_default=False).strip()
        if answer:
            mapping[cluster] = answer
    return mapping


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
