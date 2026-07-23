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
        from ..speaker import diarize, embed, identify, models, store
    except ImportError as exc:
        console.print(
            f"[red]Speaker dependencies aren't importable[/] ({exc}). Reinstall wcfi-tools:\n"
            "  pip install -e ."
        )
        raise typer.Exit(1) from None
    return embed, identify, models, store, diarize


def _embedder(embed, models):
    return embed.Embedder(models.ensure("embedding", log=console.print))


def _roster_note(names) -> str:
    unique = sorted({n for n in names if n})
    if not unique:
        return ""
    return (
        "Known people present in this meeting (from verified speaker identification) — use these for "
        "the Attendance section and to attribute movers/seconders when the transcript supports it; do "
        f"not invent attributions: {', '.join(unique)}."
    )


# A real recurring speaker talks for at least this many seconds total; smaller clusters are
# one-off blips / cross-talk and are dropped so there aren't dozens of "voices" to name.
MIN_CLUSTER_SEC = 6.0
# sherpa FastClustering merge threshold when the speaker count isn't given: HIGHER = fewer/coarser
# voices. Tuned high for TitaNet embeddings, which otherwise over-split badly.
CLUSTER_THRESHOLD = 0.85
# cosine similarity for deciding a diarized cluster IS an already-registered speaker.
MATCH_THRESHOLD = 0.5


def _big_clusters(raw: dict, *, min_sec: float = MIN_CLUSTER_SEC) -> dict:
    """Drop one-off / cross-talk voices and relabel the rest 'Voice N' by total speaking time."""
    big = [m for m in raw.values() if sum(s.end - s.start for s in m) >= min_sec]
    big.sort(key=lambda m: sum(s.end - s.start for s in m), reverse=True)
    return {f"Voice {i + 1}": m for i, m in enumerate(big)}


def _run(audio_files, work_dir, *, annotate: bool, on_progress=None, num_speakers: int = 0):
    """Diarize (sherpa-onnx) → match to store → annotate unknowns. Returns (present, newly_saved).
    ``num_speakers`` > 0 pins the count (use when attendance is known); 0 lets it auto-detect."""
    embed, identify, models, store, diarize = _modules()
    console.print("  loading speaker models (first run downloads them)…")
    embedder = _embedder(embed, models)
    voiceprints = store.load()
    diarizer = diarize.load_diarizer(
        threshold=CLUSTER_THRESHOLD, num_speakers=(num_speakers or -1), log=console.print
    )

    def _chunk(done: int, total: int) -> int:
        print(f"\r  separating speakers… {done * 100 // max(total, 1)}%", end="", flush=True)
        return 0

    groups = diarize.diarize(audio_files, embedder, diarizer, on_progress=on_progress, on_chunk=_chunk)
    print("\r  separating speakers… done.            ")
    named, unknown = identify.match_clusters(groups, voiceprints, threshold=MATCH_THRESHOLD)
    present: set[str] = set(named.values())
    # if the count was pinned, trust it — show even briefly-heard people instead of dropping them
    clusters = _big_clusters(unknown, min_sec=1.0 if num_speakers else MIN_CLUSTER_SEC)

    newly: dict[str, int] = {}
    if annotate and clusters:
        console.print(f"  {len(clusters)} distinct voice(s) to name; preparing clips…")
        voices = identify.prepare_annotation(clusters, Path(work_dir), embedder)
        from ..web import run_annotator

        labels, cancelled = run_annotator(voices, known_names=list(voiceprints))
        if cancelled:
            console.print("[yellow]Speaker attribution cancelled — using auto-matched names only.[/]")
            return present, newly
        for label, name in labels.items():
            name = (name or "").strip()
            if name and name.lower() != "unsure":
                store.upsert(name, [s.emb for s in clusters[label]])
                present.add(name)
                newly[name] = len(clusters[label])
    return present, newly


def identify_present(audio_files, work_dir, *, on_progress=None, num_speakers: int = 0):
    """Used by `summarize --identify`: returns (roster dict, roster_note str)."""
    present, newly = _run(audio_files, work_dir, annotate=True, on_progress=on_progress, num_speakers=num_speakers)
    if newly:
        console.print("  [green]newly registered:[/] " + ", ".join(newly))
    roster = {name: "identified" for name in sorted(present)}
    return roster, _roster_note(present)


@app.command("register")
def register(
    folder: Path = typer.Argument(..., help="Meeting folder / audio to learn the distinct voices from"),
    speakers: int = typer.Option(
        0, "--speakers", "-n", help="How many people are present (0 = auto-detect). Set it when you know."
    ),
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
    _, newly = _run(audio, folder / "_work", annotate=True, num_speakers=speakers)
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
