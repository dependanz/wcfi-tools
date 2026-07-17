"""Local voiceprint store — one JSON per registered speaker in the user data dir.

Biometric-ish data: kept on this machine only, never uploaded.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import numpy as np
from platformdirs import user_data_dir


def store_dir() -> Path:
    path = Path(user_data_dir("wcfi", appauthor=False)) / "speakers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "speaker"


def list_speakers() -> list[dict]:
    rows = []
    for p in sorted(store_dir().glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        rows.append({"name": d["name"], "count": d.get("count", 0), "updated": d.get("updated", "")})
    return rows


def load() -> dict[str, np.ndarray]:
    """Return {name: centroid voiceprint}."""
    prints: dict[str, np.ndarray] = {}
    for p in sorted(store_dir().glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        prints[d["name"]] = np.asarray(d["centroid"], dtype=np.float32)
    return prints


def upsert(name: str, embeddings: list[np.ndarray]) -> None:
    """Add example embeddings for ``name``, updating its running centroid."""
    embeddings = [np.asarray(e, dtype=np.float32) for e in embeddings]
    if not embeddings:
        return
    path = store_dir() / f"{_slug(name)}.json"
    if path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        prev_count = int(d.get("count", 0))
        vec = np.asarray(d["centroid"], dtype=np.float32) * prev_count + sum(embeddings)
        count = prev_count + len(embeddings)
    else:
        vec = sum(embeddings)
        count = len(embeddings)
    norm = float(np.linalg.norm(vec))
    centroid = (vec / norm if norm else vec).astype(np.float32)
    path.write_text(
        json.dumps(
            {"name": name, "count": count, "updated": time.strftime("%Y-%m-%d"),
             "centroid": centroid.tolist()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def remove(name: str) -> bool:
    path = store_dir() / f"{_slug(name)}.json"
    if path.exists():
        path.unlink()
        return True
    return False
