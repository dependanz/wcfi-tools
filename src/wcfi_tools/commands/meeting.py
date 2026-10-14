"""Guided `wcfi meeting summarize` command and existing speaker tools."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import typer

from ..guards import require_configured
from ..meeting import configuration as mc
from ..meeting import guided
from ..providers import ProviderError
from . import speakers

app = typer.Typer(help="Summarize meetings; manage speakers.", no_args_is_help=True)
app.add_typer(speakers.app, name="speakers")


@app.command("summarize")
def summarize(
    folder: Optional[Path] = typer.Argument(None, help="Meeting folder; prompts when omitted."),
    provider: Optional[str] = typer.Option(None, "--provider", "--summary-provider",
                                           help="Text-to-minutes provider: openai|anthropic."),
    summary_model: Optional[str] = typer.Option(None, "--summary-model"),
    transcription_provider: Optional[str] = typer.Option(None, "--transcription-provider",
                                                         help="Audio-to-text provider."),
    transcribe_model: Optional[str] = typer.Option(None, "--transcribe-model"),
    chunk_minutes: Optional[float] = typer.Option(None, "--chunk-minutes"),
    emit: Optional[str] = typer.Option(None, "--emit", help="md or all; Markdown is required."),
    force: bool = typer.Option(False, "--force", help="Rebuild matching cache after explicit start."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Inspect config/inventory without API calls."),
    prepared_by: Optional[str] = typer.Option(None, "--prepared-by"),
    location: Optional[str] = typer.Option(None, "--location"),
    meeting_time: Optional[str] = typer.Option(None, "--meeting-time"),
    meeting_date: Optional[str] = typer.Option(None, "--meeting-date", help="YYYY-MM-DD or unknown."),
    title: Optional[str] = typer.Option(None, "--title"),
    output: Optional[Path] = typer.Option(None, "--output", help="Private output directory."),
    identify: Optional[bool] = typer.Option(None, "--identify/--no-identify"),
    speakers_present: Optional[int] = typer.Option(None, "--speakers", "-n"),
) -> None:
    """Review meeting.wcfi.toml or build it interactively, then explicitly start."""
    if not dry_run:
        require_configured()
    overrides: dict = {}
    values = {
        "processing.summarization.provider": provider,
        "processing.summarization.model": summary_model,
        "processing.transcription.provider": transcription_provider,
        "processing.transcription.model": transcribe_model,
        "processing.transcription.chunk_minutes": chunk_minutes,
        "processing.speakers.identify": identify,
        "processing.speakers.count": speakers_present,
        "meeting.prepared_by": prepared_by, "meeting.location": location,
        "meeting.time": meeting_time, "meeting.title": title,
        "output.directory": str(output) if output is not None else None,
    }
    try:
        if meeting_date is not None:
            values["meeting.date"] = ("unknown" if meeting_date == "unknown"
                                      else date.fromisoformat(meeting_date))
        if emit is not None:
            if emit not in {"md", "all", "md,paste"}:
                raise mc.ConfigError("--emit must include Markdown: use md, md,paste, or all.")
            values["output.formats"] = (["markdown"] if emit == "md"
                                        else ["markdown", "plain_text"])
        for field, value in values.items():
            if value is not None:
                guided.set_value(overrides, field, value)
        guided.run(folder, overrides, dry_run=dry_run, force=force)
    except typer.Abort:
        typer.echo("Cancelled; no further processing.")
        raise typer.Exit(1) from None
    except (ValueError, OSError, RuntimeError, ProviderError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
