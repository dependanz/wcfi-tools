"""Match diarized speaker clusters to registered voiceprints, and prep the annotator clips."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import decode, normalize, write_wav


@dataclass
class Seg:
    audio: Path
    start: float
    end: float
    samples: np.ndarray
    emb: np.ndarray
    name: str | None = None


def match_clusters(
    clusters: dict[str, list[Seg]], voiceprints: dict[str, np.ndarray], *, threshold: float = 0.5
) -> tuple[dict[str, str], dict[str, list[Seg]]]:
    """Assign each *whole* cluster to the nearest registered speaker by centroid similarity (used
    after diarization, which already groups turns by speaker). Returns
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
            if samp is None or len(samp) == 0:  # diarization keeps no audio — re-decode the turn
                samp = decode(m.audio, start=m.start, dur=max(0.2, m.end - m.start))
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
