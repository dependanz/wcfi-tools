"""Provider interfaces.

Three capabilities are intentionally separate because they map to different backends:

* ``Transcriber`` (audio -> text) — only providers with audio models qualify (OpenAI, local Whisper).
* ``Summarizer``  (LLM structured JSON) — provider-agnostic (OpenAI or Anthropic/Claude).
* ``Diarizer``    (audio -> "who spoke when" + per-speaker voiceprints) — a dedicated speech model
  (e.g. pyannote), never the summarizer LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Summarizer(Protocol):
    name: str

    def structured_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        reasoning_effort: str = "low",
    ) -> dict[str, Any]:
        """Return a dict conforming to ``schema``."""
        ...


@runtime_checkable
class Transcriber(Protocol):
    name: str

    def transcribe(self, audio_path: Path, *, prompt: str) -> tuple[str, dict[str, Any]]:
        """Return (text, raw_response_json)."""
        ...


@dataclass(frozen=True)
class SpeakerTurn:
    """A single contiguous stretch of speech attributed to one (anonymous) cluster."""

    start: float
    end: float
    speaker: str  # anonymous cluster label from the diarizer, e.g. "SPEAKER_00"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class DiarizationResult:
    """Output of a :class:`Diarizer`: time-stamped turns plus one voiceprint per cluster.

    ``embeddings`` maps an anonymous cluster label to its centroid voiceprint (a plain list of
    floats so it is trivially JSON-serialisable and comparable without numpy).
    """

    turns: list[SpeakerTurn] = field(default_factory=list)
    embeddings: dict[str, list[float]] = field(default_factory=dict)

    def labels(self) -> list[str]:
        """Distinct cluster labels, preferring the embedding keys, else derived from turns."""
        if self.embeddings:
            return sorted(self.embeddings)
        return sorted({turn.speaker for turn in self.turns})


@runtime_checkable
class Diarizer(Protocol):
    name: str

    def diarize(self, audio_path: Path) -> DiarizationResult:
        """Return diarized turns and per-cluster voiceprints for one audio file."""
        ...
