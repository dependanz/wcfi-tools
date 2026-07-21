"""Tie it together: segment a meeting's audio, embed, match to voiceprints, cluster the rest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import decode, write_wav
from .embed import Embedder
from .vad import Segmenter

Progress = Callable[[str, int, int], None]


@dataclass
class Seg:
    audio: Path
    start: float
    end: float
    samples: np.ndarray
    emb: np.ndarray
    name: str | None = None


def analyze(
    audio_files: list[Path],
    embedder: Embedder,
    segmenter: Segmenter,
    *,
    min_sec: float = 1.2,
    on_progress: Progress | None = None,
) -> list[Seg]:
    """VAD + embed every speech segment across the meeting's audio."""
    segs: list[Seg] = []
    for idx, audio in enumerate(audio_files, 1):
        if on_progress:
            on_progress("listen", idx, len(audio_files))
        samples = decode(Path(audio))
        for sp in segmenter.segments(samples):
            if sp.end - sp.start < min_sec:
                continue
            segs.append(Seg(Path(audio), sp.start, sp.end, sp.samples, embedder.embed(sp.samples)))
    return segs


def match(segs: list[Seg], voiceprints: dict[str, np.ndarray], *, threshold: float = 0.5) -> None:
    """Label each segment with the nearest registered speaker (or leave None)."""
    names = list(voiceprints)
    mat = np.stack([voiceprints[n] for n in names]) if names else None
    for s in segs:
        if mat is None:
            s.name = None
            continue
        sims = mat @ s.emb
        j = int(np.argmax(sims))
        s.name = names[j] if float(sims[j]) >= threshold else None


def cluster_unknown(segs: list[Seg], *, threshold: float = 0.55) -> dict[str, list[Seg]]:
    """Agglomerative (average-linkage) cosine clustering of unnamed segments into distinct voices."""
    unknown = [s for s in segs if s.name is None]
    n = len(unknown)
    if n == 0:
        return {}
    emb = np.stack([s.emb for s in unknown])
    sim = emb @ emb.T  # cosine, embeddings are unit-norm
    groups: list[list[int]] = [[i] for i in range(n)]
    while len(groups) > 1:
        best, pair = -1.0, None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                s = float(sim[np.ix_(groups[i], groups[j])].mean())
                if s > best:
                    best, pair = s, (i, j)
        if pair is None or best < threshold:
            break
        i, j = pair
        groups[i] += groups[j]
        del groups[j]
    groups.sort(key=lambda g: sum(unknown[k].end - unknown[k].start for k in g), reverse=True)
    return {f"Voice {c + 1}": [unknown[k] for k in g] for c, g in enumerate(groups)}


def prepare_annotation(
    clusters: dict[str, list[Seg]], work_dir: Path, embedder, *, per: int = 2, clip_sec: float = 4.0
) -> list[dict]:
    """Pick the most-representative clips per voice (closest to its centroid, trimmed short), cut
    wavs, and compute spectrogram + target-highlight data. Returns a list of
    ``{label, seconds, clips: [{path, spec, scores, dur}]}``."""
    from . import viz

    snip_dir = Path(work_dir) / "snippets"
    snip_dir.mkdir(parents=True, exist_ok=True)
    n = int(clip_sec * 16000)
    voices: list[dict] = []
    for label, members in clusters.items():
        centroid = sum(m.emb for m in members)
        centroid = centroid / (np.linalg.norm(centroid) or 1.0)
        ranked = sorted(members, key=lambda m: float(m.emb @ centroid), reverse=True)[:per]
        clips = []
        for k, m in enumerate(ranked):
            samp = m.samples
            if len(samp) > n:  # middle clip_sec seconds
                start = (len(samp) - n) // 2
                samp = samp[start : start + n]
            path = snip_dir / f"{label.replace(' ', '_')}_{k}.wav"
            write_wav(path, samp)
            v = viz.clip_viz(samp, centroid, embedder)
            clips.append({"path": path, "spec": v["spec"], "scores": v["scores"], "dur": v["dur"]})
        voices.append(
            {"label": label, "seconds": round(sum(m.end - m.start for m in members)), "clips": clips}
        )
    return voices
