"""`wcfi meeting speakers …` — register, list, and remove speaker voiceprints (local, torch-free)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .. import config as cfg
from ..guards import require_configured
from ..meeting.pipeline import discover_audio

app = typer.Typer(help="Register and manage speaker voiceprints (stored locally).", no_args_is_help=True)
console = Console()


def _modules():
    try:
        from ..speaker import embed, identify, models, store, vad
    except ImportError:
        console.print(
            '[red]Speaker identification needs the optional extra[/] (torch-free). Install it, then '
            'retry:\n  pip install -e ".\\[speaker]"'
        )
        raise typer.Exit(1) from None
    return embed, identify, models, store, vad


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


def _select_backend(config) -> str:
    """Pick the speaker-separation backend: explicit config, or 'auto' — pyannote when a Hugging
    Face token + install are present, else the built-in torch-free engine."""
    backend = config.get("diarize", {}).get("backend", "auto")
    if backend in ("pyannote", "onnx"):
        return backend
    from ..speaker import diarize

    return "pyannote" if (cfg.get_secret("huggingface") and diarize.available()) else "onnx"


def _big_clusters(raw: dict) -> dict:
    """Drop one-off / cross-talk voices and relabel the rest 'Voice N' by total speaking time."""
    big = [m for m in raw.values() if sum(s.end - s.start for s in m) >= MIN_CLUSTER_SEC]
    big.sort(key=lambda m: sum(s.end - s.start for s in m), reverse=True)
    return {f"Voice {i + 1}": m for i, m in enumerate(big)}


def _gate_help() -> None:
    from ..speaker import hf

    console.print(
        "[yellow]pyannote model access needed[/] (free, one-time). Using the built-in engine for now.\n"
        f"  1. Create a token: {hf.TOKENS_URL}\n"
        f"  2. Accept the terms: {hf.ACCEPT_URL}\n"
        "  3. Re-run [bold]wcfi setup[/] to store the token (or set HF_TOKEN)."
    )


def _diarize_pyannote(audio_files, embedder, identify, voiceprints, *, on_progress, threshold):
    """Returns (present:set, clusters:dict) via pyannote, or None to fall back to the onnx engine."""
    from ..speaker import diarize, hf

    if not diarize.available():
        console.print('[yellow]pyannote not installed[/] — using built-in engine (pip install -e ".[diarize]").')
        return None
    try:
        pipeline = diarize.load_pipeline(cfg.get_secret("huggingface"))
    except hf.GatedModelError:
        _gate_help()
        return None
    except Exception as exc:  # noqa: BLE001 - any load failure should degrade, not crash
        console.print(f"[yellow]pyannote failed to load[/] ({exc}); using built-in engine.")
        return None
    console.print("  separating speakers with pyannote (community-1)…")
    groups = diarize.diarize(audio_files, embedder, pipeline, on_progress=on_progress)
    named, unknown = identify.match_clusters(groups, voiceprints, threshold=threshold)
    return set(named.values()), _big_clusters(unknown)


def _run(audio_files, work_dir, *, annotate: bool, on_progress=None, threshold: float = 0.5):
    """Analyze audio → match to store → cluster + annotate unknowns. Returns (present, newly_saved)."""
    embed, identify, models, store, vad = _modules()
    console.print("  loading speaker models (first run downloads them)…")
    embedder = _embedder(embed, models)
    voiceprints = store.load()
    backend = _select_backend(cfg.load_config())

    present: set[str] = set()
    clusters: dict = {}
    if backend == "pyannote":
        got = _diarize_pyannote(
            audio_files, embedder, identify, voiceprints, on_progress=on_progress, threshold=threshold
        )
        if got is None:
            backend = "onnx"  # graceful fallback
        else:
            present, clusters = got
    if backend == "onnx":
        segmenter = vad.Segmenter(models.ensure("vad", log=console.print))
        segs = identify.analyze(audio_files, embedder, segmenter, on_progress=on_progress)
        identify.match(segs, voiceprints, threshold=threshold)
        present = {s.name for s in segs if s.name}
        unknown = [s for s in segs if s.name is None]
        clusters = _big_clusters(identify.cluster_unknown(unknown, threshold=threshold)) if unknown else {}

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
