"""Speaker embeddings (voice fingerprints) via sherpa-onnx."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import models


class Embedder:
    """Turns a waveform into an L2-normalized speaker embedding."""

    def __init__(self, model_path: Path | None = None, num_threads: int = 2) -> None:
        import sherpa_onnx

        path = str(model_path or models.ensure("embedding"))
        self._ext = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=path, num_threads=num_threads)
        )

    def embed(self, samples: np.ndarray) -> np.ndarray:
        stream = self._ext.create_stream()
        stream.accept_waveform(16000, samples)
        stream.input_finished()
        vec = np.asarray(self._ext.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec
