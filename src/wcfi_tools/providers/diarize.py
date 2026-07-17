"""Speaker diarization: cluster an audio file into anonymous speaker turns.

``PyannoteDiarizer`` is the real backend (local, needs a HuggingFace token + the ``[speaker]``
extra). ``WindowDiarizer`` is a dependency-free dev stub that fakes turns so the attribution
flow (snippet selection + annotator + roster injection) can be exercised without pyannote.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core import ffmpeg


@dataclass(frozen=True)
class DiarTurn:
    speaker: str  # anonymous cluster label, e.g. "Speaker A"
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@runtime_checkable
class Diarizer(Protocol):
    name: str

    def diarize(self, audio_path: Path) -> list[DiarTurn]:
        ...


class WindowDiarizer:
    """DEV/TEST ONLY: split audio into fixed windows and round-robin fake speakers.

    Not real diarization — it just produces plausible turn structure so the rest of the
    attribution pipeline can be built and tested without torch/pyannote/a HF token.
    """

    name = "window"

    def __init__(self, window_seconds: float = 15.0, speakers: int = 3) -> None:
        self.window_seconds = window_seconds
        self.speakers = max(1, speakers)

    def diarize(self, audio_path: Path) -> list[DiarTurn]:
        duration = ffmpeg.ffprobe_duration(audio_path)
        turns: list[DiarTurn] = []
        start = 0.0
        idx = 0
        while start < duration:
            end = min(start + self.window_seconds, duration)
            label = f"Speaker {chr(ord('A') + (idx % self.speakers))}"
            turns.append(DiarTurn(label, round(start, 3), round(end, 3)))
            start = end
            idx += 1
        return turns


class PyannoteDiarizer:
    """Real diarization via pyannote.audio (imported lazily; needs the ``[speaker]`` extra)."""

    name = "pyannote"

    def __init__(self, hf_token: str, model: str = "pyannote/speaker-diarization-3.1") -> None:
        self.hf_token = hf_token
        self.model = model

    def diarize(self, audio_path: Path) -> list[DiarTurn]:
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:  # pragma: no cover - optional heavy dep
            raise RuntimeError(
                "pyannote.audio is not installed. Install the speaker extra: "
                'pip install "wcfi-tools[speaker]"'
            ) from exc

        pipeline = Pipeline.from_pretrained(self.model, use_auth_token=self.hf_token)
        if pipeline is None:  # pragma: no cover - happens on auth/ToS failure
            raise RuntimeError(
                f"Could not load {self.model}. Accept the model's conditions on HuggingFace and "
                "check your HF token (run `wcfi setup`)."
            )
        try:  # move to GPU when available
            import torch

            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
        except Exception:  # pragma: no cover - best effort
            pass

        annotation = pipeline(str(audio_path))
        turns = [
            DiarTurn(str(speaker), round(float(segment.start), 3), round(float(segment.end), 3))
            for segment, _, speaker in annotation.itertracks(yield_label=True)
        ]
        turns.sort(key=lambda t: t.start)
        return turns
