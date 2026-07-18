"""Turn a diarization result into named speakers.

The flow, in pure logic a front-end can drive:

1. :func:`propose_from_voiceprints` — match each anonymous cluster against the enrolled voiceprint
   DB and propose a name (confident if similarity clears the threshold).
2. A front-end (CLI walk-through or web app) shows representative snippets
   (:func:`select_representative_turns`) and lets a human confirm or correct the leftovers.
3. :func:`enroll_confirmed` — save the confirmed clusters' voiceprints so returning speakers are
   recognised automatically next time.
4. :func:`attribute_turns` — stamp the final names onto every turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..providers.base import DiarizationResult, SpeakerTurn
from .voiceprints import VoiceprintDB


@dataclass
class Proposal:
    """A suggested name for one anonymous cluster."""

    cluster: str
    name: str | None
    score: float
    source: str  # "voiceprint" | "none" (LLM-based proposals may set "llm" later)
    confident: bool


def propose_from_voiceprints(
    diar: DiarizationResult, db: VoiceprintDB, threshold: float
) -> dict[str, Proposal]:
    """Propose a name per cluster from the voiceprint DB. Never invents — unknown stays ``None``."""
    proposals: dict[str, Proposal] = {}
    for label in diar.labels():
        embedding = diar.embeddings.get(label)
        if not embedding:
            proposals[label] = Proposal(label, None, 0.0, "none", False)
            continue
        ranked = db.match(embedding)
        if ranked and ranked[0][1] > 0.0:
            name, score = ranked[0]
            proposals[label] = Proposal(label, name, score, "voiceprint", score >= threshold)
        else:
            proposals[label] = Proposal(label, None, 0.0, "none", False)
    return proposals


def select_representative_turns(
    turns: list[SpeakerTurn],
    *,
    per_speaker: int = 3,
    min_dur: float = 3.0,
) -> dict[str, list[SpeakerTurn]]:
    """Pick a few clear snippets per cluster to play to the human — longest turns first,
    preferring those at least ``min_dur`` seconds long, falling back to whatever exists."""
    by_speaker: dict[str, list[SpeakerTurn]] = {}
    for turn in turns:
        by_speaker.setdefault(turn.speaker, []).append(turn)

    chosen: dict[str, list[SpeakerTurn]] = {}
    for speaker, speaker_turns in by_speaker.items():
        ranked = sorted(speaker_turns, key=lambda t: t.duration, reverse=True)
        long_enough = [t for t in ranked if t.duration >= min_dur]
        picks = (long_enough or ranked)[:per_speaker]
        chosen[speaker] = sorted(picks, key=lambda t: t.start)
    return chosen


def attribute_turns(
    turns: list[SpeakerTurn], mapping: dict[str, str]
) -> list[tuple[float, float, str]]:
    """Apply confirmed cluster->name assignments, returning ``(start, end, name)`` tuples.

    Clusters left unnamed keep their anonymous label so nothing is silently dropped.
    """
    return [(t.start, t.end, mapping.get(t.speaker, t.speaker)) for t in turns]


def enroll_confirmed(
    db: VoiceprintDB,
    diar: DiarizationResult,
    mapping: dict[str, str],
    *,
    max_samples: int = 8,
) -> int:
    """Store the voiceprint of every confirmed (named) cluster. Returns how many were enrolled."""
    enrolled = 0
    for cluster, name in mapping.items():
        name = (name or "").strip()
        embedding = diar.embeddings.get(cluster)
        if not name or not embedding:
            continue
        db.enroll(name, embedding, max_samples=max_samples)
        enrolled += 1
    return enrolled
