"""Recorded stems must share sample positions across wraps, mutes and restarts."""
import hashlib
import json
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.client import request
from wcfi_tools.audio.engine import EmulatorEngine
from wcfi_tools.audio.service import EmulatorServer
from wcfi_tools.audio.takes import RecordedTake, catalog


class FakeNative:
    def __init__(self, _dll=None):
        self.active = False

    def set_active(self, value):
        self.active = value

    def close(self):
        pass


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    folder = root / "sunday-test"
    originals = folder / "originals"
    originals.mkdir(parents=True)
    frames = 24000
    ramp = (.05 + .1 * np.arange(frames) / frames).astype(np.float32)
    signals = [ramp[:, None], np.column_stack((-ramp, ramp * 2))]
    sources = []
    for name, ports, signal in zip(("RED", "KEYS"), ([1], [13, 14]), signals):
        path = originals / f"{name}.wav"
        sf.write(path, signal, 48000, subtype="FLOAT")
        sources.append({"track_name": name, "emulator_inputs": ports,
            "channels": len(ports), "frames": frames, "sample_rate": 48000, "subtype": "FLOAT",
            "file": f"originals/{name}.wav", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    data = {"schema_version": 1, "kind": "wcfi_real_source_take", "take_id": folder.name,
            "sample_rate": 48000, "frames": frames, "sources": sources}
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return root, folder, data, signals


@pytest.fixture
def engine(tmp_path, library):
    result = EmulatorEngine(tmp_path / "state", native_factory=FakeNative, library_root=library[0])
    yield result
    result.close()


def test_stereo_and_mono_wrap_on_the_same_source_samples(library):
    root, _, _, signals = library
    assert catalog(root)[0]["input_channel_count"] == 3
    with closing(RecordedTake(root, "sunday-test", start_frame=4800, end_frame=14400, fade_ms=0)) as reader:
        # Uneven chunks cross the boundary more than once.
        actual = np.concatenate([reader.read(n) for n in (1023, 8192, 17003, 511)])
        indexes = 4800 + np.arange(len(actual)) % 9600
        np.testing.assert_array_equal(actual[:, 0], signals[0][indexes, 0])
        np.testing.assert_array_equal(actual[:, 12:14], signals[1][indexes])
        assert not np.any(actual[:, 1:12])
        assert reader.cursor == 4800 + len(actual) % 9600


def test_boundary_fades_use_the_same_envelope_without_changing_loop_length(library):
    root, _, _, _ = library
    with closing(RecordedTake(root, "sunday-test", start_frame=4800, end_frame=9600, fade_ms=5)) as reader:
        actual = reader.read(4800)
        assert reader.cursor == 4800
        assert np.all(actual[[0, -1]] == 0)
        np.testing.assert_array_equal(actual[:, 12], -actual[:, 0])
        np.testing.assert_array_equal(actual[:, 13], actual[:, 0] * 2)
        assert np.min(actual[240:-240, 0]) > .05


def test_disk_decoding_is_bounded_by_requested_frames(library, monkeypatch):
    calls = []
    read = sf.SoundFile.read

    def measured(self, frames=-1, *args, **kwargs):
        calls.append(frames)
        return read(self, frames, *args, **kwargs)

    monkeypatch.setattr(sf.SoundFile, "read", measured)
    with closing(RecordedTake(library[0], "sunday-test")) as reader:
        assert calls == []  # Loading hashes bytes but does not decode the full recording.
        reader.read(1024)
        assert calls == [1024, 1024]


@pytest.mark.parametrize("problem", ["duplicate", "outside", "rate", "length", "offset", "checksum"])
def test_invalid_take_cannot_replace_or_interrupt_existing_inputs(engine, library, problem):
    _, folder, data, _ = library
    engine.configure(1, {"enabled": True, "source": {"kind": "tone", "frequency_hz": 440}})
    engine.start()
    engine.set_sending(True)
    before = engine.snapshot()["channels"]
    if problem == "duplicate":
        data["sources"][1]["emulator_inputs"] = [1, 14]
    elif problem == "outside":
        data["sources"][0]["file"] = "../outside.wav"
    elif problem == "rate":
        data["sources"][0]["sample_rate"] = 44100
    elif problem == "length":
        data["sources"][0]["frames"] -= 1
    elif problem == "offset":
        data["sources"][0]["source_start_relative_beat"] = 1
    else:
        data["sources"][0]["sha256"] = "0" * 64
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        engine.load_take("sunday-test")
    assert engine.sending and engine.take is None
    assert engine.snapshot()["channels"] == before


def test_mapping_pause_mute_and_resume_keep_one_playhead(engine):
    engine.load_take("sunday-test", start_frame=4800, end_frame=14400, fade_ms=0)
    state = engine.snapshot()
    assert [c["id"] for c in state["channels"] if c["enabled"]] == [1, 13, 14]
    assert state["channels"][12]["name"] == "KEYS L"
    assert state["channels"][13]["source"]["file_channel"] == 2
    assert not state["sending"]
    engine.render(512)
    assert engine.take.cursor == 4800
    engine.start()
    engine.set_sending(True)
    engine.render(512)
    engine.configure(13, {"muted": True})
    engine.configure(1, {"enabled": False})
    engine.render(512)
    assert engine.take.cursor == 5824
    engine.set_sending(False)
    assert not np.any(engine.render(1024))
    assert engine.take.cursor == 5824
    engine.configure(13, {"muted": False})
    engine.configure(1, {"enabled": True})
    engine.set_sending(True)
    block = engine.render(512)
    # With identical gain ramps, both stereo sides and RED are the same sample index.
    np.testing.assert_allclose(block[:, 12], -block[:, 0], atol=1e-8)
    np.testing.assert_allclose(block[:, 13], 2 * block[:, 0], atol=1e-8)
    assert engine.take.cursor == 6336
    before = engine.take.snapshot()
    with pytest.raises(ValueError):
        engine.set_take_loop(14400, 4800)
    assert engine.sending and engine.take.snapshot() == before
    with pytest.raises(ValueError):
        engine.configure(1, {"source": {"kind": "noise"}})
    engine.set_take_loop(9600, 19200, 5)
    assert not engine.sending and engine.take.cursor == 9600


def test_take_settings_restore_paused_and_unload_recovers_previous_sources(engine, library):
    engine.configure(1, {"gain_db": -8, "source": {"kind": "tone", "frequency_hz": 440}})
    manual = engine.snapshot()["channels"]
    engine.load_take("sunday-test")
    engine.configure(13, {"gain_db": -3, "muted": True})
    engine.set_take_loop(4800, 14400, 0)
    engine.start()
    engine.set_sending(True)
    engine.render(1024)
    with closing(EmulatorEngine(engine.state_dir, native_factory=FakeNative, library_root=library[0])) as restored:
        state = restored.snapshot()
        assert state["take"]["position_frame"] == 4800
        assert not state["sending"] and not state["running"]
        assert state["channels"][12]["gain_db"] == -3 and state["channels"][12]["muted"]
        restored.unload_take()
        assert restored.snapshot()["channels"] == manual
        assert restored.take is None


def test_missing_assets_restore_manual_setup_with_visible_warning(engine, library):
    engine.configure(1, {"gain_db": -8})
    engine.load_take("sunday-test")
    engine.close()
    (library[1] / "originals/RED.wav").unlink()
    with closing(EmulatorEngine(engine.state_dir, native_factory=FakeNative, library_root=library[0])) as restored:
        restored.start()
        assert restored.take is None and not restored.sending
        assert restored.channels[0]["gain_db"] == -8
        assert "unavailable" in restored.snapshot()["warning"]


def test_failed_state_write_rolls_back_range_and_playback(engine, monkeypatch):
    engine.load_take("sunday-test")
    engine.start()
    engine.set_sending(True)
    engine.render(1024)
    previous = engine.take.snapshot()

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(engine, "_save", fail)
    with pytest.raises(OSError):
        engine.set_take_loop(4800, 14400)
    assert engine.sending and engine.take.snapshot() == previous
    with pytest.raises(OSError):
        engine.unload_take()
    assert engine.sending and engine.take.snapshot() == previous


def test_incomplete_audio_pauses_the_whole_take(engine, monkeypatch):
    engine.load_take("sunday-test")
    monkeypatch.setattr(engine.take.files[0][0], "read", lambda count, **_: np.zeros((count - 1, 1)))
    engine.start()
    engine.set_sending(True)
    assert not np.any(engine.render(1024))
    assert not engine.sending and "incomplete" in engine.error


def test_take_api_maps_inputs_and_updates_one_shared_range(engine):
    server = EmulatorServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    descriptor = {"port": server.server_port, "token": server.token}

    def call(method, path, data=None):
        return request(method, path, data, descriptor=descriptor)

    try:
        assert call("GET", "/api/takes")["takes"][0]["id"] == "sunday-test"
        state = call("POST", "/api/take/load", {"take_id": "sunday-test"})
        assert state["take"]["mapped_inputs"] == [1, 13, 14] and not state["sending"]
        state = call("POST", "/api/take/loop", {"start_frame": 4800, "end_frame": 14400, "fade_ms": 0})
        assert state["take"]["position_frame"] == 4800
        assert call("POST", "/api/take/restart")["take"]["position_frame"] == 4800
        with pytest.raises(RuntimeError):
            call("POST", "/api/take/load", {"take_id": "../outside"})
        assert call("POST", "/api/take/unload")["take"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


NATIVE = Path(__file__).resolve().parents[1] / "native/wcfi_asio/build/v2"


@pytest.mark.skipif(sys.platform != "win32" or not (NATIVE / "WcfiAsioTest.dll").exists(),
                    reason="Build isolated Windows ASIO binaries first")
def test_recorded_stereo_stays_aligned_through_native_asio(library, tmp_path):
    engine = EmulatorEngine(tmp_path / "native-state", dll=NATIVE / "WcfiAsioTest.dll", library_root=library[0])
    process = None
    try:
        engine.load_take("sunday-test", start_frame=4800, end_frame=14400, fade_ms=5)
        engine.start()
        engine.launch_workers()
        engine.set_sending(True)
        process = subprocess.Popen([str(NATIVE / "wcfi-asio-test.exe"), str(NATIVE / "WcfiAsioTest.dll"),
            "--external-service", "8"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            measured = engine.analyze([13, 14])
            if measured["available"] and measured["results"][0]["peak_dbfs"] > -40:
                break
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            time.sleep(.05)
        assert measured["available"] and measured["correlation"] == pytest.approx(-1)
        with engine.lock:
            history = engine.history.copy()
        assert np.max(np.abs(history[:, 12])) > .02
        np.testing.assert_allclose(history[:, 13], -2 * history[:, 12], atol=1e-7)
        engine.set_sending(False)
        time.sleep(.2)
        cursor = engine.take.cursor
        time.sleep(.1)
        assert engine.take.cursor == cursor
    finally:
        if process and process.poll() is None:
            process.terminate()
        if process:
            process.wait(timeout=5)
        engine.close()
