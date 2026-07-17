"""Turn a diarization into per-speaker audio snippets and a name roster.

Flow: diarize -> pick a few clear snippets per speaker cluster -> a human labels each cluster
(via the web annotator) -> the resulting {cluster: name} roster is injected into summarization.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core import ffmpeg
from ..providers.diarize import DiarTurn


def speaker_durations(turns: list[DiarTurn]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for turn in turns:
        totals[turn.speaker] = totals.get(turn.speaker, 0.0) + turn.duration
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def select_snippets(
    audio_path: Path,
    turns: list[DiarTurn],
    work_dir: Path,
    *,
    per_speaker: int = 3,
    min_len: float = 2.5,
    max_len: float = 8.0,
) -> dict[str, list[Path]]:
    """Cut a few representative clips per speaker into ``work_dir/snippets``.

    Picks each speaker's longest turns (clear, solo speech), trimming long turns to their
    middle ``max_len`` seconds. Returns {speaker: [clip paths]} ordered by total talk time.
    """
    snippet_dir = work_dir / "snippets"
    snippet_dir.mkdir(parents=True, exist_ok=True)

    by_speaker: dict[str, list[DiarTurn]] = {}
    for turn in turns:
        if turn.duration >= min_len:
            by_speaker.setdefault(turn.speaker, []).append(turn)

    ordered = sorted(
        by_speaker.items(),
        key=lambda kv: sum(t.duration for t in kv[1]),
        reverse=True,
    )

    result: dict[str, list[Path]] = {}
    for speaker, speaker_turns in ordered:
        speaker_turns.sort(key=lambda t: t.duration, reverse=True)
        clips: list[Path] = []
        for idx, turn in enumerate(speaker_turns[:per_speaker]):
            length = min(turn.duration, max_len)
            start = turn.start + max(0.0, (turn.duration - length) / 2)
            dest = snippet_dir / f"{_slug(speaker)}_{idx}.wav"
            ffmpeg.extract_chunk(audio_path, dest, start, length)
            clips.append(dest)
        if clips:
            result[speaker] = clips
    return result


def roster_context(roster: dict[str, str]) -> str:
    """Build a prompt-injection note naming identified speakers (skips 'Unsure'/blank)."""
    named = [name for name in roster.values() if name and name.strip().lower() != "unsure"]
    if not named:
        return ""
    unique = sorted(dict.fromkeys(n.strip() for n in named))
    return (
        "Known people present in this meeting (from verified speaker identification) — use these "
        "for the Attendance section and to attribute movers/seconders when the transcript supports "
        f"it; do not invent attributions: {', '.join(unique)}."
    )


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "speaker"
