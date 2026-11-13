"""Rendered-mix checks are separate from raw-input calibration."""

import json
import shutil
from pathlib import Path

import pytest

from wcfi_tools.soundcheck.mix import inspect_mix, parse_measurements


def test_scanner_rejects_incomplete_summary():
    with pytest.raises(ValueError, match="summary"):
        parse_measurements("Summary:\n Integrated loudness:\n I: -20.0 LUFS")


def test_scanner_handles_silence_without_nonfinite_json():
    report = parse_measurements("""Summary:
 Integrated loudness:
   I: -70.0 LUFS
 Loudness range:
   LRA: 0.0 LU
 Sample peak:
   Peak: -inf dBFS
 True peak:
   Peak: -inf dBFS
""")
    assert report["integrated_lufs"] is None
    assert report["true_peak_dbtp"] is None
    assert report["minute_shortterm"] == []
    json.dumps(report, allow_nan=False)


@pytest.fixture
def ffmpeg_binary():
    binary = shutil.which("ffmpeg")
    if not binary:
        try:
            import imageio_ffmpeg
            binary = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pytest.skip("FFmpeg required for numerical integration checks")
    return Path(binary)


def test_render_measurement_gain_scaling_and_antiphase(tmp_path, ffmpeg_binary):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    wave = .1 * np.sin(2 * np.pi * 1000 * np.arange(48000 * 12) / 48000)
    # Opposite polarity must not disappear as it would in a naive mono sum.
    stereo = np.column_stack([wave, -wave])
    path = tmp_path / "render.wav"
    sf.write(path, stereo, 48000, subtype="FLOAT")
    before = path.read_bytes()
    first = inspect_mix(path, tmp_path / "first", ffmpeg=ffmpeg_binary)
    assert path.read_bytes() == before
    assert first["metrics"]["sample_peak_dbfs"] == pytest.approx(-20, abs=.1)
    assert first["metrics"]["integrated_lufs"] == pytest.approx(-20, abs=.3)
    assert first["metrics"]["minute_shortterm"][0]["median_lufs"] > -21
    sf.write(path, stereo * 10 ** (6 / 20), 48000, subtype="FLOAT")
    second = inspect_mix(path, tmp_path / "second", ffmpeg=ffmpeg_binary)
    for key in ("integrated_lufs", "sample_peak_dbfs", "true_peak_dbtp"):
        assert second["metrics"][key] - first["metrics"][key] == pytest.approx(6, abs=.2)
    assert first["audio_sha256"] != second["audio_sha256"]
    with pytest.raises(ValueError, match="never overwritten"):
        inspect_mix(path, tmp_path / "first", ffmpeg=ffmpeg_binary)


def test_silent_render_has_no_usable_programme(tmp_path, ffmpeg_binary):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    path = tmp_path / "silence.wav"
    sf.write(path, np.zeros((48000 * 10, 2)), 48000, subtype="FLOAT")
    result = inspect_mix(path, tmp_path / "result", ffmpeg=ffmpeg_binary)
    assert result["metrics"]["integrated_lufs"] is None
    assert result["warnings"]
    json.dumps(result, allow_nan=False)
