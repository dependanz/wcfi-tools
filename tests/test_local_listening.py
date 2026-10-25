"""Check local audio provenance and failure boundaries, not model accuracy."""

import importlib.util
import json
from pathlib import Path

import pytest

from wcfi_tools.soundcheck.local_listening import (
    MusicFlamingoListener,
    listen_local,
    prepare_model_audio,
    read_model_audio,
)
from wcfi_tools.soundcheck.profiles import file_hash

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")


def test_required_resampling_retains_tone_and_records_exact_input(tmp_path):
    rate = 48000
    t = np.arange(rate * 10) / rate
    tone = .25 * np.sin(2 * np.pi * 440 * t)
    samples = np.column_stack([tone + .1 * np.sin(2 * np.pi * 12000 * t), tone])
    source, out = tmp_path / "source.wav", tmp_path / "model.wav"
    sf.write(source, samples, rate, subtype="FLOAT")
    original = source.read_bytes()
    result = prepare_model_audio(source, out)
    actual, model_rate = sf.read(out)
    assert model_rate == 16000 and actual.shape == (160000,)
    expected = .25 * np.sin(2 * np.pi * 440 * np.arange(160000) / model_rate)
    assert np.sqrt(np.mean((actual[100:-100] - expected[100:-100]) ** 2)) < .001
    assert result["sha256"] == file_hash(out)
    assert result["source_sha256"] == file_hash(source)
    assert result["protective_gain_db"] == 0
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="already exists"):
        prepare_model_audio(source, out)


def test_antiphase_refused_before_creating_model_input(tmp_path):
    rate = 16000
    tone = .25 * np.sin(2 * np.pi * 440 * np.arange(rate * 10) / rate)
    source, out = tmp_path / "source.wav", tmp_path / "model.wav"
    sf.write(source, np.column_stack([tone, -tone]), rate, subtype="FLOAT")
    with pytest.raises(ValueError, match="cancels strongly"):
        prepare_model_audio(source, out)
    assert not out.exists()


@pytest.mark.parametrize("level", [0, .25])
@pytest.mark.parametrize("rate", [16000, 48000])
def test_silence_and_dc_never_receive_quality_ratings(tmp_path, level, rate):
    source, out = tmp_path / "source.wav", tmp_path / "model.wav"
    sf.write(source, np.full(rate * 10, level, dtype="float32"), rate, subtype="FLOAT")
    with pytest.raises(ValueError, match="No varying audio"):
        prepare_model_audio(source, out)
    assert not out.exists()
    if rate == 16000:
        with pytest.raises(ValueError, match="No varying audio"):
            read_model_audio(source)


def test_nonfinite_model_input_rejected(tmp_path):
    source = tmp_path / "invalid.wav"
    sf.write(source, np.full(160000, np.nan), 16000, subtype="FLOAT")
    with pytest.raises(ValueError, match="finite mono"):
        read_model_audio(source)


def test_gpu_requires_opt_in_before_loading_libraries(tmp_path, monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"torch", "bitsandbytes", "transformers"}:
            pytest.fail("A default call must not load GPU libraries")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(RuntimeError, match="explicit --allow-gpu"):
        MusicFlamingoListener(tmp_path / "missing-checkpoint")


def test_resume_partial_review_fails_before_loading_gpu(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "local-listener.py"
    spec = importlib.util.spec_from_file_location("local_listener_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = tmp_path / "trial"
    (directory / "clips").mkdir(parents=True)
    clip = directory / "clips" / "clip-01.wav"
    clip.write_bytes(b"placeholder: only its hash is needed before preflight fails")
    (directory / "blind-manifest.json").write_text(json.dumps({"clips": [{
        "id": "clip-01", "file": "clips/clip-01.wav", "sha256": file_hash(clip),
        "duration_seconds": 15,
    }]}))
    output = tmp_path / "results"
    (output / "clip-01").mkdir(parents=True)
    monkeypatch.setattr(module, "MusicFlamingoListener",
                        lambda *_: pytest.fail("GPU must not load for an incomplete resume"))
    with pytest.raises(ValueError, match="Interrupted review"):
        module.benchmark(directory, output, tmp_path / "model", tmp_path / "ffmpeg", resume=True)


def test_local_review_records_failure_without_retry_or_cloud(tmp_path, monkeypatch):
    from wcfi_tools.soundcheck import local_listening

    rate = 16000
    source = tmp_path / "private-source.wav"
    sf.write(source, .25 * np.sin(2 * np.pi * 440 * np.arange(rate * 10) / rate), rate)
    binary = tmp_path / "ffmpeg.exe"
    binary.touch()
    monkeypatch.setattr(local_listening, "inspect_mix", lambda *args, **kwargs: {
        "metrics": {"integrated_lufs": -70, "loudness_range_lu": 0,
                    "sample_peak_dbfs": -100, "true_peak_dbtp": -100}})

    class Listener:
        def __init__(self):
            self.runtime = {"engine": "test-local"}
            self.calls = 0

        def observe(self, audio: Path, *, context: str):
            self.calls += 1
            assert audio.name == "excerpt-model.wav"
            assert context == ""
            raise RuntimeError("GPU failure")

    listener = Listener()
    out = tmp_path / "review"
    with pytest.raises(RuntimeError, match="GPU failure"):
        listen_local(source, out, listener=listener, seconds=10, ffmpeg=binary)
    assert listener.calls == 1
    request = json.loads((out / "request.json").read_text())
    report = json.loads((out / "listen.json").read_text())
    assert request["audio_upload"] is False and request["automatic_retries"] == 0
    assert "measurements" not in request
    assert request["model_audio"]["sha256"] == file_hash(out / "excerpt-model.wav")
    assert report["status"] == "failed" and report["observations"] is None
    markup = (out / "report.html").read_text()
    assert "src='excerpt-model.wav'" in markup and "autoplay" not in markup
    assert "Stereo/polarity preserved" not in markup
