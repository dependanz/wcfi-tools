"""File fixtures and fake capture only; these tests never open a microphone."""
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio import detection_input as inputs


class FakeSoundDevice:
    class PortAudioError(Exception):
        pass

    def __init__(self):
        self.default = SimpleNamespace(device=(1, 0))
        self.devices = [
            {"index": 0, "name": "Speakers", "max_input_channels": 0,
             "default_samplerate": 48000, "hostapi": 0},
            {"index": 1, "name": "Built-in mic", "max_input_channels": 2,
             "default_samplerate": 8000, "hostapi": 0},
        ]
        self.streams = []

    def query_devices(self, device=None, kind=None):
        if kind == "input":
            return self.devices[1]
        return self.devices

    def query_hostapis(self, index=None):
        host = {"name": "Fake host"}
        return [host] if index is None else host

    def check_input_settings(self, **kwargs):
        self.checked = kwargs

    def InputStream(self, **kwargs):
        stream = SimpleNamespace(**kwargs, active=False, aborted=False, closed=False)
        stream.start = lambda: setattr(stream, "active", True)
        stream.abort = lambda **kw: setattr(stream, "aborted", True)
        stream.close = lambda **kw: setattr(stream, "closed", True)
        self.streams.append(stream)
        return stream


@pytest.fixture
def fake_device(monkeypatch):
    fake = FakeSoundDevice()
    original = inputs._dependency
    monkeypatch.setattr(inputs, "_dependency",
                        lambda name: fake if name == "sounddevice" else original(name))
    return fake


def test_lists_only_inputs_without_opening_them(fake_device):
    assert inputs.list_input_devices() == [{"index": 1, "name": "Built-in mic",
        "hostapi": "Fake host", "default": True, "sample_rate": 8000., "channels": 2}]
    assert not fake_device.streams


def test_mic_resolves_device_uses_native_rate_and_copies_selected_channel(fake_device):
    with inputs.open_microphone(device="Built-in", channel=2) as source:
        stream = fake_device.streams[-1]
        assert stream.device == 1 and stream.samplerate == 8000
        assert source.info["channel"] == 2
        data = np.array([[.3, -.3], [.6, -.6]], dtype=np.float32)
        stream.callback(data, len(data), None, False)
        data[:] = 0  # PortAudio reuses its callback buffers.
        block = source.read()
        np.testing.assert_allclose(block.samples, [-.3, -.6])
        assert block.gap is False
    assert stream.aborted and stream.closed


def test_capture_backlog_is_bounded_and_reports_a_gap(fake_device):
    with inputs.open_microphone() as source:
        stream = fake_device.streams[-1]
        # This configuration permits one queued block, not an entire recording.
        for value in (.1, .2, .3):
            stream.callback(np.full((2048, 1), value, dtype=np.float32), 2048, None, False)
        block = source.read()
        assert block.gap
        np.testing.assert_allclose(block.samples, .3)


def test_portaudio_overflow_is_reported(fake_device):
    with inputs.open_microphone() as source:
        fake_device.streams[-1].callback(np.ones((10, 1)), 10, None, True)
        assert source.read().gap


def test_stale_capture_is_discarded_and_breaks_continuity(monkeypatch):
    blocks = Queue()
    blocks.put((98., inputs.AudioBlock(np.ones(10))))
    blocks.put((100., inputs.AudioBlock(np.zeros(10))))
    monkeypatch.setattr(inputs.time, "monotonic", lambda: 100.)
    reader = inputs._MicrophoneReader(SimpleNamespace(active=True), blocks, 3.)
    block = reader()
    assert block.gap and np.all(block.samples == 0)


def test_timeout_and_disconnect_are_errors_and_close_capture(fake_device):
    with (pytest.raises(RuntimeError, match="timed out"),
          inputs.open_microphone(timeout=.01) as source):
        source.read()
    assert fake_device.streams[-1].closed
    with (pytest.raises(RuntimeError, match="disconnected"),
          inputs.open_microphone(timeout=.2) as source):
        fake_device.streams[-1].active = False
        source.read()
    assert fake_device.streams[-1].closed


def test_interrupt_closes_capture(fake_device):
    with pytest.raises(KeyboardInterrupt), inputs.open_microphone():
        raise KeyboardInterrupt
    assert fake_device.streams[-1].aborted and fake_device.streams[-1].closed


def test_invalid_channel_does_not_open_capture(fake_device):
    with pytest.raises(ValueError, match="requested 3"), inputs.open_microphone(channel=3):
        pass
    assert not fake_device.streams


def test_file_preserves_opposing_channels_and_reads_to_eof(tmp_path):
    path = tmp_path / "opposing.wav"
    left = np.linspace(-.5, .5, 33, dtype=np.float32)
    sf.write(path, np.column_stack((left, -left)), 16000, subtype="FLOAT")
    for channel, expected in ((1, left), (2, -left)):
        with inputs.open_audio_file(path, channel=channel, blocksize=10) as source:
            assert source.info["sample_rate"] == 16000
            assert source.info["frames"] == 33
            chunks = []
            while (block := source.read()) is not None:
                assert not block.gap
                chunks.append(block.samples)
            np.testing.assert_allclose(np.concatenate(chunks), expected)


def test_ffmpeg_fallback_preserves_channels_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "phone recording.m4a"
    path.write_bytes(b"fake encoded file")
    decoded_paths = []
    temporary_directory = inputs.TemporaryDirectory
    monkeypatch.setattr(inputs, "TemporaryDirectory",
                        lambda **kwargs: temporary_directory(dir=tmp_path, **kwargs))
    monkeypatch.setattr(inputs.shutil, "which", lambda name: "ffmpeg")

    def decode(command, **kwargs):
        assert command[command.index("-i") + 1] == str(path.resolve())
        assert "-ac" not in command and "-ar" not in command
        decoded = Path(command[-1])
        decoded_paths.append(decoded)
        sf.write(decoded, np.array([[.2, -.2]], dtype=np.float32), 44100, subtype="FLOAT")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(inputs.subprocess, "run", decode)
    with inputs.open_audio_file(path, channel=2) as source:
        assert source.info["decoder"] == "ffmpeg"
        np.testing.assert_allclose(source.read().samples, [-.2])
        assert decoded_paths[0].exists()
    assert not decoded_paths[0].parent.exists()


def test_decode_failure_cleans_temporary_directory(tmp_path, monkeypatch):
    path = tmp_path / "bad.m4a"
    path.write_bytes(b"bad")
    decoded_paths = []
    temporary_directory = inputs.TemporaryDirectory
    monkeypatch.setattr(inputs, "TemporaryDirectory",
                        lambda **kwargs: temporary_directory(dir=tmp_path, **kwargs))
    monkeypatch.setattr(inputs.shutil, "which", lambda name: "ffmpeg")

    def fail(command, **kwargs):
        decoded_paths.append(Path(command[-1]))
        return SimpleNamespace(returncode=1, stderr="invalid audio")

    monkeypatch.setattr(inputs.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="invalid audio"), inputs.open_audio_file(path):
        pass
    assert not decoded_paths[0].parent.exists()


def test_unsupported_file_without_ffmpeg_has_actionable_error(tmp_path, monkeypatch):
    path = tmp_path / "phone.m4a"
    path.write_bytes(b"encoded")
    monkeypatch.setattr(inputs.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Install FFmpeg"), inputs.open_audio_file(path):
        pass
