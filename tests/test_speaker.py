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


def test_match_clusters_splits_known_and_unknown():
    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    clusters = {"Voice 1": [_seg(a), _seg(a * 0.9 + b * 0.1)], "Voice 2": [_seg(b), _seg(b * 0.95)]}
    named, unknown = identify.match_clusters(clusters, {"Alice": a}, threshold=0.7)
    assert named == {"Voice 1": "Alice"}
    assert list(unknown) == ["Voice 2"]
    assert all(s.name == "Alice" for s in clusters["Voice 1"])  # named segs are tagged


def test_merge_across_files_merges_same_voice_only_across_files():
    from wcfi_tools.speaker import diarize

    a = np.array([1, 0, 0], np.float32)
    b = np.array([0, 1, 0], np.float32)
    # file 0 has voices a and b; file 1 has voice a again (independent pyannote labels)
    per_file = [(0, [_seg(a)]), (0, [_seg(b)]), (1, [_seg(a * 0.98)])]
    groups = diarize.merge_across_files(per_file, threshold=0.7)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2]  # the two 'a' groups (different files) merged; 'b' stayed separate


def test_diarize_available_is_bool_without_pyannote():
    from wcfi_tools.speaker import diarize

    assert isinstance(diarize.available(), bool)  # importing the backend never requires pyannote


def test_hf_check_access_no_token():
    from wcfi_tools.speaker import hf

    assert hf.check_access(None) == ("no_token", "")
    assert hf.check_access("") == ("no_token", "")


def test_hf_check_access_gated(monkeypatch):
    import urllib.error

    from wcfi_tools.speaker import hf

    def boom(url, token, timeout=15.0):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(hf, "_get", boom)
    monkeypatch.setattr(hf, "whoami", lambda token: "danzel")  # token is valid, so it's the gate
    status, _ = hf.check_access("tok")
    assert status == "gated"


def test_hf_get_retries_transient_then_succeeds(monkeypatch):
    from wcfi_tools.speaker import hf

    calls = {"n": 0}

    class _Resp:
        pass

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("forcibly closed")
        return _Resp()

    monkeypatch.setattr(hf.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(hf.time, "sleep", lambda *_: None)
    assert isinstance(hf._get("https://huggingface.co/x", None), _Resp)
    assert calls["n"] == 3  # retried past the two resets


def test_hf_get_does_not_retry_http_error(monkeypatch):
    import urllib.error

    import pytest

    from wcfi_tools.speaker import hf

    calls = {"n": 0}

    def http401(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(hf.urllib.request, "urlopen", http401)
    monkeypatch.setattr(hf.time, "sleep", lambda *_: None)
    with pytest.raises(urllib.error.HTTPError):
        hf._get("https://huggingface.co/x", "tok")
    assert calls["n"] == 1  # a real HTTP status is definitive — not retried


def test_hf_reachable_distinguishes_network_from_auth(monkeypatch):
    import urllib.error

    from wcfi_tools.speaker import hf

    def http401(url, token, timeout=20.0):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    def reset(url, token, timeout=20.0):
        raise ConnectionResetError("forcibly closed")

    monkeypatch.setattr(hf, "_get", http401)
    assert hf.reachable() is True  # 401 means we reached HF
    monkeypatch.setattr(hf, "_get", reset)
    assert hf.reachable() is False  # network reset means we didn't


def test_config_exposes_hf_token_and_diarize_backend():
    from wcfi_tools import config as cfg

    assert cfg.SECRET_ENV_VARS["huggingface"] == "HF_TOKEN"
    assert cfg.DEFAULT_CONFIG["diarize"]["backend"] == "auto"


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
