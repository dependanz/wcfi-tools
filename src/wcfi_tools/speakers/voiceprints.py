"""A small local database of enrolled speaker voiceprints.

Each person maps to one or more embedding vectors (voiceprints) captured from past meetings.
Matching a new, anonymous cluster is a nearest-neighbour lookup by cosine similarity against every
stored sample. The store is a single JSON file in the platform config dir.

NOTE: voiceprints are biometric data. They never leave the machine, and `wcfi speakers forget`
deletes them. Disclose their use to the people being recorded.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .. import config as cfg

DB_VERSION = 1
MAX_SAMPLES_DEFAULT = 8


def db_path() -> Path:
    return cfg.config_dir() / "speakers" / "voiceprints.json"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 for empty or zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VoiceprintDB:
    """Load/modify/save enrolled speaker voiceprints.

    In-memory shape: ``{name: {"samples": [[float, ...], ...], "meta": {...}}}``.
    """

    def __init__(self, speakers: dict[str, dict[str, Any]] | None = None, *, path: Path | None = None):
        self._speakers: dict[str, dict[str, Any]] = speakers or {}
        self._path = path

    # --- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "VoiceprintDB":
        path = path or db_path()
        if not path.exists():
            return cls({}, path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls({}, path=path)
        speakers = data.get("speakers", {}) if isinstance(data, dict) else {}
        return cls(dict(speakers), path=path)

    def save(self, path: Path | None = None) -> Path:
        path = path or self._path or db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": DB_VERSION, "speakers": self._speakers}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._path = path
        return path

    # --- queries -------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._speakers)

    def sample_count(self, name: str) -> int:
        return len(self._speakers.get(name, {}).get("samples", []))

    def __contains__(self, name: str) -> bool:
        return name in self._speakers

    def __len__(self) -> int:
        return len(self._speakers)

    def match(self, embedding: list[float]) -> list[tuple[str, float]]:
        """Ranked ``(name, best_similarity)`` for every enrolled speaker, most similar first.

        A speaker's score is the *max* similarity across their stored samples — robust to a person
        sounding different across recordings (phone vs. in-room, etc.).
        """
        scores: list[tuple[str, float]] = []
        for name, record in self._speakers.items():
            best = max(
                (cosine_similarity(embedding, sample) for sample in record.get("samples", [])),
                default=0.0,
            )
            scores.append((name, best))
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores

    def best_match(self, embedding: list[float], threshold: float) -> tuple[str, float] | None:
        ranked = self.match(embedding)
        if ranked and ranked[0][1] >= threshold:
            return ranked[0]
        return None

    # --- mutations -----------------------------------------------------------

    def enroll(self, name: str, embedding: list[float], *, max_samples: int = MAX_SAMPLES_DEFAULT) -> None:
        """Add a voiceprint sample for ``name``, keeping at most ``max_samples`` (most recent)."""
        if not embedding:
            return
        record = self._speakers.setdefault(name, {"samples": [], "meta": {}})
        samples: list[list[float]] = record.setdefault("samples", [])
        samples.append([float(x) for x in embedding])
        if max_samples > 0 and len(samples) > max_samples:
            del samples[: len(samples) - max_samples]
        record.setdefault("meta", {})["samples"] = len(samples)

    def forget(self, name: str) -> bool:
        return self._speakers.pop(name, None) is not None

    def rename(self, old: str, new: str) -> bool:
        if old not in self._speakers or old == new:
            return False
        record = self._speakers.pop(old)
        if new in self._speakers:
            self._speakers[new].setdefault("samples", []).extend(record.get("samples", []))
        else:
            self._speakers[new] = record
        return True

    def clear(self) -> None:
        self._speakers.clear()
