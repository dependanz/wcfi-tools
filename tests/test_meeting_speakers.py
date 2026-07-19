"""Tests for the unified summarize-with-speaker-identification path (no torch/network/audio)."""

from __future__ import annotations

from pathlib import Path

from wcfi_tools.meeting.pipeline import (
    _merge_turns_to_segments,
    _render_transcript_md,
    build_speaker_chunk_records,
)
from wcfi_tools.meeting.speaker_id import auto_resolver, resolve_meeting_speakers
from wcfi_tools.providers.base import DiarizationResult, SpeakerTurn
from wcfi_tools.speakers.voiceprints import VoiceprintDB


# --- turn merging ------------------------------------------------------------


def test_merge_same_speaker_within_gap():
    turns = [SpeakerTurn(0.0, 4.0, "S0"), SpeakerTurn(5.0, 8.0, "S0")]  # 1s gap
    segs = _merge_turns_to_segments(turns, {}, chunk_seconds=300)
    assert len(segs) == 1
    assert (segs[0].start, segs[0].end, segs[0].name) == (0.0, 8.0, "S0")


def test_merge_breaks_on_speaker_change_and_large_gap():
    turns = [
        SpeakerTurn(0.0, 4.0, "S0"),
        SpeakerTurn(4.0, 8.0, "S1"),   # speaker change
        SpeakerTurn(30.0, 34.0, "S1"),  # same speaker but 22s gap
    ]
    segs = _merge_turns_to_segments(turns, {}, chunk_seconds=300)
    assert [s.name for s in segs] == ["S0", "S1", "S1"]


def test_merge_applies_names_and_caps_length():
    turns = [SpeakerTurn(0.0, 25.0, "S0")]
    segs = _merge_turns_to_segments(turns, {"S0": "Pastor Jun"}, chunk_seconds=10)
    assert len(segs) == 3  # 25s / 10s cap -> 3 pieces
    assert all(s.name == "Pastor Jun" for s in segs)
    assert all(s.end - s.start <= 10.0 + 1e-6 for s in segs)


# --- speaker-aware chunk records --------------------------------------------


def test_build_speaker_chunk_records_attributes_each_chunk(tmp_path):
    audio = tmp_path / "mtg.m4a"  # need not exist: turns present -> no ffprobe/ffmpeg
    diar = DiarizationResult(
        turns=[
            SpeakerTurn(0.0, 6.0, "SPEAKER_00"),
            SpeakerTurn(6.5, 10.0, "SPEAKER_00"),
            SpeakerTurn(10.0, 18.0, "SPEAKER_01"),
        ],
        embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
    )
    names = {"SPEAKER_00": "Pastor Jun", "SPEAKER_01": "Sister Mae"}
    records = build_speaker_chunk_records(
        tmp_path, [audio], {str(audio): diar}, {str(audio): names}, tmp_path / "_work", 5.0
    )
    assert [r.speaker for r in records] == ["Pastor Jun", "Sister Mae"]
    assert [r.start_seconds for r in records] == [0.0, 10.0]
    assert all(r.sequence == i + 1 for i, r in enumerate(records))


# --- resolution + reuse ------------------------------------------------------


class _FakeDiarizer:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def diarize(self, audio_path: Path) -> DiarizationResult:
        self.calls += 1
        return DiarizationResult(
            turns=[SpeakerTurn(0.0, 8.0, "SPEAKER_00"), SpeakerTurn(8.0, 16.0, "SPEAKER_01")],
            embeddings={"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0]},
        )


def test_resolve_auto_matches_known_leaves_unknown_anonymous(tmp_path):
    db = VoiceprintDB()
    db.enroll("Pastor Jun", [1.0, 0.0, 0.0])
    audio = tmp_path / "mtg.m4a"
    work_dir = tmp_path / "_work"

    _, names_by_file = resolve_meeting_speakers(
        [audio], diarizer=_FakeDiarizer(), db=db, work_dir=work_dir,
        resolver=auto_resolver, threshold=0.65,
    )
    # SPEAKER_00 auto-matched; SPEAKER_01 has no enrolled match -> left out (anonymous downstream).
    assert names_by_file[str(audio)] == {"SPEAKER_00": "Pastor Jun"}
    assert (work_dir / "diarization" / "mtg.raw.json").exists()
    assert (work_dir / "diarization" / "mtg.names.json").exists()


def test_resolve_reuses_prior_names_without_reprompting(tmp_path):
    db = VoiceprintDB()
    audio = tmp_path / "mtg.m4a"
    work_dir = tmp_path / "_work"

    # First run: a resolver that names both clusters.
    first = resolve_meeting_speakers(
        [audio], diarizer=_FakeDiarizer(), db=db, work_dir=work_dir,
        resolver=lambda ctx: {"SPEAKER_00": "A", "SPEAKER_01": "B"}[ctx.cluster],
    )
    assert first[1][str(audio)] == {"SPEAKER_00": "A", "SPEAKER_01": "B"}

    # Second run: diarization is cached and every cluster is preassigned, so the resolver that
    # explodes if called must never be called.
    def boom(ctx):
        raise AssertionError("resolver should not be called when names are cached")

    fake = _FakeDiarizer()
    _, names_by_file = resolve_meeting_speakers(
        [audio], diarizer=fake, db=db, work_dir=work_dir, resolver=boom,
    )
    assert fake.calls == 0  # raw.json cache means no re-diarization
    assert names_by_file[str(audio)] == {"SPEAKER_00": "A", "SPEAKER_01": "B"}


# --- attributed transcript ---------------------------------------------------


def test_transcript_render_includes_speaker():
    items = [
        {"chunk": {"chunk_id": "0001_x", "source_audio_rel": "x.m4a",
                   "start_seconds": 0.0, "end_seconds": 8.0, "speaker": "Pastor Jun"},
         "text": "Let us open in prayer."},
        {"chunk": {"chunk_id": "0002_x", "source_audio_rel": "x.m4a",
                   "start_seconds": 8.0, "end_seconds": 16.0, "speaker": None},
         "text": "..."},
    ]
    md = _render_transcript_md(items)
    assert "0001_x — Pastor Jun" in md
    assert "- Speaker: Pastor Jun" in md
    assert "0002_x\n" in md  # unattributed chunk keeps a plain heading
