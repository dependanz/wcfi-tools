"""Verify blind inputs, source preservation, and explicit paid-request boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest

from wcfi_tools.soundcheck.profiles import file_hash, write_json

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")

spec = importlib.util.spec_from_file_location(
    "listening_benchmark", Path(__file__).parents[1] / "scripts" / "listening-benchmark.py",
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_preparation_preserves_source_and_blinds_verified_matched_clips(tmp_path, monkeypatch):
    import imageio_ffmpeg

    rate = 16000
    time = np.arange(rate * 32) / rate
    signal = .45 * np.sin(2 * np.pi * 440 * time) + .1 * np.sin(2 * np.pi * 6000 * time)
    path = tmp_path / "private-church-name.wav"
    sf.write(path, np.column_stack([signal, -signal]), rate, subtype="FLOAT")
    before = file_hash(path)
    monkeypatch.setattr(benchmark, "listen", lambda *a, **kw: pytest.fail("Preparation uploaded"))
    out = tmp_path / "trial"
    manifest = benchmark.prepare(path, out, starts=[0, 20], seconds=10,
                                 seed=7, ffmpeg=Path(imageio_ffmpeg.get_ffmpeg_exe()))
    assert file_hash(path) == before
    public_text = json.dumps(manifest)
    for secret in (path.name, "control", "lowpass", "hard_clipping", "1500", "lufs"):
        assert secret not in public_text
    key = json.loads((out / "private-answer-key.json").read_text())
    assert len(manifest["clips"]) == 6
    for case, truth in zip(manifest["clips"], key["clips"], strict=True):
        clip = out / case["file"]
        assert file_hash(clip) == case["sha256"]
        samples, _ = sf.read(clip, always_2d=True)
        # Conversion/filtering must retain both channels and their polarity.
        assert np.max(np.abs(samples.sum(axis=1))) <= 1 / 32768 + 1e-8
        assert truth["after_matching"]["integrated_lufs"] == pytest.approx(
            key["target_integrated_lufs"], abs=.21,
        )
        assert truth["after_matching"]["true_peak_dbtp"] <= -2.7
    with pytest.raises(ValueError, match="never overwritten"):
        benchmark.prepare(path, out, starts=[0, 20], seconds=10, seed=7, ffmpeg=Path(__file__))


def prepared_manifest(tmp_path):
    (tmp_path / "clips").mkdir()
    cases = []
    for index in (1, 2):
        clip = tmp_path / "clips" / f"clip-{index:02d}.wav"
        clip.write_bytes(b"placeholder verified file")
        cases.append({"id": f"clip-{index:02d}", "file": f"clips/clip-{index:02d}.wav",
                      "duration_seconds": 10, "sha256": file_hash(clip)})
    write_json(tmp_path / "blind-manifest.json", {"clips": cases})


def test_submission_stops_after_failure_and_cannot_automatically_retry(tmp_path, monkeypatch):
    prepared_manifest(tmp_path)
    attempted = []

    def failed(clip, output, **kwargs):
        attempted.append(clip)
        assert kwargs["context"] == ""
        raise RuntimeError("sensitive-server-body")

    monkeypatch.setattr(benchmark, "listen", failed)
    result = benchmark.submit(tmp_path, api_key="test-secret", model="test", ffmpeg=Path(__file__))
    assert len(attempted) == 1
    assert result["status"] == "failed_partial"
    saved = (tmp_path / "submission.json").read_text()
    assert "test-secret" not in saved and "sensitive-server-body" not in saved
    with pytest.raises(ValueError, match="no automatic retries"):
        benchmark.submit(tmp_path, api_key="test-secret", model="test", ffmpeg=Path(__file__))


def test_tampered_clip_is_rejected_before_any_request(tmp_path, monkeypatch):
    prepared_manifest(tmp_path)
    (tmp_path / "clips" / "clip-02.wav").write_bytes(b"changed")
    monkeypatch.setattr(benchmark, "listen", lambda *a, **kw: pytest.fail("Tampered audio uploaded"))
    with pytest.raises(ValueError, match="changed or escaped"):
        benchmark.submit(tmp_path, api_key="test", model="test", ffmpeg=Path(__file__))
    assert not (tmp_path / "reviews").exists()
