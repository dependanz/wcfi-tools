"""Provider factory — build a Summarizer/Transcriber from config + stored secrets."""

from __future__ import annotations

from typing import Any

from .. import config as cfg
from .base import DiarizationResult, Diarizer, SpeakerTurn, Summarizer, Transcriber

__all__ = [
    "Summarizer",
    "Transcriber",
    "Diarizer",
    "DiarizationResult",
    "SpeakerTurn",
    "build_summarizer",
    "build_transcriber",
    "build_diarizer",
    "ProviderError",
]


class ProviderError(RuntimeError):
    """Raised when a provider can't be built (e.g. missing API key)."""


def _require_key(provider: str) -> str:
    key = cfg.get_secret(provider)
    if not key:
        env = cfg.SECRET_ENV_VARS.get(provider, provider.upper())
        raise ProviderError(
            f"No API key for '{provider}'. Run `wcfi setup`, or set the {env} environment variable."
        )
    return key


def build_summarizer(config: dict[str, Any], *, provider: str | None = None, model: str | None = None) -> Summarizer:
    provider = provider or config["providers"]["summarizer"]
    if provider == "openai":
        from .openai_provider import OpenAISummarizer

        return OpenAISummarizer(_require_key("openai"), model or config["models"]["openai_summary"])
    if provider == "anthropic":
        from .anthropic_provider import AnthropicSummarizer

        return AnthropicSummarizer(_require_key("anthropic"), model or config["models"]["anthropic_summary"])
    raise ProviderError(f"Unknown summarizer provider: {provider!r} (expected 'openai' or 'anthropic').")


def build_transcriber(config: dict[str, Any], *, model: str | None = None) -> Transcriber:
    provider = config["providers"].get("transcriber", "openai")
    if provider == "openai":
        from .openai_provider import OpenAITranscriber

        return OpenAITranscriber(_require_key("openai"), model or config["models"]["transcribe"])
    raise ProviderError(
        f"Unknown/unsupported transcriber provider: {provider!r}. "
        f"Audio transcription currently requires OpenAI (local Whisper is on the roadmap)."
    )


def build_diarizer(
    config: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    device: str | None = None,
) -> Diarizer:
    provider = provider or config["providers"].get("diarizer", "pyannote")
    if provider == "pyannote":
        from .pyannote_provider import PyannoteDiarizer

        return PyannoteDiarizer(
            _require_key("hf"), model or config["models"]["diarization"], device=device
        )
    raise ProviderError(
        f"Unknown diarizer provider: {provider!r} (expected 'pyannote'). "
        f"Local speaker identification currently uses pyannote.audio."
    )
