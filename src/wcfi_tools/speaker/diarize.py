"""Speaker diarization via sherpa-onnx (offline, non-gated, torch-free).

sherpa-onnx answers *who spoke when* using the pyannote segmentation model as ONNX + speaker
embeddings + clustering — no account, token, or gated form; the models auto-download from k2-fsa's
public GitHub releases. We then re-embed each speaker turn with the same TitaNet embedder used for
enrollment, so the voiceprint store stays independent and registered speakers keep matching.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import models
from .audio import decode
from .identify import Seg

SR = 16000


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
    audio_files, embedder, diarizer, *, min_sec: float = 1.0, on_progress=None, on_chunk=None
) -> dict[str, list[Seg]]:
    """Diarize each file, embed every turn with TitaNet, return ``{"Voice N": [Seg, ...]}``.
    ``on_chunk(done, total)`` (if given) is called during processing to report progress."""
    per_file: list[tuple[int, list[Seg]]] = []  # one entry per (file, sherpa-speaker)
    for fi, audio in enumerate(audio_files):
        if on_progress:
            on_progress("diarize", fi + 1, len(audio_files))
        samples = decode(Path(audio))
        by_spk: dict[int, list[Seg]] = {}
        for start, end, spk in _segments(diarizer, samples, on_chunk):
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
    """Merge same-speaker groups labeled independently in different files. Groups from the *same*
    file are never merged (the diarizer already separated those)."""
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
