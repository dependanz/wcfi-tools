"""Provider interfaces.

Two capabilities are intentionally separate because they map to different backends:

* ``Transcriber`` (audio -> text) — only providers with audio models qualify (OpenAI, local Whisper).
* ``Summarizer``  (LLM structured JSON) — provider-agnostic (OpenAI or Anthropic/Claude).
"""

from __future__ import annotations

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
