"""`wcfi meeting speakers …` — register, list, and remove speaker voiceprints (local, torch-free)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..guards import require_configured
from ..meeting.pipeline import discover_audio

app = typer.Typer(help="Register and manage speaker voiceprints (stored locally).", no_args_is_help=True)
console = Console()


def _modules():
    try:
        from ..speaker import embed, identify, models, store, vad
    except ImportError:
        console.print('[red]Speaker features need the extra:[/] pip install ".[speaker]"')
        raise typer.Exit(1) from None
    return embed, identify, models, store, vad


def _engine(embed, models, vad):
    epath = models.ensure("embedding", log=console.print)
    vpath = models.ensure("vad", log=console.print)
    return embed.Embedder(epath), vad.Segmenter(vpath)


def _roster_note(names) -> str:
    unique = sorted({n for n in names if n})
    if not unique:
        return ""
    return (
        "Known people present in this meeting (from verified speaker identification) — use these for "
        "the Attendance section and to attribute movers/seconders when the transcript supports it; do "
        f"not invent attributions: {', '.join(unique)}."
    )


def _run(audio_files, work_dir, *, annotate: bool, on_progress=None, threshold: float = 0.5):
    """Analyze audio → match to store → cluster + annotate unknowns. Returns (present, newly_saved)."""
    embed, identify, models, store, vad = _modules()
    console.print("  loading speaker models (first run downloads them)…")
    embedder, segmenter = _engine(embed, models, vad)
    segs = identify.analyze(audio_files, embedder, segmenter, on_progress=on_progress)
    voiceprints = store.load()
    identify.match(segs, voiceprints, threshold=threshold)
    present = {s.name for s in segs if s.name}
    newly: dict[str, int] = {}
    unknown = [s for s in segs if s.name is None]
    if annotate and unknown:
        clusters = identify.cluster_unknown(unknown, threshold=threshold)
        snippets, seconds = identify.snippets_for(clusters, Path(work_dir))
        from ..web import run_annotator

        labels = run_annotator(snippets, seconds, known_names=list(voiceprints))
        for label, name in labels.items():
            name = (name or "").strip()
            if name and name.lower() != "unsure":
                store.upsert(name, [s.emb for s in clusters[label]])
                present.add(name)
                newly[name] = len(clusters[label])
    return present, newly


def identify_present(audio_files, work_dir, *, on_progress=None):
    """Used by `summarize --identify`: returns (roster dict, roster_note str)."""
    present, newly = _run(audio_files, work_dir, annotate=True, on_progress=on_progress)
    if newly:
        console.print("  [green]newly registered:[/] " + ", ".join(newly))
    roster = {name: "identified" for name in sorted(present)}
    return roster, _roster_note(present)


@app.command("register")
def register(
    folder: Path = typer.Argument(..., help="Meeting folder / audio to learn the distinct voices from"),
) -> None:
    """Detect the distinct voices in a recording and name them (saves voiceprints for reuse)."""
    require_configured()
    folder = folder.resolve()
    if not folder.is_dir():
        console.print(f"[red]Not a folder:[/] {folder}")
        raise typer.Exit(1)
    audio = discover_audio(folder)
    if not audio:
        console.print("[yellow]No audio files found in that folder.[/]")
        raise typer.Exit(1)
    console.print(f"Listening for distinct voices in [bold]{folder.name}[/]…")
    _, newly = _run(audio, folder / "_work", annotate=True)
    if newly:
        console.print("[green]Registered:[/] " + ", ".join(f"{n} ({c} clips)" for n, c in newly.items()))
    else:
        console.print("[yellow]No new speakers registered.[/]")


@app.command("list")
def list_speakers() -> None:
    """List the speakers registered on this machine."""
    _, _, _, store, _ = _modules()
    rows = store.list_speakers()
    if not rows:
        console.print("No speakers registered yet. Run: [bold]wcfi meeting speakers register <folder>[/]")
        return
    console.print("[bold]Registered speakers:[/]")
    for r in rows:
        console.print(f"  {r['name']}  [dim]({r['count']} clips, updated {r['updated']})[/]")


@app.command("remove")
def remove(name: str = typer.Argument(..., help="Speaker name to remove")) -> None:
    """Remove a registered speaker's voiceprint."""
    _, _, _, store, _ = _modules()
    if store.remove(name):
        console.print(f"[green]Removed[/] {name}")
    else:
        console.print(f"[yellow]Not found:[/] {name}")
