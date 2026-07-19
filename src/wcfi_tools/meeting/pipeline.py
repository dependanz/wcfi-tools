"""Meeting summarization pipeline: chunk -> transcribe -> atomic facts -> consolidate -> minutes.

UI-agnostic and resumable. Front-ends (CLI/web/desktop) call :func:`summarize_meeting` and may
pass an ``on_progress(stage, current, total)`` callback to drive a progress bar.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..core import ffmpeg
from ..providers.base import DiarizationResult, Diarizer, SpeakerTurn, Summarizer, Transcriber
from ..speakers.voiceprints import VoiceprintDB
from . import artifacts, schemas
from .speaker_id import ClusterResolver, resolve_meeting_speakers

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".mp4", ".mpeg", ".mpga", ".webm"}
WORK_DIR_NAME = "_work"
# Directories whose contents are pipeline artifacts, never source audio. Includes the legacy
# "_minutes_work" name used by the original script so old caches aren't picked up as inputs.
WORK_DIR_NAMES = {"_work", "_minutes_work"}
# Consecutive turns by the same speaker up to this gap (seconds) merge into one transcription unit.
SPEAKER_MERGE_GAP_SECONDS = 2.0
Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    sequence: int
    source_audio: str
    source_audio_rel: str
    chunk_path: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    speaker: str | None = None  # resolved speaker name (or anonymous label) when identified


@dataclass(frozen=True)
class _Segment:
    start: float
    end: float
    name: str


@dataclass
class PipelineResult:
    meeting_dir: Path
    meeting_date: str
    work_dir: Path
    minutes_path: Path
    minutes_markdown: str
    atomic_summary: dict[str, Any]
    artifacts: dict[str, Path] = field(default_factory=dict)


def summarize_meeting(
    meeting_dir: Path,
    *,
    summarizer: Summarizer,
    transcriber: Transcriber,
    prepared_by: str = "Danzel Serrano",
    location: str = "44 Van Dyke Rd",
    meeting_date: str | None = None,
    meeting_time: str = "Not explicitly captured",
    chunk_minutes: float = 5.0,
    reasoning_effort: str = "low",
    emit: tuple[str, ...] = ("md", "paste"),
    force: bool = False,
    on_progress: Progress | None = None,
    identify_speakers: bool = False,
    diarizer: Diarizer | None = None,
    speaker_resolver: ClusterResolver | None = None,
    speaker_threshold: float = 0.65,
    speaker_max_samples: int = 8,
    voiceprint_db: VoiceprintDB | None = None,
) -> PipelineResult:
    meeting_dir = meeting_dir.resolve()
    if not meeting_dir.is_dir():
        raise NotADirectoryError(f"Meeting folder does not exist: {meeting_dir}")

    ffmpeg.require_tool("ffmpeg")
    ffmpeg.require_tool("ffprobe")

    meeting_date = meeting_date or derive_meeting_date(meeting_dir)
    work_dir = meeting_dir / WORK_DIR_NAME
    work_dir.mkdir(parents=True, exist_ok=True)

    audio_files = discover_audio(meeting_dir)
    if not audio_files:
        raise FileNotFoundError("No audio files were found in the meeting folder.")

    if identify_speakers and diarizer is not None and speaker_resolver is not None:
        db = voiceprint_db or VoiceprintDB.load()
        diar_by_file, names_by_file = resolve_meeting_speakers(
            audio_files, diarizer=diarizer, db=db, work_dir=work_dir,
            resolver=speaker_resolver, threshold=speaker_threshold,
            max_samples=speaker_max_samples, force=force,
        )
        chunks = build_speaker_chunk_records(
            meeting_dir, audio_files, diar_by_file, names_by_file, work_dir, chunk_minutes
        )
    else:
        chunks = build_chunk_records(meeting_dir, audio_files, work_dir, chunk_minutes, on_progress)
    _write_json(work_dir / "chunks_manifest.json", [asdict(c) for c in chunks])
    ensure_audio_chunks(chunks, force, on_progress)

    transcript_items = transcribe_chunks(chunks, transcriber, work_dir, force, on_progress)
    _write_text(work_dir / "transcript.md", _render_transcript_md(transcript_items))

    chunk_atomics = extract_chunk_atomics(
        chunks, transcript_items, summarizer, work_dir, reasoning_effort, force, on_progress
    )
    final_atomic = consolidate(chunk_atomics, meeting_date, summarizer, work_dir, reasoning_effort, force)
    _write_json(work_dir / "atomic_summary.json", final_atomic)
    _write_text(work_dir / "atomic_summary.md", artifacts.render_atomic_markdown(final_atomic, schemas.ATOMIC_CATEGORIES))

    minutes_md = generate_minutes(
        final_atomic, meeting_dir, audio_files, meeting_date, meeting_time,
        location, prepared_by, summarizer, reasoning_effort,
    )

    result = PipelineResult(
        meeting_dir=meeting_dir,
        meeting_date=meeting_date,
        work_dir=work_dir,
        minutes_path=meeting_dir / "minutes.md",
        minutes_markdown=minutes_md,
        atomic_summary=final_atomic,
    )

    if "md" in emit or "all" in emit:
        _write_text(result.minutes_path, minutes_md.rstrip() + "\n")
        result.artifacts["minutes.md"] = result.minutes_path
    if "paste" in emit or "all" in emit:
        paste_path = meeting_dir / "paste-block.txt"
        _write_text(paste_path, artifacts.markdown_to_paste_block(minutes_md))
        result.artifacts["paste-block.txt"] = paste_path
    return result


# --- stages ------------------------------------------------------------------


def discover_audio(meeting_dir: Path) -> list[Path]:
    files = [
        p for p in meeting_dir.rglob("*")
        if p.is_file()
        and not (set(p.parts) & WORK_DIR_NAMES)
        and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    files.sort(key=lambda item: _natural_key(_relposix(item, meeting_dir)))
    return files


def build_chunk_records(
    meeting_dir: Path, audio_files: list[Path], work_dir: Path, chunk_minutes: float,
    on_progress: Progress | None,
) -> list[ChunkRecord]:
    chunk_seconds = chunk_minutes * 60.0
    chunk_dir = work_dir / "chunks"
    records: list[ChunkRecord] = []
    sequence = 1
    for idx, audio in enumerate(audio_files, 1):
        _emit(on_progress, "probe", idx, len(audio_files))
        duration = ffmpeg.ffprobe_duration(audio)
        rel = _relposix(audio, meeting_dir)
        slug = _slugify(Path(rel).with_suffix("").as_posix())
        for c in range(math.ceil(duration / chunk_seconds)):
            start = c * chunk_seconds
            end = min(start + chunk_seconds, duration)
            chunk_id = f"{sequence:04d}_{slug}"
            records.append(ChunkRecord(
                chunk_id=chunk_id, sequence=sequence, source_audio=str(audio),
                source_audio_rel=rel, chunk_path=str(chunk_dir / f"{chunk_id}.wav"),
                start_seconds=round(start, 3), end_seconds=round(end, 3),
                duration_seconds=round(end - start, 3),
            ))
            sequence += 1
    return records


def build_speaker_chunk_records(
    meeting_dir: Path,
    audio_files: list[Path],
    diar_by_file: dict[str, DiarizationResult],
    names_by_file: dict[str, dict[str, str]],
    work_dir: Path,
    chunk_minutes: float,
) -> list[ChunkRecord]:
    """Chunk each file by speaker turn (merged + capped) so every chunk is one named speaker.

    Files with no diarization fall back to fixed-time chunks (speaker unknown), so no audio is lost.
    """
    chunk_seconds = chunk_minutes * 60.0
    chunk_dir = work_dir / "chunks"
    records: list[ChunkRecord] = []
    sequence = 1
    for audio in audio_files:
        rel = _relposix(audio, meeting_dir)
        slug = _slugify(Path(rel).with_suffix("").as_posix())
        diar = diar_by_file.get(str(audio))
        names = names_by_file.get(str(audio), {})
        segments = _merge_turns_to_segments(diar.turns if diar else [], names, chunk_seconds)

        if not segments:  # no diarization for this file — keep it, just unattributed
            duration = ffmpeg.ffprobe_duration(audio)
            for c in range(math.ceil(duration / chunk_seconds)):
                start = c * chunk_seconds
                end = min(start + chunk_seconds, duration)
                segments.append(_Segment(round(start, 3), round(end, 3), ""))

        for seg in segments:
            chunk_id = f"{sequence:04d}_{slug}"
            records.append(ChunkRecord(
                chunk_id=chunk_id, sequence=sequence, source_audio=str(audio),
                source_audio_rel=rel, chunk_path=str(chunk_dir / f"{chunk_id}.wav"),
                start_seconds=seg.start, end_seconds=seg.end,
                duration_seconds=round(seg.end - seg.start, 3),
                speaker=seg.name or None,
            ))
            sequence += 1
    return records


def _merge_turns_to_segments(
    turns: list[SpeakerTurn], names: dict[str, str], chunk_seconds: float,
    gap: float = SPEAKER_MERGE_GAP_SECONDS,
) -> list[_Segment]:
    """Merge consecutive same-speaker turns (within ``gap``) and split anything over the cap."""
    merged: list[_Segment] = []
    current: _Segment | None = None
    for turn in sorted(turns, key=lambda t: t.start):
        name = names.get(turn.speaker, turn.speaker)
        if (
            current is not None
            and current.name == name
            and turn.start - current.end <= gap
            and turn.end - current.start <= chunk_seconds
        ):
            current = _Segment(current.start, max(current.end, turn.end), name)
        else:
            if current is not None:
                merged.append(current)
            current = _Segment(turn.start, turn.end, name)
    if current is not None:
        merged.append(current)

    out: list[_Segment] = []
    for seg in merged:
        span = seg.end - seg.start
        if span <= chunk_seconds:
            out.append(_Segment(round(seg.start, 3), round(seg.end, 3), seg.name))
            continue
        pieces = math.ceil(span / chunk_seconds)
        step = span / pieces
        for i in range(pieces):
            start = seg.start + i * step
            end = min(seg.end, start + step)
            out.append(_Segment(round(start, 3), round(end, 3), seg.name))
    return out


def ensure_audio_chunks(chunks: list[ChunkRecord], force: bool, on_progress: Progress | None) -> None:
    for idx, record in enumerate(chunks, 1):
        _emit(on_progress, "chunk", idx, len(chunks))
        path = Path(record.chunk_path)
        if path.exists() and path.stat().st_size > 1024 and not force:
            continue
        ffmpeg.extract_chunk(Path(record.source_audio), path, record.start_seconds, record.duration_seconds)


def transcribe_chunks(
    chunks: list[ChunkRecord], transcriber: Transcriber, work_dir: Path, force: bool,
    on_progress: Progress | None,
) -> list[dict[str, Any]]:
    tdir = work_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    prompt = schemas.transcription_prompt()
    items: list[dict[str, Any]] = []
    for idx, record in enumerate(chunks, 1):
        _emit(on_progress, "transcribe", idx, len(chunks))
        txt_path = tdir / f"{record.chunk_id}.txt"
        json_path = tdir / f"{record.chunk_id}.json"
        if txt_path.exists() and json_path.exists() and not force:
            text = txt_path.read_text(encoding="utf-8").strip()
        else:
            text, raw = transcriber.transcribe(Path(record.chunk_path), prompt=prompt)
            _write_text(txt_path, text.rstrip() + "\n")
            _write_json(json_path, raw)
        items.append({"chunk": asdict(record), "text": text})
    return items


def extract_chunk_atomics(
    chunks: list[ChunkRecord], transcript_items: list[dict[str, Any]], summarizer: Summarizer,
    work_dir: Path, reasoning_effort: str, force: bool, on_progress: Progress | None,
) -> list[dict[str, Any]]:
    adir = work_dir / "atomic_chunks"
    adir.mkdir(parents=True, exist_ok=True)
    by_id = {item["chunk"]["chunk_id"]: item for item in transcript_items}
    all_items: list[dict[str, Any]] = []
    for idx, record in enumerate(chunks, 1):
        _emit(on_progress, "atomic", idx, len(chunks))
        out = adir / f"{record.chunk_id}.json"
        if out.exists() and not force:
            data = _read_json(out)
        else:
            text = by_id[record.chunk_id]["text"]
            speaker_line = f"Speaker: {record.speaker}\n" if record.speaker else ""
            user = (
                f"Source chunk: {record.chunk_id}\nSource audio: {record.source_audio_rel}\n"
                f"{speaker_line}"
                f"Time range: {_fmt(record.start_seconds)} - {_fmt(record.end_seconds)}\n\n"
                f"Transcript:\n{text or '[empty transcript]'}"
            )
            data = summarizer.structured_json(
                system=schemas.atomic_extraction_system_prompt(), user=user,
                schema_name="atomic_summary", schema=schemas.atomic_summary_schema(),
                reasoning_effort=reasoning_effort,
            )
            _write_json(out, data)
        all_items.extend(data.get("items", []))
    return all_items


def consolidate(
    chunk_atomics: list[dict[str, Any]], meeting_date: str, summarizer: Summarizer,
    work_dir: Path, reasoning_effort: str, force: bool,
) -> dict[str, Any]:
    out = work_dir / "atomic_summary.json"
    if out.exists() and not force:
        return _read_json(out)
    user = (
        f"Meeting date: {meeting_date}\n\nChunk-level atomic facts:\n"
        f"{json.dumps({'items': chunk_atomics}, ensure_ascii=False, indent=2)}\n\n"
        "Deduplicate the facts and keep only facts useful for board minutes. "
        "Retain conservative uncertainty markers."
    )
    return summarizer.structured_json(
        system=schemas.atomic_consolidation_system_prompt(), user=user,
        schema_name="atomic_summary", schema=schemas.atomic_summary_schema(),
        reasoning_effort=reasoning_effort,
    )


def generate_minutes(
    atomic_summary: dict[str, Any], meeting_dir: Path, audio_files: list[Path],
    meeting_date: str, meeting_time: str, location: str, prepared_by: str,
    summarizer: Summarizer, reasoning_effort: str,
) -> str:
    recordings = ", ".join(a.name for a in audio_files) or "Not explicitly captured"
    user = (
        "Create Google-Docs-friendly Markdown board meeting minutes in this exact section order:\n"
        "1. Board Meeting Minutes\n2. Metadata\n3. Attendance\n4. Decisions / Approvals\n"
        "5. Discussion & Updates\n6. Motions\n7. Action Items / Assignments\n8. Next Steps\n9. Adjournment\n\n"
        "Use Markdown tables for Metadata, Motions, and Action Items / Assignments. "
        "Use concise bullets elsewhere. Do not include source chunk IDs. Do not invent names, movers, "
        "seconders, due dates, votes, attendance, or adjournment time. Use 'Not explicitly captured', "
        "'Person', 'N/A', or '(Follow-up)' when details are unclear.\n\n"
        "Metadata defaults unless contradicted by the atomic summary:\n"
        f"- Meeting Date: {meeting_date}\n- Meeting Time: {meeting_time}\n- Meeting Location: {location}\n"
        f"- Audio/Video Recording File: {recordings}\n- Minutes Prepared By: {prepared_by}\n\n"
        "Atomic summary JSON:\n"
        f"{json.dumps(atomic_summary, ensure_ascii=False, indent=2)}"
    )
    data = summarizer.structured_json(
        system=schemas.minutes_system_prompt(), user=user,
        schema_name="minutes_markdown", schema=schemas.minutes_schema(),
        reasoning_effort=reasoning_effort,
    )
    markdown = (data.get("markdown") or "").strip()
    if not markdown:
        raise RuntimeError("The summarizer returned an empty minutes markdown field.")
    return markdown


# --- helpers -----------------------------------------------------------------


def derive_meeting_date(meeting_dir: Path) -> str:
    match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", meeting_dir.name)
    if not match:
        return "Not explicitly captured"
    month, day, year = (int(p) for p in match.groups())
    full_year = 2000 + year if year < 70 else 1900 + year
    try:
        return datetime(full_year, month, day).strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return "Not explicitly captured"


def _render_transcript_md(items: list[dict[str, Any]]) -> str:
    lines = ["# WCFI Meeting Transcript", ""]
    for item in items:
        chunk = item["chunk"]
        speaker = chunk.get("speaker")
        heading = f"### {chunk['chunk_id']}" + (f" — {speaker}" if speaker else "")
        meta = [f"- Source: {chunk['source_audio_rel']}"]
        if speaker:
            meta.append(f"- Speaker: {speaker}")
        meta.append(f"- Time: {_fmt(chunk['start_seconds'])} - {_fmt(chunk['end_seconds'])}")
        lines += [heading, "", *meta, "", item["text"] or "[empty transcript]", ""]
    return "\n".join(lines).rstrip() + "\n"


def _emit(cb: Progress | None, stage: str, current: int, total: int) -> None:
    if cb is not None:
        cb(stage, current, total)


def _fmt(seconds: float) -> str:
    total = int(round(seconds))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _natural_key(value: str) -> list[tuple[int, Any]]:
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", value.lower())]


def _relposix(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _slugify(value: str, max_len: int = 100) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return (re.sub(r"_+", "_", value) or "item")[:max_len]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
