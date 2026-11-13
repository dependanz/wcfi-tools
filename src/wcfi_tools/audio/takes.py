"""Local recorded-take inventory and bounded, sample-synchronous disk playback."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import numpy as np
import soundfile as sf

from .asio import CHANNELS, RATE

MIN_LOOP = RATE // 10


def library_directory():
    override = os.environ.get("WCFI_EMULATOR_SOURCE_ROOT")
    return Path(override) if override else Path(__file__).resolve().parents[3] / "real-sources"


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer between {low} and {high}")
    return value


def _manifest(root: Path, take_id):
    if not isinstance(take_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", take_id):
        raise ValueError("Choose a recorded take from the local library")
    root = root.resolve()
    folder = (root / take_id).resolve()
    path = (folder / "manifest.json").resolve()
    if not folder.is_relative_to(root) or not path.is_relative_to(folder):
        raise ValueError("Recorded take must stay inside its library folder")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Recorded take manifest is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("kind") != "wcfi_real_source_take"
            or data.get("schema_version") != 1 or data.get("take_id") != take_id):
        raise ValueError("Not a version 1 recorded take manifest")
    _integer(data.get("sample_rate"), "Take sample rate", RATE, RATE)
    _integer(data.get("frames"), "Take frames", MIN_LOOP, RATE * 86400)
    sources = data.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= CHANNELS:
        raise ValueError("A take needs 1–32 source files")
    ports = set()
    for source in sources:
        if not isinstance(source, dict):
            raise TypeError("Invalid recorded source")
        name = source.get("track_name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Use source names of 1–100 characters")
        count = _integer(source.get("channels"), "Source channels", 1, 2)
        _integer(source.get("frames"), "Source frames", data["frames"], data["frames"])
        _integer(source.get("sample_rate"), "Source sample rate", RATE, RATE)
        mapping = source.get("emulator_inputs")
        if not isinstance(mapping, list) or len(mapping) != count:
            raise ValueError(f"{name}: map one input per file channel")
        for port in mapping:
            _integer(port, "Input", 1, CHANNELS)
            if port in ports:
                raise ValueError(f"Input {port} is mapped more than once")
            ports.add(port)
        relative = source.get("file")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("Recorded source paths must be relative to the take")
        audio = (folder / relative).resolve()
        if not audio.is_relative_to(folder):
            raise ValueError("Recorded source must stay inside its take folder")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", source.get("sha256", "")):
            raise ValueError(f"{name}: a SHA-256 checksum is required")
        if any(source.get(k, 0) != 0 for k in ("source_loop_start_beat", "source_start_relative_beat")):
            raise ValueError("This take format requires source files aligned at frame zero")
    for field in ("arrangement_start_beat", "arrangement_end_beat"):
        values = [s.get(field) for s in sources]
        if any(v != values[0] for v in values):
            raise ValueError("Source arrangement positions do not match")
    return folder, data


def _summary(data):
    title = data.get("title", data["take_id"])
    if not isinstance(title, str) or not 1 <= len(title) <= 160:
        title = data["take_id"]
    return {"id": data["take_id"], "title": title, "frames": data["frames"],
            "sample_rate": RATE, "duration_seconds": data["frames"] / RATE,
            "source_count": len(data["sources"]),
            "input_channel_count": sum(s["channels"] for s in data["sources"])}


def catalog(root: Path):
    """Read small inventory files; opening/hash-checking audio happens on load."""
    result = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            _, data = _manifest(root, path.parent.name)
            result.append({**_summary(data), "error": None})
        except (OSError, ValueError, TypeError) as exc:
            result.append({"id": path.parent.name, "title": path.parent.name, "error": str(exc)})
    return result


class RecordedTake:
    def __init__(self, root: Path, take_id, *, start_frame=0, end_frame=None, fade_ms=5):
        self.folder, self.data = _manifest(root, take_id)
        self.files = []
        self.ports = {p for s in self.data["sources"] for p in s["emulator_inputs"]}
        self.start_frame, self.end_frame, self.fade_ms = self.validate_range(
            start_frame, self.data["frames"] if end_frame is None else end_frame, fade_ms)
        self.cursor = self.start_frame
        try:
            for source in self.data["sources"]:
                path = (self.folder / source["file"]).resolve()
                before = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest().lower() != source["sha256"].lower():
                    raise ValueError(f"{source['track_name']}: source checksum changed")
                audio = sf.SoundFile(path, mode="r")
                self.files.append((audio, source))
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("A recorded source changed while loading")
                if (audio.frames != self.data["frames"] or audio.samplerate != RATE
                        or audio.channels != source["channels"]
                        or audio.subtype != source.get("subtype", audio.subtype)):
                    raise ValueError(f"{source['track_name']}: source format or length does not match")
        except Exception:
            self.close()
            raise

    def validate_range(self, start_frame, end_frame, fade_ms):
        start_frame = _integer(start_frame, "Loop start", 0, self.data["frames"] - MIN_LOOP)
        end_frame = _integer(end_frame, "Loop end", start_frame + MIN_LOOP, self.data["frames"])
        fade_ms = _integer(fade_ms, "Boundary fade milliseconds", 0, 20)
        return start_frame, end_frame, fade_ms

    def set_range(self, start_frame, end_frame, fade_ms):
        self.start_frame, self.end_frame, self.fade_ms = self.validate_range(start_frame, end_frame, fade_ms)
        self.cursor = self.start_frame

    def settings(self):
        return {"id": self.data["take_id"], "start_frame": self.start_frame,
                "end_frame": self.end_frame, "fade_ms": self.fade_ms}

    def snapshot(self):
        return {**_summary(self.data), **self.settings(), "position_frame": self.cursor,
                "mapped_inputs": sorted(self.ports), "checksums_verified": True}

    def read(self, frames):
        """One cursor for all files, including muted/disabled ports. Bounded memory."""
        _integer(frames, "Read frames", 1, 65536)
        block = np.zeros((frames, CHANNELS), dtype=np.float32)
        written = 0
        cursor = self.cursor
        while written < frames:
            count = min(frames - written, self.end_frame - cursor)
            for audio, source in self.files:
                if audio.tell() != cursor:
                    audio.seek(cursor)
                samples = audio.read(count, dtype="float32", always_2d=True)
                if len(samples) != count or not np.all(np.isfinite(samples)):
                    raise RuntimeError(f"{source['track_name']}: incomplete or invalid recorded audio")
                block[written:written + count, np.array(source["emulator_inputs"]) - 1] = samples
            if self.fade_ms:
                # Same sample-indexed envelope on every port; no overlap or time stretching.
                positions = np.arange(cursor, cursor + count)
                fade = min(round(self.fade_ms * RATE / 1000), (self.end_frame - self.start_frame) // 2)
                envelope = np.minimum(1, np.minimum(positions - self.start_frame,
                    self.end_frame - 1 - positions) / max(1, fade)).astype(np.float32)
                block[written:written + count] *= envelope[:, None]
            written += count
            cursor += count
            if cursor == self.end_frame:
                cursor = self.start_frame
        self.cursor = cursor
        return block

    def close(self):
        for audio, _ in self.files:
            audio.close()
        self.files.clear()
