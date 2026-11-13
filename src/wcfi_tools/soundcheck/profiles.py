"""Explicit channel roles and deliberately restrained starter settings."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

# Octave energy shapes at 125..8000 Hz. These are heuristics, not standards or
# measurements of a professional mix. Only excess energy is cut, never boosted.
ROLES = {
    "vocal": {"target_dbfs": -24.0, "highpass_hz": 80, "shape": [-18, -8, -2, 0, 0, -3, -10]},
    "speech": {"target_dbfs": -24.0, "highpass_hz": 80, "shape": [-18, -8, -2, 0, 0, -4, -12]},
    "electric_guitar": {"target_dbfs": -26.0, "highpass_hz": 70, "shape": [-12, -4, 0, 0, -2, -7, -18]},
    "acoustic_guitar": {"target_dbfs": -26.0, "highpass_hz": 70, "shape": [-10, -3, 0, 0, -2, -4, -10]},
    "keys": {"target_dbfs": -26.0, "highpass_hz": 35, "shape": [-4, -2, 0, 0, -2, -5, -10]},
    "bass": {"target_dbfs": -24.0, "highpass_hz": 30, "shape": [8, 5, 1, 0, -7, -15, -25]},
    "drums": {"target_dbfs": -26.0, "highpass_hz": 30, "shape": [0, -4, -3, 0, 1, 2, 0]},
    "generic": {"target_dbfs": -26.0, "highpass_hz": 0, "shape": None},
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: dict) -> None:
    """Exclusive creation: never overwrite a previous run or user configuration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write("\n")


def guess_role(name: str) -> tuple[str, bool]:
    key = name.upper()
    if any(word in key for word in ("AMBIENT", "TALKBACK", "LAPTOP")):
        return "generic", False
    if key in {"RED", "YELLOW", "GREEN"} or "VOCAL" in key:
        return "vocal", True
    if "LAPEL" in key or "PASTOR" in key:
        return "speech", True
    for word, role in (("EGUITAR", "electric_guitar"), ("AC GUITAR", "acoustic_guitar"),
                       ("KEY", "keys"), ("BASS", "bass"), ("KIT", "drums")):
        if word in key:
            return role, True
    return "generic", False


def number(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a number")  # noqa: TRY004 - configuration validation
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def load_profile(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("tracks"), list):
        raise ValueError("Not a version 1 soundcheck profile")
    if data.get("capture_kind") != "raw_pre_fx":
        raise ValueError("Use raw individual input recordings, not rendered/post-effects stems")
    if not isinstance(data.get("source_set"), str):
        raise ValueError("Profile needs source_set")  # noqa: TRY004 - configuration validation
    if data.get("eq_mode") not in {"highpass", "gentle_cuts"}:
        raise ValueError("eq_mode must be highpass or gentle_cuts")
    number(data.get("max_boost_db"), "max_boost_db", 0, 12)
    number(data.get("max_cut_db"), "max_cut_db", 0, 24)
    number(data.get("peak_ceiling_dbfs"), "peak_ceiling_dbfs", -24, -6)
    seen = set()
    for track in data["tracks"]:
        key = str(track.get("track_id"))
        if key in seen or not key.isdigit():
            raise ValueError("Track IDs must be unique nonnegative integers")
        seen.add(key)
        if track.get("role") not in ROLES or not isinstance(track.get("enabled"), bool):
            raise ValueError(f"Invalid role or enabled flag on track {key}")
        if not isinstance(track.get("name"), str) or not track["name"].strip():
            raise ValueError(f"Missing track name: {key}")
        if track.get("file") is not None and not isinstance(track["file"], str):
            raise ValueError(f"Invalid audio filename on {track['name']}")
    return data
