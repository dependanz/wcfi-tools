"""Tie it together: segment a meeting's audio, embed, match to voiceprints, cluster the rest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import decode, normalize, write_wav
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


def match_clusters(
    clusters: dict[str, list[Seg]], voiceprints: dict[str, np.ndarray], *, threshold: float = 0.5
) -> tuple[dict[str, str], dict[str, list[Seg]]]:
    """Assign each *whole* cluster to the nearest registered speaker by centroid similarity (used by
    the pyannote backend, which already groups turns by speaker). Returns
    ``(named={label: name}, unknown={label: [Seg]})``; named clusters' segments get ``.name`` set."""
    names = list(voiceprints)
    mat = np.stack([voiceprints[n] for n in names]) if names else None
    named: dict[str, str] = {}
    unknown: dict[str, list[Seg]] = {}
    for label, segs in clusters.items():
        if mat is not None and segs:
            cen = sum(s.emb for s in segs)
            cen = cen / (np.linalg.norm(cen) or 1.0)
            sims = mat @ cen
            j = int(np.argmax(sims))
            if float(sims[j]) >= threshold:
                for s in segs:
                    s.name = names[j]
                named[label] = names[j]
                continue
        unknown[label] = segs
    return named, unknown


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


# A shown clip must have at least one window this similar to the voice's centroid, otherwise its
# spectrogram would carry no highlight and give the annotator nothing to go on (the "no green" case).
GREEN_MIN = 0.42


def prepare_annotation(
    clusters: dict[str, list[Seg]],
    work_dir: Path,
    embedder,
    *,
    per: int = 2,
    clip_sec: float = 4.0,
    candidates: int = 4,
) -> list[dict]:
    """Pick clips that actually contain each voice, cut loudness-normalized wavs, and compute
    spectrogram + target-highlight data. For each voice we score several candidates (closest to the
    centroid) by how strongly the voice shows up window-by-window, keep those with a real highlight,
    and drop clips that would render with no green. Returns a list of
    ``{label, seconds, clips: [{path, spec, scores, dur}]}``."""
    from . import viz

    snip_dir = Path(work_dir) / "snippets"
    snip_dir.mkdir(parents=True, exist_ok=True)
    n = int(clip_sec * 16000)
    voices: list[dict] = []
    for label, members in clusters.items():
        centroid = sum(m.emb for m in members)
        centroid = centroid / (np.linalg.norm(centroid) or 1.0)
        ranked = sorted(members, key=lambda m: float(m.emb @ centroid), reverse=True)
        scored = []  # (peak_window_score, viz_dict, samples)
        for m in ranked[: max(per, candidates)]:
            samp = m.samples
            if len(samp) > n:  # middle clip_sec seconds
                start = (len(samp) - n) // 2
                samp = samp[start : start + n]
            samp = normalize(samp)
            v = viz.clip_viz(samp, centroid, embedder)
            peak = max((s["score"] for s in v["scores"]), default=0.0)
            scored.append((peak, v, samp))
        scored.sort(key=lambda t: t[0], reverse=True)
        keep = [t for t in scored if t[0] >= GREEN_MIN][:per] or scored[:1]  # always show one
        clips = []
        for k, (_peak, v, samp) in enumerate(keep):
            path = snip_dir / f"{label.replace(' ', '_')}_{k}.wav"
            write_wav(path, samp)
            clips.append({"path": path, "spec": v["spec"], "scores": v["scores"], "dur": v["dur"]})
        voices.append(
            {"label": label, "seconds": round(sum(m.end - m.start for m in members)), "clips": clips}
        )
    return voices
