"""Fetch and cache the ONNX models (non-gated public URLs)."""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_data_dir

_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
# (filename, url). The speaker-model release tag is misspelled "recongition" upstream.
SPEC = {
    "embedding": (
        "nemo_en_titanet_small.onnx",
        f"{_REL}/speaker-recongition-models/nemo_en_titanet_small.onnx",
    ),
    "vad": ("silero_vad.onnx", f"{_REL}/asr-models/silero_vad.onnx"),
}
_MIN_BYTES = 100_000


def models_dir() -> Path:
    path = Path(user_data_dir("wcfi", appauthor=False)) / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure(kind: str, *, log: Callable[[str], None] = print) -> Path:
    """Return the local path to a model, downloading it once if missing."""
    filename, url = SPEC[kind]
    path = models_dir() / filename
    if path.exists() and path.stat().st_size > _MIN_BYTES:
        return path
    log(f"  downloading {kind} model ({filename}) — one time only…")
    tmp = path.with_suffix(path.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https github release URL
    if tmp.stat().st_size < _MIN_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Download for the {kind} model looked wrong (too small): {url}")
    tmp.replace(path)
    return path
