"""Tests for the speaker-attribution pieces (no torch/pyannote/HF token needed)."""

from __future__ import annotations

from wcfi_tools.meeting.speakers import roster_context, speaker_durations
from wcfi_tools.providers.diarize import DiarTurn, WindowDiarizer
from wcfi_tools.web.annotator import AnnotatorState, build_app


def test_window_diarizer_covers_audio(monkeypatch):
    import wcfi_tools.providers.diarize as d

    monkeypatch.setattr(d.ffmpeg, "ffprobe_duration", lambda _p: 32.0)
    turns = WindowDiarizer(window_seconds=15.0, speakers=2).diarize(__import__("pathlib").Path("x.wav"))
    assert len(turns) == 3  # 15 + 15 + 2
    assert turns[0].speaker != turns[1].speaker  # round-robin
    assert abs(turns[-1].end - 32.0) < 1e-6


def test_speaker_durations_sorted():
    turns = [DiarTurn("A", 0, 10), DiarTurn("B", 10, 12), DiarTurn("A", 12, 15)]
    durations = speaker_durations(turns)
    assert durations["A"] == 13
    assert list(durations)[0] == "A"  # sorted by total desc


def test_roster_context_skips_unsure():
    assert roster_context({"A": "Joy Rimundo", "B": "Unsure", "C": ""}) != ""
    assert "Joy Rimundo" in roster_context({"A": "Joy Rimundo", "B": "Unsure"})
    assert roster_context({"A": "Unsure", "B": ""}) == ""


def test_annotator_app_serves_and_accepts_labels(tmp_path):
    from fastapi.testclient import TestClient

    clip = tmp_path / "spk_0.wav"
    clip.write_bytes(b"RIFF....WAVEfake")  # served as bytes; content not validated here
    state = AnnotatorState(
        speakers=[{"label": "Speaker A", "seconds": 42, "count": 1}],
        files={"Speaker A": [clip]},
    )
    client = TestClient(build_app(state))

    page = client.get("/")
    assert page.status_code == 200 and "Who is speaking" in page.text

    snippet = client.get("/snippet", params={"speaker": "Speaker A", "idx": 0})
    assert snippet.status_code == 200

    resp = client.post("/submit", json={"labels": {"Speaker A": "Joy Rimundo"}})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert state.result == {"Speaker A": "Joy Rimundo"}
    assert state.done.is_set()
