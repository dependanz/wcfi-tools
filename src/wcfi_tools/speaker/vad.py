"""Voice-activity detection: split a waveform into speech segments (sherpa-onnx + silero)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import models


@dataclass
class Speech:
    start: float  # seconds from the start of the given waveform
    end: float
    samples: np.ndarray


class Segmenter:
    def __init__(
        self,
        model_path: Path | None = None,
        threshold: float = 0.5,
        min_speech: float = 0.4,
        min_silence: float = 0.3,
    ) -> None:
        import sherpa_onnx

        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_path or models.ensure("vad"))
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.sample_rate = 16000
        self._cfg = cfg

    def segments(self, samples: np.ndarray) -> list[Speech]:
        import sherpa_onnx

        vad = sherpa_onnx.VoiceActivityDetector(self._cfg, buffer_size_in_seconds=30)
        out: list[Speech] = []

        def drain() -> None:
            while not vad.empty():
                seg = vad.front
                s = np.asarray(seg.samples, dtype=np.float32)
                out.append(Speech(seg.start / 16000, (seg.start + len(s)) / 16000, s))
                vad.pop()

        window = 512
        i = 0
        n = len(samples)
        while i < n:
            vad.accept_waveform(samples[i : i + window])
            i += window
            drain()
        vad.flush()
        drain()
        return out
