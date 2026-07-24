"""Speaker diarization via sherpa-onnx (offline, non-gated, torch-free).

sherpa-onnx answers *who spoke when* using the pyannote segmentation model as ONNX + speaker
embeddings + clustering — no account, token, or gated form; the models auto-download from k2-fsa's
public GitHub releases. We then re-embed each speaker turn with the same TitaNet embedder used for
enrollment, so the voiceprint store stays independent and registered speakers keep matching.
"""

from __future__ import annotations

from math import ceil
from pathlib import Path

import numpy as np

from . import models
from .audio import decode, duration
from .identify import Seg

SR = 16000
# Diarize long meetings in windows so a single process() call never has to hold hours of audio +
# clustering buffers (a 2.5 h file at once exhausts memory). Speakers are merged back across windows.
WINDOW_SEC = 900  # 15 minutes


def available() -> bool:
    """True if sherpa-onnx (with the diarization API) can be imported."""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed / broken install
        return False
    return True


def load_diarizer(*, threshold: float = 0.5, num_speakers: int = -1, log=print):
    """Build a sherpa-onnx OfflineSpeakerDiarization (pyannote segmentation + TitaNet + clustering).

    ``num_speakers`` < 0 lets clustering estimate the count using ``threshold`` (higher = more,
    finer-grained speakers); set it to a positive integer if the count is known.
    """
    import sherpa_onnx as so

    seg = models.ensure("segmentation", log=log)
    emb = models.ensure("embedding", log=log)
    config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(emb)),
        clustering=so.FastClusteringConfig(num_clusters=num_speakers, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("sherpa-onnx diarization config failed validation (check the model files).")
    return so.OfflineSpeakerDiarization(config)


def _segments(diarizer, samples: np.ndarray, callback=None):
    """Yield (start_s, end_s, speaker_index) for one 16 kHz float32 waveform. ``callback(done,
    total) -> int`` (return 0 to continue) reports chunk progress during processing."""
    result = diarizer.process(samples, callback=callback) if callback else diarizer.process(samples)
    for r in result.sort_by_start_time():
        yield float(r.start), float(r.end), int(r.speaker)


def _centroid(embs: list[np.ndarray]) -> np.ndarray:
    c = np.sum(embs, axis=0)
    n = float(np.linalg.norm(c))
    return c / n if n else c


def diarize(
    audio_files, embedder, diarizer, *, min_sec: float = 1.0, num_speakers: int = 0,
    on_progress=None, on_chunk=None,
) -> dict[str, list[Seg]]:
    """Diarize each file in ``WINDOW_SEC`` windows, embed every turn with TitaNet, and merge the same
    speakers across windows. Returns ``{"Voice N": [Seg, ...]}``. Segments carry only their embedding
    (not raw audio — the annotator re-decodes clips lazily), so hours of meeting stay memory-light.
    ``num_speakers`` > 0 targets that many final voices; ``on_chunk(done, total)`` reports progress."""
    units: list[tuple[int, list[Seg]]] = []  # (window_id, [Seg]); a window's speakers never re-merge
    win_id = 0
    for fi, audio in enumerate(audio_files):
        if on_progress:
            on_progress("diarize", fi + 1, len(audio_files))
        nwin = max(1, ceil(duration(Path(audio)) / WINDOW_SEC))
        for wi in range(nwin):
            offset = float(wi * WINDOW_SEC)
            ws = decode(Path(audio), start=offset, dur=float(WINDOW_SEC))  # just this window
            if len(ws) < int(min_sec * SR):
                win_id += 1
                continue

            def _cb(done, total, _wi=wi, _nwin=nwin):
                if on_chunk:
                    on_chunk(int((_wi + done / max(total, 1)) / _nwin * 1000), 1000)
                return 0

            by_spk: dict[int, list[Seg]] = {}
            for start, end, spk in _segments(diarizer, ws, _cb if on_chunk else None):
                if end - start < min_sec:
                    continue
                chunk = ws[int(start * SR) : int(end * SR)]
                if len(chunk) < int(0.3 * SR):
                    continue
                emb = embedder.embed(chunk)  # keep only the embedding, not the audio
                by_spk.setdefault(spk, []).append(
                    Seg(Path(audio), offset + start, offset + end, np.empty(0, np.float32), emb)
                )
            units += [(win_id, segs) for segs in by_spk.values() if segs]
            win_id += 1
    groups = merge_units(units, num_speakers=num_speakers)
    groups.sort(key=lambda segs: sum(s.end - s.start for s in segs), reverse=True)
    return {f"Voice {i + 1}": segs for i, segs in enumerate(groups)}


def merge_units(
    units: list[tuple[int, list[Seg]]], *, threshold: float = 0.45, num_speakers: int = 0
) -> list[list[Seg]]:
    """Merge same-speaker groups labeled independently in different windows/files. Groups sharing a
    unit id (same window) are never merged — the diarizer already separated those. With
    ``num_speakers`` > 0, keep merging the closest pairs until that many groups remain (best effort);
    otherwise merge while similarity stays above ``threshold``."""
    if not units:
        return []
    ids = [u for u, _ in units]
    segs = [g for _, g in units]
    cents = [_centroid([x.emb for x in g]) for g in segs]
    members = [[i] for i in range(len(segs))]
    unitset = [{u} for u in ids]
    target = num_speakers if num_speakers and num_speakers > 0 else None
    while len(members) > 1:
        best, pair = -1.0, None
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if unitset[i] & unitset[j]:  # two distinct speakers from the same window
                    continue
                sim = float(np.mean([cents[a] @ cents[b] for a in members[i] for b in members[j]]))
                if sim > best:
                    best, pair = sim, (i, j)
        if pair is None:  # nothing left that's allowed to merge
            break
        if target is not None:
            if len(members) <= target:
                break
        elif best < threshold:
            break
        i, j = pair
        members[i] += members[j]
        unitset[i] |= unitset[j]
        del members[j]
        del unitset[j]
    return [[s for m in group for s in segs[m]] for group in members]
