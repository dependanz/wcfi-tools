"""Audio evidence and request boundaries; paid model quality requires a separate trial."""

import base64
import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.soundcheck import listening

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")


@pytest.fixture
def ffmpeg():
    binary = shutil.which("ffmpeg")
    if not binary:
        try:
            import imageio_ffmpeg
        except ImportError:
            pytest.skip("FFmpeg required for audio integration test")
        binary = imageio_ffmpeg.get_ffmpeg_exe()
    return Path(binary)


@pytest.fixture
def recording(tmp_path):
    # First two seconds are silence: a wrong seek is observable. Antiphase stereo
    # would disappear in a mono sum, so it also catches accidental downmixing.
    rate = 16000
    tone = .25 * np.sin(2 * np.pi * 440 * np.arange(rate * 12) / rate)
    samples = np.column_stack([tone, -tone])
    samples[:2 * rate] = 0
    path = tmp_path / "private-source-name.wav"
    sf.write(path, samples, rate, subtype="FLOAT")
    return path


def test_cloud_request_contains_only_exact_excerpt_and_context(
    tmp_path, recording, ffmpeg, monkeypatch,
):
    import openai

    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            seen["request"] = kwargs
            return SimpleNamespace(
                model="audio-test-snapshot", id="test-response", usage=None,
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                    content="Uncertain tone. <script>alert('bad')</script>", refusal=None))],
            )

    monkeypatch.setattr(openai, "OpenAI", Client)
    original = recording.read_bytes()
    out = tmp_path / "review"
    result = listening.listen(recording, out, start=2, seconds=10, ffmpeg=ffmpeg,
                              api_key="test-secret", context="Blind sample A")
    assert recording.read_bytes() == original
    assert result["status"] == "reviewed"
    assert result["observations"]["model"] == "audio-test-snapshot"
    payload = seen["request"]
    assert payload["modalities"] == ["text"]
    assert payload["store"] is False
    assert "response_format" not in payload  # Audio model lacks strict structured output.
    assert seen["client"]["base_url"] == "https://api.openai.com/v1"
    assert seen["client"]["max_retries"] == 0
    audio = base64.b64decode(payload["messages"][1]["content"][1]["input_audio"]["data"])
    assert audio == (out / "excerpt-upload.wav").read_bytes()
    samples, rate = sf.read(io.BytesIO(audio), always_2d=True)
    assert len(samples) == 10 * rate
    assert np.max(np.abs(samples)) == pytest.approx(.25, abs=1e-4)
    assert np.max(np.abs(samples.sum(axis=1))) < 1e-4
    assert recording.name not in json.dumps(payload)
    assert "integrated_lufs" not in json.dumps(payload)
    assert result["measurements"]["metrics"]["sample_peak_dbfs"] == pytest.approx(-12, abs=.1)
    for name in ("listen.json", "request.json", "report.html"):
        assert "test-secret" not in (out / name).read_text(encoding="utf-8")
    report = (out / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in report and "&lt;script&gt;" in report


def test_offline_does_not_contact_model_and_protects_float_overload(
    tmp_path, ffmpeg, monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline mode attempted API access")

    monkeypatch.setattr(listening, "_request_observations", forbidden)
    path = tmp_path / "over.wav"
    samples = 2 * np.sin(2 * np.pi * 1000 * np.arange(16000 * 10) / 16000)
    sf.write(path, samples, 16000, subtype="FLOAT")
    out = tmp_path / "offline"
    result = listening.listen(path, out, seconds=10, offline=True, ffmpeg=ffmpeg)
    assert result["status"] == "prepared_offline"
    assert result["observations"] is None
    assert result["excerpt"]["upload_gain_db"] == pytest.approx(-6.021, abs=.002)
    original, _ = sf.read(out / "excerpt-original.wav")
    uploaded, _ = sf.read(out / "excerpt-upload.wav")
    assert np.max(original) == pytest.approx(2)
    assert np.max(uploaded) < 1
    assert result["measurements"]["metrics"]["sample_peak_dbfs"] == pytest.approx(6, abs=.1)
    assert json.loads((out / "request.json").read_text())["mode"] == "offline_no_upload"
    with pytest.raises(ValueError, match="never overwritten"):
        listening.listen(path, out, seconds=10, offline=True, ffmpeg=ffmpeg)


@pytest.mark.parametrize("start,seconds", [(float('nan'), 10), (0, 61), (3, 10)])
def test_invalid_window_never_requests_or_creates_output(
    tmp_path, recording, ffmpeg, monkeypatch, start, seconds,
):
    monkeypatch.setattr(listening, "_request_observations", lambda **kw: pytest.fail("API called"))
    out = tmp_path / "invalid"
    with pytest.raises(ValueError):
        listening.listen(recording, out, start=start, seconds=seconds,
                         api_key="test", ffmpeg=ffmpeg)
    assert not out.exists()


def test_failed_request_preserves_evidence_without_leaking_error_body(
    tmp_path, recording, ffmpeg, monkeypatch,
):
    def failed(*args, **kwargs):
        raise RuntimeError("secret-private-server-body")

    monkeypatch.setattr(listening, "_request_observations", failed)
    out = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="No automatic retry") as error:
        listening.listen(recording, out, seconds=10, api_key="test", ffmpeg=ffmpeg)
    assert "secret-private" not in str(error.value)
    report = json.loads((out / "listen.json").read_text())
    assert report["status"] == "failed" and report["observations"] is None
    assert (out / "excerpt-upload.wav").exists()
    assert "secret-private" not in (out / "report.html").read_text()


def test_cli_missing_key_explains_offline_path_without_writing(tmp_path, recording, monkeypatch):
    from wcfi_tools import config

    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(config, "get_secret", lambda _: None)
    out = tmp_path / "no-key"
    result = CliRunner().invoke(app, ["soundcheck", "listen", str(recording),
                                     "--output", str(out)])
    assert result.exit_code == 1
    assert "--offline" in result.output
    assert not out.exists()


@pytest.mark.parametrize("finish,content,refusal", [
    ("length", "partial", None), ("stop", None, "refused"), ("stop", "", None),
])
def test_unusable_model_responses_are_rejected(monkeypatch, finish, content, refusal):
    import openai

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:
                SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                    message=SimpleNamespace(content=content, refusal=refusal))])))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(openai, "OpenAI", Client)
    with pytest.raises(RuntimeError):
        listening._request_observations(b"fake", api_key="test", model="test", context="")
