"""ffmpeg / ffprobe helpers for probing and chunking audio."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class ToolNotFound(RuntimeError):
    pass


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ToolNotFound(
            f"Required tool not found on PATH: {name}. Install ffmpeg (which provides "
            f"ffmpeg and ffprobe) — see the README for platform instructions."
        )


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned an invalid duration for {path}: {result.stdout}") from exc


def extract_chunk(source: Path, dest: Path, start_seconds: float, duration_seconds: float) -> None:
    """Write a mono 16kHz PCM WAV chunk — small and well under transcription upload limits."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_seconds:.3f}",
        "-i", str(source),
        "-t", f"{duration_seconds:.3f}",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {source} at {start_seconds:.1f}s: {result.stderr.strip()}")
    if not dest.exists() or dest.stat().st_size <= 1024:
        raise RuntimeError(f"ffmpeg produced an empty chunk: {dest}")
