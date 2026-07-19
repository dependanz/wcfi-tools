"""Shared speaker-identification core, used by both `wcfi meeting identify` and `summarize`.

UI-agnostic: the human interaction (playing snippets, asking "who is speaking?") is injected as a
``resolver`` callback, so the same logic backs the CLI walk-through today and a web front-end later.

Per audio file it: diarizes (cached), reuses any names resolved on a previous run, proposes names
from enrolled voiceprints, asks the resolver only for the still-unknown clusters, enrolls the newly
confirmed voices, and persists the cluster->name map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..providers.base import DiarizationResult, Diarizer, SpeakerTurn
from ..speakers.identify import (
    Proposal,
    enroll_confirmed,
    propose_from_voiceprints,
    select_representative_turns,
)
from ..speakers.voiceprints import VoiceprintDB


@dataclass
class ClusterPrompt:
    """Everything a front-end needs to decide who an anonymous cluster is."""

    audio_name: str
    source_path: Path
    cluster: str
    proposal: Proposal | None
    turns: list[SpeakerTurn]


# A front-end returns a confirmed name for the cluster, or None to leave it anonymous.
ClusterResolver = Callable[[ClusterPrompt], "str | None"]


def resolve_meeting_speakers(
    audio_files: list[Path],
    *,
    diarizer: Diarizer,
    db: VoiceprintDB,
    work_dir: Path,
    resolver: ClusterResolver,
    threshold: float = 0.65,
    max_samples: int = 8,
    force: bool = False,
    snippets_per_speaker: int = 3,
    log: Callable[[str], None] | None = None,
) -> tuple[dict[str, DiarizationResult], dict[str, dict[str, str]]]:
    """Diarize + name every file. Returns ``(diar_by_file, names_by_file)`` keyed by str(path)."""
    diar_dir = work_dir / "diarization"
    diar_dir.mkdir(parents=True, exist_ok=True)

    diar_by_file: dict[str, DiarizationResult] = {}
    names_by_file: dict[str, dict[str, str]] = {}

    for audio in audio_files:
        raw_path = diar_dir / f"{audio.stem}.raw.json"
        names_path = diar_dir / f"{audio.stem}.names.json"

        if raw_path.exists() and not force:
            _emit(log, f"[bold]Diarization cached[/] for {audio.name} [dim](--force to redo)[/]")
            diar = DiarizationResult.from_dict(json.loads(raw_path.read_text(encoding="utf-8")))
        else:
            _emit(log, f"[bold]Diarizing[/] {audio.name} … [dim](one-time; slow on CPU)[/]")
            diar = diarizer.diarize(audio)
            raw_path.write_text(
                json.dumps(diar.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        if not diar.turns:
            _emit(log, f"  [yellow]No speech detected in {audio.name}; skipping.[/]")
            diar_by_file[str(audio)] = diar
            names_by_file[str(audio)] = {}
            continue

        preassigned = _load_names(names_path) if not force else {}
        proposals = propose_from_voiceprints(diar, db, threshold)
        snippets = select_representative_turns(diar.turns, per_speaker=snippets_per_speaker)

        mapping: dict[str, str] = dict(preassigned)
        newly: dict[str, str] = {}
        for cluster in diar.labels():
            if cluster in mapping:
                continue
            ctx = ClusterPrompt(audio.name, audio, cluster, proposals.get(cluster), snippets.get(cluster, []))
            name = resolver(ctx)
            if name and name.strip():
                mapping[cluster] = name.strip()
                newly[cluster] = name.strip()

        enroll_confirmed(db, diar, newly, max_samples=max_samples)
        names_path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _emit(log, f"  [green]{audio.name}[/]: {len(newly)} new voice(s) enrolled; {len(mapping)} named.")

        diar_by_file[str(audio)] = diar
        names_by_file[str(audio)] = mapping

    db.save()
    return diar_by_file, names_by_file


def _emit(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)


def auto_resolver(ctx: ClusterPrompt) -> str | None:
    """Non-interactive policy: accept a confident voiceprint match, otherwise stay anonymous."""
    if ctx.proposal and ctx.proposal.name and ctx.proposal.confident:
        return ctx.proposal.name
    return None


def _load_names(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
