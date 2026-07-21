"""Speaker store + matching/clustering + annotator tests (no sherpa-onnx / model download)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wcfi_tools.speaker import identify, store
from wcfi_tools.speaker.identify import Seg


def _seg(vec):
    v = np.asarray(vec, dtype=np.float32)
    return Seg(Path("x"), 0.0, 1.0, np.zeros(1, dtype=np.float32), v / (np.linalg.norm(v) or 1))


def test_store_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    store.upsert("Joy Rimundo", [np.array([1, 0, 0], np.float32), np.array([0.9, 0.1, 0], np.float32)])
    rows = store.list_speakers()
    assert [r["name"] for r in rows] == ["Joy Rimundo"] and rows[0]["count"] == 2
    prints = store.load()
    assert "Joy Rimundo" in prints and prints["Joy Rimundo"].shape == (3,)
    assert store.remove("Joy Rimundo") is True
    assert store.remove("Joy Rimundo") is False


def test_match_labels_known_and_leaves_unknown():
    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    segs = [_seg(a), _seg(a * 0.9 + b * 0.1), _seg(b), _seg(b * 0.95 + a * 0.05)]
    identify.match(segs, {"Alice": a}, threshold=0.7)
    assert segs[0].name == "Alice" and segs[1].name == "Alice"
    assert segs[2].name is None and segs[3].name is None  # Bob not registered


def test_cluster_unknown_groups_same_voice():
    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    segs = [_seg(a), _seg(b), _seg(b * 0.97 + a * 0.03), _seg(b * 0.95)]  # 3 of "b", 1 of "a"
    clusters = identify.cluster_unknown(segs, threshold=0.7)
    sizes = sorted(len(m) for m in clusters.values())
    assert sizes == [1, 3]  # one voice with 3 segments, one with 1


def test_annotator_app(tmp_path):
    from fastapi.testclient import TestClient

    from wcfi_tools.web.annotator import AnnotatorState, build_app

    clip = tmp_path / "v.wav"
    clip.write_bytes(b"RIFF0000WAVE")
    voices = [
        {
            "label": "Voice 1",
            "seconds": 12,
            "clips": [
                {"path": clip, "spec": [[0.1, 0.2], [0.3, 0.4]],
                 "scores": [{"t0": 0.0, "t1": 1.0, "score": 0.7}], "dur": 4.0}
            ],
        }
    ]
    state = AnnotatorState(voices=voices, known_names=["Donna Reyes"])
    client = TestClient(build_app(state))
    assert client.get("/").status_code == 200
    data = client.get("/data").json()
    assert data["voices"][0] == {"label": "Voice 1", "seconds": 12, "count": 1}
    assert data["known"] == ["Donna Reyes"]
    cd = client.get("/clip", params={"speaker": "Voice 1", "idx": 0}).json()
    assert cd["dur"] == 4.0 and cd["scores"][0]["score"] == 0.7 and len(cd["spec"]) == 2
    assert client.get("/snippet", params={"speaker": "Voice 1", "idx": 0}).status_code == 200
    assert client.post("/submit", json={"labels": {"Voice 1": "Joy Rimundo"}}).json()["ok"] is True
    assert state.result == {"Voice 1": "Joy Rimundo"} and state.done.is_set()


class _FakeEmbedder:
    def embed(self, x):
        v = np.array([1.0, float(len(x) % 7), 0.0], dtype=np.float32)
        return v / (np.linalg.norm(v) or 1)


def test_mel_spectrogram_shape():
    from wcfi_tools.speaker import viz

    spec = viz.mel_spectrogram(np.random.randn(16000 * 3).astype(np.float32), n_mels=32, n_frames=64)
    assert len(spec) <= 64 and len(spec[0]) == 32
    flat = [x for row in spec for x in row]
    assert 0.0 <= min(flat) and max(flat) <= 1.0


def test_clip_viz_runs():
    from wcfi_tools.speaker import viz

    d = viz.clip_viz(np.random.randn(16000 * 3).astype(np.float32), np.array([1, 0, 0], np.float32), _FakeEmbedder())
    assert d["dur"] == 3.0 and len(d["spec"]) > 0 and len(d["scores"]) > 0


def test_annotator_cancel():
    from fastapi.testclient import TestClient

    from wcfi_tools.web.annotator import AnnotatorState, build_app

    state = AnnotatorState(voices=[{"label": "Voice 1", "seconds": 5, "clips": []}])
    client = TestClient(build_app(state))
    assert client.post("/cancel").json()["ok"] is True
    assert state.cancelled is True and state.done.is_set()
