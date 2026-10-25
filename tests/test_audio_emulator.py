"""Known-fault fixtures and real native ASIO transport, with no DAW/hardware."""
import copy
import ctypes
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from wcfi_tools.audio.asio import NativeBridge, capture, mapped_block, stream
from wcfi_tools.audio.emulator import InputBank, load_profile, make_profile, render
from wcfi_tools.cli import app


def profile_file(tmp_path, data=None):
    data = copy.deepcopy(data or make_profile())
    data["seconds"] = 1
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_reproducible_faults_distinguish_upstream_clipping_from_low_level(tmp_path):
    profile = load_profile(profile_file(tmp_path))
    bank = InputBank(profile)
    baseline, _ = bank.prepare("baseline")
    quiet, _ = bank.prepare("quiet-vocal")
    clipped, truth = bank.prepare("clipped-vocal")
    np.testing.assert_allclose(quiet[0], baseline[0] * 10 ** (-24 / 20), atol=1e-8)
    event = truth[0]["faults"][0]
    assert event["clipped_sample_fraction"] > .5
    assert np.max(np.abs(clipped[0])) < .36  # damaged upstream, quiet at the ADC
    assert "upstream" in event["expected_response"]
    first, first_truth = bank.prepare("chaos")
    second, second_truth = bank.prepare("chaos")
    assert first_truth == second_truth
    for a, b in zip(first, second, strict=True):
        np.testing.assert_array_equal(a, b)
        assert np.isfinite(a).all() and np.max(np.abs(a)) <= 1


def test_real_file_channels_offset_resampling_and_hash(tmp_path):
    data = make_profile()
    data["channels"] = [data["channels"][0]]
    rate = 24000
    raw = np.linspace(-.5, .5, rate * 2, dtype=np.float32)
    sf.write(tmp_path / "voice.wav", raw, rate, subtype="FLOAT")
    data["channels"][0]["source"] = {"kind": "file", "path": "voice.wav", "start_seconds": .5}
    bank = InputBank(load_profile(profile_file(tmp_path, data)))
    stems, truth = bank.prepare("baseline")
    assert stems[0].shape == (48000, 1)
    assert abs(stems[0][1000, 0] - raw[12500]) < .002
    assert truth[0]["source"]["original_sample_rate"] == rate
    assert len(truth[0]["source"]["sha256"]) == 64
    assert "voice.wav" in truth[0]["source"]["path"]


@pytest.mark.parametrize("change", [
    lambda p: p["channels"][1].update(inputs=[1]),
    lambda p: p["channels"][0].update(inputs=[True]),
    lambda p: p.update(seconds=float("nan")),
    lambda p: p["channels"][0].update(source=[]),
    lambda p: p["channels"][0].update(faults=["clip"]),
])
def test_invalid_routing_and_faults_fail_before_device_access(tmp_path, change):
    data = make_profile()
    change(data)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_profile(path)


def test_stereo_ports_loop_together_and_nonloop_tail_is_silent():
    p = {"loop": True, "channels": [{"inputs": [31, 32]}, {"inputs": [2]}]}
    stems = [np.array([[1, -1], [.5, -.5]], dtype=np.float32),
             np.array([[.1], [.2]], dtype=np.float32)]
    block = mapped_block(p, stems, 1, 4)
    np.testing.assert_array_equal(block[:, 30:], [[.5, -.5], [1, -1], [.5, -.5], [1, -1]])
    np.testing.assert_allclose(block[:, 1], [.2, .1, .2, .1])
    assert np.count_nonzero(block[:, :1]) == 0
    p["loop"] = False
    block = mapped_block(p, stems, 1, 4)
    assert np.count_nonzero(block[1:]) == 0


def test_render_provenance_and_no_overwrite(tmp_path):
    path = profile_file(tmp_path)
    out = tmp_path / "rendered"
    report = render(path, out, scenario="guitar-noise")
    assert (out / "manifest.json").exists()
    guitar = next(t for t in report["tracks"] if t["name"] == "EGUITAR")
    assert [f["kind"] for f in guitar["faults"]] == ["hum", "noise"]
    assert sf.info(out / guitar["file"]).frames == 48000
    with pytest.raises(ValueError, match="new output"):
        render(path, out)


def test_audio_cli_init_and_scenarios_do_not_require_a_driver(tmp_path):
    runner = CliRunner()
    out = tmp_path / "cli.json"
    assert runner.invoke(app, ["audio", "emulate", "init", str(out)]).exit_code == 0
    assert runner.invoke(app, ["audio", "emulate", "init", str(out)]).exit_code == 1
    result = runner.invoke(app, ["audio", "emulate", "scenarios"])
    assert result.exit_code == 0 and "guitar-noise" in result.output


NATIVE = Path(__file__).resolve().parents[1] / "native" / "wcfi_asio" / "build"
if (NATIVE / "v2" / "WcfiAsioTest.dll").exists():
    NATIVE = NATIVE / "v2"


@pytest.mark.skipif(sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8
                    or not (NATIVE / "wcfi-asio-test.exe").exists(),
                    reason="Build the optional Windows x64 ASIO test host first")
def test_native_python_roundtrip_and_single_producer(tmp_path):
    """Native callback -> output 23/24 capture, while the Python feeder runs."""
    dll = NATIVE / "WcfiAsioTest.dll"
    if not dll.exists():
        pytest.skip("Build the isolated ASIO test DLL first")
    output = tmp_path / "captured.wav"
    path = profile_file(tmp_path)
    process = subprocess.Popen([str(NATIVE / "wcfi-asio-test.exe"), str(dll), "--serve", "10"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    errors, finished = [], threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            line = pool.submit(process.stdout.readline).result(timeout=5)
        assert line.strip() == "READY", line
        with NativeBridge(dll, roles=1):
            with pytest.raises(RuntimeError, match="already in use"):
                NativeBridge(dll, roles=1)
            with NativeBridge(dll, roles=2):
                pass

        def feed():
            try:
                stream(path, dll=dll, seconds=2.5, ready=lambda _: finished.set())
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(exc)
                finished.set()

        thread = threading.Thread(target=feed)
        thread.start()
        assert finished.wait(5)
        time.sleep(.1)  # Begin after producer startup, excluding explicit startup silence.
        report = capture(output, seconds=1, dll=dll)
        thread.join(timeout=5)
        assert not thread.is_alive() and not errors
        audio, rate = sf.read(output, dtype="float32", always_2d=True)
        assert rate == 48000 and audio.shape == (48000, 2)
        assert report["output_drops_during_capture"] == 0
        # Test host routes inputs 1/2 to outputs 23/24 at -6.02 dB.
        expected_peak = 10 ** (-24 / 20) * .5
        assert np.max(np.abs(audio[:, 0])) == pytest.approx(expected_peak, rel=.001)
        assert np.max(np.abs(audio[:, 1])) == pytest.approx(expected_peak, rel=.001)
        frequencies = np.fft.rfftfreq(len(audio), 1 / rate)
        for channel, expected in enumerate((220, 275)):
            peak = frequencies[np.argmax(np.abs(np.fft.rfft(audio[:, channel])))]
            assert peak == pytest.approx(expected, abs=1)
        # Device loss is surfaced, never an endless silent feed.
        with NativeBridge(dll) as bridge:
            process.terminate()
            process.wait(timeout=5)
            # A retained map alone cannot detect a crash; check the process handle.
            with pytest.raises(RuntimeError, match="host has exited"):
                bridge.check_session()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
