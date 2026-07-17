"""Provider factory — build a Summarizer/Transcriber from config + stored secrets."""

from __future__ import annotations

from typing import Any

from .. import config as cfg
from .base import Summarizer, Transcriber
from .diarize import Diarizer

__all__ = [
    "Summarizer", "Transcriber", "Diarizer",
    "build_summarizer", "build_transcriber", "build_diarizer", "ProviderError",
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


def build_diarizer(config: dict[str, Any], *, backend: str | None = None, model: str | None = None) -> Diarizer:
    backend = backend or config["providers"].get("diarizer", "pyannote")
    if backend == "window":
        from .diarize import WindowDiarizer

        return WindowDiarizer()
    if backend == "pyannote":
        from .diarize import PyannoteDiarizer

        token = cfg.get_secret("huggingface")
        if not token:
            raise ProviderError(
                "Speaker diarization needs a HuggingFace token. Run `wcfi setup`, or set HF_TOKEN. "
                "You must also accept the conditions for pyannote/speaker-diarization-3.1 on HuggingFace."
            )
        return PyannoteDiarizer(token, model or config["models"]["diarize"])
    raise ProviderError(f"Unknown diarizer backend: {backend!r} (expected 'pyannote' or 'window').")
