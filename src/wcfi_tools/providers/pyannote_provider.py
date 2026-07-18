"""Local speaker diarization via `pyannote.audio`.

Heavyweight dependencies (torch, pyannote.audio, huggingface_hub) are imported lazily so the base
package stays light and importable without them. Install with ``pip install 'wcfi-tools[speakers]'``.

pyannote's pretrained pipelines are *gated* on Hugging Face: a token alone is not enough — a human
must accept each model's license once. :func:`check_model_access` reports exactly which repos still
need that one-time click so `wcfi setup` can guide the user through it.
"""

from __future__ import annotations

from pathlib import Path

from .base import DiarizationResult, SpeakerTurn

# Gated Hugging Face repos each diarization pipeline pulls in. Accepting these once (per HF account)
# is required before the model can be downloaded.
GATED_REPOS: dict[str, list[str]] = {
    "pyannote/speaker-diarization-3.1": [
        "pyannote/speaker-diarization-3.1",
        "pyannote/segmentation-3.0",
    ],
    "pyannote/speaker-diarization-community-1": [
        "pyannote/speaker-diarization-community-1",
    ],
}


class DiarizerUnavailable(RuntimeError):
    """Raised when pyannote cannot load — missing deps, bad token, or unaccepted model license."""


def required_repos(model: str) -> list[str]:
    return GATED_REPOS.get(model, [model])


def check_model_access(hf_token: str | None, model: str) -> tuple[bool, list[str]]:
    """Return ``(ok, missing_urls)`` for the gated repos a model needs.

    ``missing_urls`` are the model pages whose license still has to be accepted. Raises
    ``ModuleNotFoundError`` if huggingface_hub is not installed. Network/other errors propagate.
    """
    from huggingface_hub import HfApi
    from huggingface_hub.utils import GatedRepoError

    api = HfApi(token=hf_token)
    missing: list[str] = []
    for repo in required_repos(model):
        try:
            api.model_info(repo)
        except GatedRepoError:
            missing.append(f"https://huggingface.co/{repo}")
    return (not missing, missing)


class PyannoteDiarizer:
    """Diarizer backed by a pyannote pretrained pipeline (loaded on first use)."""

    name = "pyannote"

    def __init__(
        self,
        hf_token: str,
        model: str = "pyannote/speaker-diarization-3.1",
        *,
        device: str | None = None,
    ) -> None:
        self.hf_token = hf_token
        self.model = model
        self.device = device
        self._pipeline = None

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        try:
            import torch
            from pyannote.audio import Pipeline
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
            raise DiarizerUnavailable(
                "Speaker identification needs the optional 'speakers' extra. "
                "Install it with: pip install 'wcfi-tools[speakers]'"
            ) from exc

        try:
            pipeline = Pipeline.from_pretrained(self.model, use_auth_token=self.hf_token)
        except Exception as exc:  # noqa: BLE001 - surface any load failure with guidance
            raise DiarizerUnavailable(_load_hint(self.model, exc)) from exc
        # pyannote returns None (rather than raising) when the token cannot access a gated repo.
        if pipeline is None:
            raise DiarizerUnavailable(_load_hint(self.model, None))

        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        pipeline.to(torch.device(device))
        self._pipeline = pipeline
        return pipeline

    def diarize(self, audio_path: Path) -> DiarizationResult:
        pipeline = self._load()
        # ``return_embeddings=True`` yields one centroid voiceprint per speaker, row-aligned with
        # ``diarization.labels()``.
        diarization, embeddings = pipeline(str(audio_path), return_embeddings=True)

        turns = [
            SpeakerTurn(round(float(turn.start), 3), round(float(turn.end), 3), str(speaker))
            for turn, _, speaker in diarization.itertracks(yield_label=True)
        ]

        emb: dict[str, list[float]] = {}
        if embeddings is not None:
            for idx, label in enumerate(diarization.labels()):
                try:
                    vector = embeddings[idx]
                except (IndexError, TypeError):  # pragma: no cover - defensive
                    continue
                values = [float(x) for x in vector]
                if not values or any(v != v for v in values):  # skip all-NaN rows
                    continue
                emb[str(label)] = values

        return DiarizationResult(turns=turns, embeddings=emb)


def _load_hint(model: str, exc: Exception | None) -> str:
    urls = "\n  ".join(f"https://huggingface.co/{repo}" for repo in required_repos(model))
    detail = f" ({exc})" if exc else ""
    return (
        f"Could not load pyannote model '{model}'{detail}. Check that your Hugging Face token is "
        f"valid and that you have accepted the license (one-time) for each of:\n  {urls}\n"
        "Run `wcfi setup` to (re)configure the token and see which licenses still need accepting."
    )
