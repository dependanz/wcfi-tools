"""Audio decode/encode helpers (16 kHz mono, via ffmpeg)."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 16000


def decode(path: Path, start: float = 0.0, dur: float | None = None) -> np.ndarray:
    """Decode any audio file to a float32 mono 16 kHz waveform in [-1, 1]."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def normalize(samples: np.ndarray, peak: float = 0.95, pct: float = 99.0) -> np.ndarray:
    """Boost a quiet clip to a consistent loudness. Scales the 99th-percentile amplitude to
    ``peak`` (robust to lone transients) and clips to [-1, 1]."""
    x = np.asarray(samples, dtype=np.float32)
    if not len(x):
        return x
    ref = float(np.percentile(np.abs(x), pct))
    if ref < 1e-4:
        ref = float(np.abs(x).max()) or 1.0
    return np.clip(x * (peak / ref), -1.0, 1.0).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, sr: int = SR) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
