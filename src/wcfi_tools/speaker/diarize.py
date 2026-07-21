"""pyannote diarization backend.

pyannote answers *who spoke when* (segmentation + overlap-aware clustering — the hard part that the
built-in engine does poorly). We then embed each speaker turn with the same torch-free TitaNet
embedder used everywhere else, so the voiceprint store stays backend-independent and existing
enrollments keep working. pyannote is imported lazily so the package still runs without it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .audio import decode
from .hf import MODEL, GatedModelError
from .identify import Seg

SR = 16000
_GATE_HINTS = ("gated", "401", "403", "restricted", "awaiting", "authorized", "unauthorized", "terms", "agree")


def available() -> bool:
    """True if pyannote.audio can be imported."""
    try:
        import pyannote.audio  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed / broken install
        return False
    return True


def load_pipeline(token: str | None, model: str = MODEL):
    """Load the pyannote pipeline, translating auth/gate failures into GatedModelError."""
    try:
        import torch
        from pyannote.audio import Pipeline
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError('pyannote.audio is not installed — pip install -e ".[diarize]"') from exc
    try:
        pipe = Pipeline.from_pretrained(model, token=token)
    except Exception as exc:  # noqa: BLE001 - normalize the many auth/gate error types
        if any(h in str(exc).lower() for h in _GATE_HINTS):
            raise GatedModelError(str(exc)) from exc
        raise
    if pipe is None:  # some versions return None instead of raising when the gate isn't accepted
        raise GatedModelError(f"Could not load {model}: check your Hugging Face token and accept the model terms.")
    if torch.cuda.is_available():
        pipe.to(torch.device("cuda"))
    return pipe


def _turns(pipeline, samples: np.ndarray):
    """Yield (start_s, end_s, speaker_label) for one waveform (handles pyannote 3.x/4.x outputs)."""
    import torch

    wav = {"waveform": torch.from_numpy(np.ascontiguousarray(samples)).unsqueeze(0), "sample_rate": SR}
    out = pipeline(wav)
    diar = getattr(out, "speaker_diarization", out)
    if hasattr(diar, "itertracks"):
        for turn, _track, speaker in diar.itertracks(yield_label=True):
            yield float(turn.start), float(turn.end), str(speaker)
    else:  # pragma: no cover - defensive for future output shapes
        for turn, speaker in diar:
            yield float(turn.start), float(turn.end), str(speaker)


def _centroid(embs: list[np.ndarray]) -> np.ndarray:
    c = np.sum(embs, axis=0)
    n = float(np.linalg.norm(c))
    return c / n if n else c


def diarize(audio_files, embedder, pipeline, *, min_sec: float = 1.0, on_progress=None) -> dict[str, list[Seg]]:
    """Run pyannote per file, embed each turn with TitaNet, return ``{"Voice N": [Seg, ...]}``."""
    per_file: list[tuple[int, list[Seg]]] = []  # one entry per (file, pyannote-speaker)
    for fi, audio in enumerate(audio_files):
        if on_progress:
            on_progress("diarize", fi + 1, len(audio_files))
        samples = decode(Path(audio))
        by_spk: dict[str, list[Seg]] = {}
        for start, end, spk in _turns(pipeline, samples):
            if end - start < min_sec:
                continue
            chunk = samples[int(start * SR) : int(end * SR)]
            if len(chunk) < int(0.3 * SR):
                continue
            by_spk.setdefault(spk, []).append(Seg(Path(audio), start, end, chunk, embedder.embed(chunk)))
        per_file += [(fi, segs) for segs in by_spk.values() if segs]
    groups = merge_across_files(per_file) if len(audio_files) > 1 else [segs for _fi, segs in per_file]
    groups.sort(key=lambda segs: sum(s.end - s.start for s in segs), reverse=True)
    return {f"Voice {i + 1}": segs for i, segs in enumerate(groups)}


def merge_across_files(per_file: list[tuple[int, list[Seg]]], *, threshold: float = 0.55) -> list[list[Seg]]:
    """Merge same-speaker groups that pyannote labeled independently in different files. Groups from
    the *same* file are never merged (pyannote already separated those)."""
    files = [fi for fi, _ in per_file]
    segs = [g for _, g in per_file]
    cents = [_centroid([x.emb for x in g]) for g in segs]
    members = [[i] for i in range(len(segs))]
    fileset = [{fi} for fi in files]
    while len(members) > 1:
        best, pair = -1.0, None
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if fileset[i] & fileset[j]:  # would merge two distinct speakers from one file
                    continue
                sim = float(np.mean([cents[a] @ cents[b] for a in members[i] for b in members[j]]))
                if sim > best:
                    best, pair = sim, (i, j)
        if pair is None or best < threshold:
            break
        i, j = pair
        members[i] += members[j]
        fileset[i] |= fileset[j]
        del members[j]
        del fileset[j]
    return [[s for m in group for s in segs[m]] for group in members]
