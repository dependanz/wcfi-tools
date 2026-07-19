"""Tests for local speaker identity: voiceprint DB, matching, and cluster-naming logic.

Pure logic only — no torch, pyannote, network, or audio required.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.providers.base import DiarizationResult, SpeakerTurn
from wcfi_tools.speakers.identify import (
    attribute_turns,
    enroll_confirmed,
    propose_from_voiceprints,
    select_representative_turns,
)
from wcfi_tools.speakers.voiceprints import VoiceprintDB, cosine_similarity

runner = CliRunner()


def test_cosine_similarity():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 1.0], [2.0, 2.0]) == pytest.approx(1.0)
    assert cosine_similarity([], [1.0]) == 0.0  # length mismatch / empty
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector


def test_voiceprint_db_roundtrip(tmp_path):
    path = tmp_path / "voiceprints.json"
    db = VoiceprintDB.load(path)
    db.enroll("Pastor Jun", [1.0, 0.0, 0.0])
    db.enroll("Sister Mae", [0.0, 1.0, 0.0])
    db.save()

    reloaded = VoiceprintDB.load(path)
    assert reloaded.names() == ["Pastor Jun", "Sister Mae"]
    name, score = reloaded.match([0.9, 0.1, 0.0])[0]
    assert name == "Pastor Jun"
    assert score > 0.9


def test_best_match_threshold():
    db = VoiceprintDB()
    db.enroll("A", [1.0, 0.0])
    assert db.best_match([1.0, 0.05], threshold=0.9) is not None
    assert db.best_match([0.2, 1.0], threshold=0.9) is None  # too dissimilar


def test_enroll_caps_samples():
    db = VoiceprintDB()
    for i in range(10):
        db.enroll("A", [float(i), 1.0], max_samples=3)
    assert db.sample_count("A") == 3


def test_forget_and_rename():
    db = VoiceprintDB()
    db.enroll("Old", [1.0, 0.0])
    assert db.rename("Old", "New") is True
    assert "New" in db and "Old" not in db
    assert db.forget("New") is True
    assert len(db) == 0
    assert db.forget("Ghost") is False


def test_rename_merges_samples():
    db = VoiceprintDB()
    db.enroll("A", [1.0, 0.0])
    db.enroll("B", [0.0, 1.0])
    assert db.rename("A", "B") is True
    assert db.sample_count("B") == 2


def _diar() -> DiarizationResult:
    turns = [
        SpeakerTurn(0.0, 6.0, "SPEAKER_00"),
        SpeakerTurn(6.0, 6.5, "SPEAKER_01"),  # too short to be a good snippet
        SpeakerTurn(10.0, 18.0, "SPEAKER_01"),
    ]
    embeddings = {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0]}
    return DiarizationResult(turns=turns, embeddings=embeddings)


def test_diarization_result_roundtrips():
    diar = _diar()
    restored = DiarizationResult.from_dict(diar.to_dict())
    assert [(t.start, t.end, t.speaker) for t in restored.turns] == [
        (t.start, t.end, t.speaker) for t in diar.turns
    ]
    assert restored.embeddings == diar.embeddings
    assert restored.labels() == diar.labels()


def test_propose_from_voiceprints():
    db = VoiceprintDB()
    db.enroll("Pastor Jun", [1.0, 0.0, 0.0])
    proposals = propose_from_voiceprints(_diar(), db, threshold=0.65)

    assert proposals["SPEAKER_00"].name == "Pastor Jun"
    assert proposals["SPEAKER_00"].confident is True
    assert proposals["SPEAKER_01"].name is None  # nobody enrolled matches this voice


def test_select_representative_turns_prefers_long_clear_snippets():
    picks = select_representative_turns(_diar().turns, per_speaker=2, min_dur=3.0)
    # SPEAKER_01's 0.5s turn is skipped in favour of the 8s one.
    assert [t.start for t in picks["SPEAKER_01"]] == [10.0]
    assert [t.start for t in picks["SPEAKER_00"]] == [0.0]


def test_attribute_turns_keeps_unnamed_labels():
    mapping = {"SPEAKER_00": "Pastor Jun"}
    attributed = attribute_turns(_diar().turns, mapping)
    names = {name for _, _, name in attributed}
    assert "Pastor Jun" in names
    assert "SPEAKER_01" in names  # unnamed cluster preserved, not dropped


def test_enroll_confirmed_saves_named_only():
    db = VoiceprintDB()
    n = enroll_confirmed(db, _diar(), {"SPEAKER_00": "Pastor Jun", "SPEAKER_01": ""})
    assert n == 1
    assert db.names() == ["Pastor Jun"]


# --- CLI: speakers management ------------------------------------------------


def _patch_db(monkeypatch, tmp_path):
    path = tmp_path / "voiceprints.json"
    monkeypatch.setattr("wcfi_tools.speakers.voiceprints.db_path", lambda: path)
    monkeypatch.setattr("wcfi_tools.commands.speakers.db_path", lambda: path)
    return path


def test_speakers_list_empty(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    result = runner.invoke(app, ["speakers", "list"])
    assert result.exit_code == 0
    assert "No speakers enrolled" in result.output


def test_speakers_list_and_forget(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    db = VoiceprintDB.load()
    db.enroll("Pastor Jun", [1.0, 0.0])
    db.save()

    result = runner.invoke(app, ["speakers", "list"])
    assert result.exit_code == 0
    assert "Pastor Jun" in result.output

    result = runner.invoke(app, ["speakers", "forget", "Pastor Jun", "--yes"])
    assert result.exit_code == 0
    assert len(VoiceprintDB.load()) == 0
