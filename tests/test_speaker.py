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


def test_match_clusters_splits_known_and_unknown():
    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    clusters = {"Voice 1": [_seg(a), _seg(a * 0.9 + b * 0.1)], "Voice 2": [_seg(b), _seg(b * 0.95)]}
    named, unknown = identify.match_clusters(clusters, {"Alice": a}, threshold=0.7)
    assert named == {"Voice 1": "Alice"}
    assert list(unknown) == ["Voice 2"]
    assert all(s.name == "Alice" for s in clusters["Voice 1"])  # named segs are tagged


def test_merge_units_merges_same_voice_only_across_windows():
    from wcfi_tools.speaker import diarize

    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    # window 0 has voices a and b; window 1 has voice a again (independent per-window indices)
    units = [(0, [_seg(a)]), (0, [_seg(b)]), (1, [_seg(a * 0.98)])]
    groups = diarize.merge_units(units, threshold=0.7)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2]  # the two 'a' groups (different windows) merged; 'b' stayed separate


def test_merge_units_targets_speaker_count():
    from wcfi_tools.speaker import diarize

    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    c = np.array([0, 0, 1], np.float32)
    # 4 groups across distinct windows; ask for exactly 2 → closest pair collapses
    units = [(0, [_seg(a)]), (1, [_seg(a * 0.97)]), (2, [_seg(b)]), (3, [_seg(c)])]
    groups = diarize.merge_units(units, num_speakers=2)
    assert len(groups) == 2


def test_diarize_module_imports_and_segmentation_model_registered():
    from wcfi_tools.speaker import diarize, models

    assert isinstance(diarize.available(), bool)
    seg = models.SPEC["segmentation"]
    assert seg["archive"] == "tar.bz2" and seg["path"].endswith("model.onnx")
    assert seg["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/")


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
