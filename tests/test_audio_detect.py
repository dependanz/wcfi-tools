"""CLI/stream integration with deterministic audio, never a real microphone."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

from wcfi_tools.audio.howler_monitor import monitor
from wcfi_tools.cli import app

runner = CliRunner()


class Source:
    def __init__(self, blocks, *, kind="file", rate=16000):
        self.info = {"name": "test input", "kind": kind, "sample_rate": rate,
                     "channels": 1, "channel": 1}
        self.blocks = iter(blocks)
        self.reads = 0

    def read(self):
        self.reads += 1
        value = next(self.blocks, None)
        if isinstance(value, BaseException):
            raise value
        return value


def block(samples, gap=False):
    return SimpleNamespace(samples=np.asarray(samples, dtype=np.float32), gap=gap)


def test_cli_help():
    result = runner.invoke(app, ["audio", "detect", "howler", "--help"])
    assert result.exit_code == 0, result.output
    assert "--device" in result.output
    assert "--threshold-db" in result.output


def test_device_listing_does_not_capture(monkeypatch):
    from wcfi_tools.audio import detection_input

    monkeypatch.setattr(detection_input, "list_input_devices", lambda: [
        {"index": 7, "name": "Laptop mic", "hostapi": "WASAPI", "channels": 2,
         "sample_rate": 48000, "default": True}])
    result = runner.invoke(app, ["audio", "detect", "devices", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["index"] == 7


@pytest.mark.parametrize("options", [
    ["--seconds", "nan"], ["--threshold-db", "nan"],
    ["--min-frequency", "2000", "--max-frequency", "1000"],
])
def test_invalid_options_fail_before_opening_microphone(monkeypatch, options):
    from wcfi_tools.audio import detection_input

    def unexpected(**kwargs):
        pytest.fail("Must reject invalid options before microphone access")

    monkeypatch.setattr(detection_input, "open_microphone", unexpected)
    result = runner.invoke(app, ["audio", "detect", "howler", *options])
    assert result.exit_code != 0


def test_report_is_not_overwritten_or_opened_as_audio(monkeypatch, tmp_path):
    from wcfi_tools.audio import detection_input

    existing = tmp_path / "report.json"
    existing.write_text("original")
    monkeypatch.setattr(detection_input, "open_microphone", lambda **kw: pytest.fail("capture"))
    result = runner.invoke(app, ["audio", "detect", "howler", "--output", str(existing)])
    assert result.exit_code == 1
    assert existing.read_text() == "original"


def test_file_rejects_microphone_options(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"unused")
    result = runner.invoke(app, ["audio", "detect", "howler", str(path), "--device", "0"])
    assert result.exit_code == 1
    assert "only to microphone" in result.output


def test_duration_limit_trims_last_block_and_finishes():
    source = Source([block(np.zeros(1600)) for _ in range(30)])
    report = monitor(source, seconds=.25)
    assert report["duration_seconds"] == .25
    assert source.reads == 3
    assert report["event_count"] == 0


def test_interruption_produces_partial_report():
    source = Source([block(np.zeros(1600)), KeyboardInterrupt()], kind="microphone")
    report = monitor(source)
    assert report["duration_seconds"] == .1
    assert report["interrupted"] is True


def test_interrupt_while_printing_event_keeps_received_duration():
    rate = 16000
    t = np.arange(rate * 2) / rate

    def emit(row):
        if row["type"] == "detection" and row["event"] == "start":
            raise KeyboardInterrupt()

    report = monitor(Source([block(.1 * np.sin(2 * np.pi * 1500 * t))]), emit=emit)
    assert report["interrupted"] is True
    assert report["duration_seconds"] == 2
    assert report["event_count"] >= 1
    assert all(row["end_seconds"] <= report["duration_seconds"] for row in report["events"])


def test_capture_gap_cannot_join_short_tones_into_one_event():
    rate = 16000
    tone = .1 * np.sin(2 * np.pi * 1500 * np.arange(int(.3 * rate)) / rate)
    messages = []
    report = monitor(Source([block(tone), block(tone, gap=True)]), emit=messages.append,
                     min_duration=.5)
    assert report["capture_gaps"] == 1
    assert report["event_count"] == 0
    assert any(m["type"] == "gap" for m in messages)


def test_nonfinite_samples_and_empty_files_are_errors():
    with pytest.raises(ValueError, match="finite"):
        monitor(Source([block([float("nan")])]))
    with pytest.raises(ValueError, match="No audio"):
        monitor(Source([]))


def test_file_jsonl_and_saved_report_match(tmp_path):
    import soundfile as sf

    path = tmp_path / "tone.wav"
    rate = 16000
    t = np.arange(rate * 2) / rate
    audio = .1 * np.sin(2 * np.pi * 1437 * t)
    # Antiphase stereo must not cancel; the selected channel is analyzed alone.
    sf.write(path, np.column_stack([audio, -audio]), rate)
    output = tmp_path / "report.json"
    result = runner.invoke(app, ["audio", "detect", "howler", str(path),
                                 "--json", "--output", str(output)])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    report = json.loads(output.read_text())
    assert rows[0]["type"] == "session"
    assert rows[-1] == report
    assert report["event_count"] >= 1
    assert report["duration_seconds"] == 2
    assert any(abs(event["frequency_hz"] - 1437) < 20 for event in report["events"])


def test_live_command_closes_source_on_interrupt_and_writes_report(monkeypatch, tmp_path):
    from wcfi_tools.audio import detection_input

    closed = []

    @contextmanager
    def fake_microphone(**kwargs):
        try:
            yield Source([block(np.zeros(1600)), KeyboardInterrupt()], kind="microphone")
        finally:
            closed.append(True)

    monkeypatch.setattr(detection_input, "open_microphone", fake_microphone)
    output = tmp_path / "partial.json"
    result = runner.invoke(app, ["audio", "detect", "howler", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert closed == [True]
    assert json.loads(output.read_text())["interrupted"] is True
