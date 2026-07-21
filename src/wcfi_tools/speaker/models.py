"""Fetch and cache the ONNX models (non-gated public URLs, from k2-fsa's GitHub releases)."""

from __future__ import annotations

import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_data_dir

_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
# Each entry: local path under models_dir() + download URL. The speaker-model release tag is
# misspelled "recongition" upstream. The segmentation model ships as a .tar.bz2 we unpack once.
SPEC: dict[str, dict[str, str]] = {
    "embedding": {
        "path": "nemo_en_titanet_small.onnx",
        "url": f"{_REL}/speaker-recongition-models/nemo_en_titanet_small.onnx",
    },
    "segmentation": {
        "path": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
        "url": f"{_REL}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        "archive": "tar.bz2",
    },
}
_MIN_BYTES = 100_000


def models_dir() -> Path:
    path = Path(user_data_dir("wcfi", appauthor=False)) / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size > _MIN_BYTES


def ensure(kind: str, *, log: Callable[[str], None] = print) -> Path:
    """Return the local path to a model, downloading (and unpacking) it once if missing."""
    spec = SPEC[kind]
    path = models_dir() / spec["path"]
    if _ok(path):
        return path
    log(f"  downloading {kind} model — one time only…")
    if spec.get("archive") == "tar.bz2":
        tmp = models_dir() / (spec["path"].split("/")[0] + ".tar.bz2.part")
        urllib.request.urlretrieve(spec["url"], tmp)  # noqa: S310 - fixed https github release URL
        with tarfile.open(tmp, "r:bz2") as tar:
            tar.extractall(models_dir(), filter="data")  # noqa: S202 - trusted k2-fsa release, safe filter
        tmp.unlink(missing_ok=True)
        if not _ok(path):
            raise RuntimeError(f"The {kind} model was missing after unpacking {spec['url']}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    urllib.request.urlretrieve(spec["url"], tmp)  # noqa: S310 - fixed https github release URL
    if tmp.stat().st_size < _MIN_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Download for the {kind} model looked wrong (too small): {spec['url']}")
    tmp.replace(path)
    return path
