"""Service lifecycle, raw input controls, authenticated API and measured outputs."""
import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.engine import EmulatorEngine
from wcfi_tools.audio.service import EmulatorServer, InstanceLock


class FakeNative:
    def __init__(self, _dll=None):
        self.active = False
        self.closed = False

    def set_active(self, value):
        self.active = value

    def close(self):
        self.closed = True


@pytest.fixture
def engine(tmp_path, monkeypatch, request):
    if hasattr(request, "param"):
        monkeypatch.setenv("WCFI_EMULATOR_INPUT_LEAD_FRAMES", str(request.param))
    else:
        monkeypatch.delenv("WCFI_EMULATOR_INPUT_LEAD_FRAMES", raising=False)
    result = EmulatorEngine(tmp_path, native_factory=FakeNative)
    yield result
    result.close()


def tone(engine, port=1, hz=220):
    return engine.configure(port, {"enabled": True, "seconds": 1,
        "source": {"kind": "tone", "frequency_hz": hz, "level_dbfs": -30}})


def settle(engine):
    for _ in range(40):
        block = engine.render(1024)
    return block


def test_availability_is_independent_of_sending_and_window(engine):
    assert not engine.snapshot()["running"]
    engine.start()
    assert engine.native.active and engine.snapshot()["running"]
    assert not engine.snapshot()["sending"]
    tone(engine)
    assert not np.any(engine.render(1024))
    engine.set_sending(True)
    assert np.max(np.abs(settle(engine))) == pytest.approx(10 ** (-30 / 20), rel=.001)
    engine.stop()
    assert not engine.native.active and not engine.snapshot()["running"]
    assert not engine.snapshot()["sending"]
    engine.start()
    assert engine.native.active and not engine.snapshot()["sending"]


def test_live_gain_mute_solo_and_atomic_invalid_edit(engine):
    tone(engine)
    tone(engine, 2, 330)
    engine.start()
    engine.set_sending(True)
    settle(engine)
    engine.configure(1, {"gain_db": -24})
    out = settle(engine)
    assert np.max(np.abs(out[:, 0])) == pytest.approx(10 ** (-54 / 20), rel=.001)
    engine.configure(2, {"solo": True})
    out = settle(engine)
    assert np.max(np.abs(out[:, 0])) < 1e-8
    assert np.max(np.abs(out[:, 1])) > .03
    engine.configure(2, {"muted": True})
    assert np.max(np.abs(settle(engine))) < 1e-8
    before = engine.snapshot()["channels"][0]
    with pytest.raises(ValueError):
        engine.configure(1, {"source": {"kind": "tone", "frequency_hz": float("nan")}})
    assert engine.snapshot()["channels"][0] == before


def test_settings_persist_but_streaming_does_not_resume(engine, tmp_path):
    tone(engine, 32, 880)
    engine.start()
    engine.set_sending(True)
    restored = EmulatorEngine(tmp_path, native_factory=FakeNative)
    try:
        assert restored.channels[31]["source"]["frequency_hz"] == 880
        assert not restored.sending and not restored.running
    finally:
        restored.close()


@pytest.mark.parametrize("engine", [8192, 16384], indirect=True)
def test_feeder_recovers_from_worker_stalls_without_increasing_lookahead(engine, monkeypatch):
    from wcfi_tools.audio import engine as module

    engine.start()
    target = engine.input_lookahead_frames
    queue = {"frames": target, "missing": 0, "maximum": target}
    engine.native.status = lambda: {
        "running": True, "host_pid": 123, "generation": 1, "service_active": True,
        "blocksize": 512, "input_queued": queue["frames"],
    }

    class SchedulingStalls:
        ticks = 0
        stopped = False

        def wait(self, _timeout):
            self.ticks += 1
            if self.stopped or self.ticks > 30:
                return True
            # A busy UI delays the producer by 40 ms, occasionally 80 ms.
            consumed = 3840 if self.ticks % 5 == 0 else 1920
            queue["missing"] += max(0, consumed - queue["frames"])
            queue["frames"] = max(0, queue["frames"] - consumed)
            return False

        def set(self):
            self.stopped = True

    class AudioBridge:
        generation, host_pid = 1, 123

        def __init__(self, *_args, **_kwargs):
            pass

        def discard_output(self):
            pass

        def check_session(self):
            pass

        def feed(self, audio):
            assert 0 < len(audio) <= 4096
            queue["frames"] += len(audio)
            queue["maximum"] = max(queue["maximum"], queue["frames"])

        def read(self, _frames):
            return np.empty((0, 32), np.float32)

        def close(self):
            pass

    engine.shutdown_event = SchedulingStalls()
    monkeypatch.setattr(module, "NativeBridge", AudioBridge)
    engine._audio()
    assert queue["missing"] == 0
    assert queue["maximum"] == target


def test_default_lookahead_and_constructor_only_env_read(engine, monkeypatch):
    before = engine.snapshot()
    assert before["input_lookahead_frames"] == 8192
    assert before["input_lookahead_ms"] == pytest.approx(170.6666667)
    monkeypatch.setenv("WCFI_EMULATOR_INPUT_LEAD_FRAMES", "16384")
    assert engine.snapshot()["input_lookahead_frames"] == 8192


@pytest.mark.parametrize("frames", [4096, 8192, 16384, 24576])
def test_configurable_lookahead_bounds_and_snapshot(tmp_path, monkeypatch, frames):
    monkeypatch.setenv("WCFI_EMULATOR_INPUT_LEAD_FRAMES", str(frames))
    instance = EmulatorEngine(tmp_path, native_factory=FakeNative)
    try:
        state = instance.snapshot()
        assert state["input_lookahead_frames"] == frames
        assert state["input_lookahead_ms"] == pytest.approx(frames / 48)
        assert not state["running"] and not state["sending"]
    finally:
        instance.close()


@pytest.mark.parametrize("invalid", ["", "4095", "24577", "8705", "16384.0", "garbage",
                                    "-8192", "32768"])
def test_invalid_lookahead_refused_before_start(tmp_path, monkeypatch, invalid):
    monkeypatch.setenv("WCFI_EMULATOR_INPUT_LEAD_FRAMES", invalid)
    with pytest.raises(ValueError, match="WCFI_EMULATOR_INPUT_LEAD_FRAMES"):
        EmulatorEngine(tmp_path, native_factory=FakeNative)


def test_source_switch_crossfades_from_the_old_source(engine):
    tone(engine)
    engine.start()
    engine.set_sending(True)
    settle(engine)
    old = engine.prepared[1]
    position = engine.position
    tone(engine, hz=1100)
    out = engine.render(1024)
    assert out[0, 0] == pytest.approx(old[position % len(old)], abs=1e-7)
    assert np.all(np.abs(out) <= 1)


def test_output_measurements_recover_known_tones_and_full_scale(engine):
    t = np.arange(48000) / 48000
    block = np.zeros((48000, 32), dtype=np.float32)
    block[:, 22] = .5 * np.sin(2 * np.pi * 1000 * t)
    block[:, 23] = block[:, 22]
    block[0, 23] = 1
    engine.receive(block)
    result = engine.analyze([23, 24])
    assert result["available"] and result["correlation"] > .99
    left, right = result["results"]
    assert left["peak_dbfs"] == pytest.approx(-6.0206, abs=.001)
    assert left["rms_dbfs"] == pytest.approx(-9.0309, abs=.001)
    assert left["dominant_hz"] == 1000
    assert right["clipped_samples"] == 1
    assert len(left["spectrum"]) == 95 and len(left["waveform"]) == 200
    engine.last_output -= 2
    assert not engine.analyze([23, 24])["available"]


def test_instance_lock_prevents_a_second_service(tmp_path):
    first = InstanceLock(tmp_path / "instance.lock")
    try:
        with pytest.raises(RuntimeError):
            InstanceLock(tmp_path / "instance.lock")
    finally:
        first.close()


def test_http_requires_session_token_and_origin_and_imports_stereo(engine):
    server = EmulatorServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, data=None, *, token=True, origin=None, raw=False):
        headers = {"Authorization": f"Bearer {server.token}"} if token else {}
        if origin:
            headers["Origin"] = origin
        body = data if raw else json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(server.origin + path, data=body, headers=headers)
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("/api/state", token=False)
        assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("/api/start", {}, origin="https://example.com")
        assert exc.value.code == 403
        assert len(request("/api/state")["channels"]) == 32
        assert request("/api/start", {})["running"]
        assert not request("/api/stop", {})["running"]
        buffer = io.BytesIO()
        audio = np.zeros((48000, 2), dtype=np.float32)
        audio[:, 1] = .1
        sf.write(buffer, audio, 48000, format="WAV", subtype="FLOAT")
        info = request("/api/import?name=stereo.wav", buffer.getvalue(), raw=True)
        assert info["channels"] == 2
        update = {"enabled": True, "seconds": 1, "source": {"kind": "file", "path": info["path"], "file_channel": 2}}
        assert request("/api/inputs/1", update)["source"]["file_channel"] == 2
        np.testing.assert_allclose(engine.prepared[1], .1)
        with pytest.raises(urllib.error.HTTPError):
            request("/api/inputs/1", {"source": {"kind": "file", "path": str(engine.state_dir / "secret.wav")}})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


NATIVE = Path(__file__).resolve().parents[1] / "native/wcfi_asio/build/v2"


def test_headless_cli_shares_service_state_and_keeps_restart_paused(engine, monkeypatch):
    from typer.testing import CliRunner

    from wcfi_tools.audio import client
    from wcfi_tools.cli import app

    server = EmulatorServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(client, "discovery", lambda: {
        "port": server.server_address[1], "token": server.token,
    })
    runner = CliRunner()

    def run(*arguments):
        result = runner.invoke(app, ["audio", "emulator", *arguments])
        assert result.exit_code == 0, result.output
        return result

    try:
        run("start")
        run("preset", "two-tones")
        assert engine.running and not engine.sending
        run("send")
        run("start")  # Repeated startup must not interrupt an existing feed.
        assert engine.sending
        assert engine.channels[1]["source"]["frequency_hz"] == 330
        invalid = runner.invoke(app, ["audio", "emulator", "preset", "unknown"])
        assert invalid.exit_code != 0 and engine.sending
        result = run("analyze", "--channels", "1,2")
        assert json.loads(result.output)["channels"] == [1, 2]
        run("pause")
        assert engine.running and not engine.sending
        run("send")
        run("stop")
        run("start")
        assert engine.running and not engine.sending
        assert engine.channels[0]["enabled"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.skipif(sys.platform != "win32" or not (NATIVE / "WcfiAsioTest.dll").exists(),
                    reason="Build isolated Windows ASIO binaries first")
def test_service_can_start_before_the_daw_and_measure_its_return(tmp_path):
    dll = NATIVE / "WcfiAsioTest.dll"
    engine = EmulatorEngine(tmp_path, dll=dll)
    process = None
    try:
        engine.start()
        engine.launch_workers()
        tone(engine)
        tone(engine, 2, 330)
        engine.set_sending(True)
        process = subprocess.Popen([str(NATIVE / "wcfi-asio-test.exe"), str(dll), "--external-service", "15"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = engine.analyze([23, 24])
            if result["available"] and all(r["peak_dbfs"] > -50 for r in result["results"]):
                break
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            time.sleep(.05)
        assert result["available"]
        assert result["results"][0]["dominant_hz"] == pytest.approx(220, abs=3)
        assert result["results"][1]["dominant_hz"] == pytest.approx(330, abs=3)
        engine.stop()
        time.sleep(1.2)
        result = engine.analyze([23, 24])
        assert result["available"]
        assert all(r["peak_dbfs"] == -120 for r in result["results"])
        engine.start()
        engine.set_sending(True)
        time.sleep(.3)
        assert engine.snapshot()["running"]
    finally:
        if process and process.poll() is None:
            process.terminate()
        if process:
            process.wait(timeout=5)
        engine.close()
