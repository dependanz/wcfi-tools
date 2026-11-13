import ctypes
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from wcfi_tools.audio.analysis_nodes import AnalysisNodes, Status, Stream, waveform_envelope
from wcfi_tools.audio.client import request
from wcfi_tools.audio.service import EmulatorServer


def status(*, frame=0, first=0, epoch=1, host=None, position=0):
    result = Status()
    result.version, result.pid, result.channels = 1, 123, 1
    result.block_size, result.sample_rate = 480, 48000
    result.written_frame, result.first_frame, result.epoch = frame, first, epoch
    result.flags, result.active, result.position = 1, 1, position
    result.host_sample = frame - 480 if host is None else host
    result.callback_ns = 1_000_000_000
    result.label = b"RED"
    return result


def test_wire_layout_and_restart_do_not_splice_old_audio():
    assert ctypes.sizeof(Status) == 208
    stream = Stream(1)
    stream.append(np.full((480, 2), .1, np.float32), status(frame=480))
    stream.append(np.full((480, 2), .2, np.float32), status(frame=960, first=480))
    assert stream.clock_valid
    assert stream.count == 960
    stream.append(np.full((480, 2), .3, np.float32), status(frame=480, epoch=2))
    assert stream.count == 480 and not stream.clock_valid
    assert np.allclose(stream.recent(960), .3)
    stream.append(np.full((480, 2), .4, np.float32), status(frame=2400, first=1920, epoch=2))
    assert stream.gaps == 1440 and stream.count == 480


def test_alignment_uses_host_positions_and_refuses_unreliable_clocks():
    source = np.random.default_rng(3).normal(0, .1, 96000).astype(np.float32)
    before, after = Stream(1), Stream(2)
    target = np.roll(source, 960) * .3
    for end in range(480, len(source) + 1, 480):
        before.append(np.repeat(source[end-480:end, None], 2, axis=1), status(frame=end, first=end-480))
        after.append(np.repeat(target[end-480:end, None], 2, axis=1), status(frame=end, first=end-480, position=1))
    result = AnalysisNodes.align(before, after)
    assert result["available"] and result["lag_ms"] == pytest.approx(20, abs=.18)
    # One node can be polled one callback later without inventing extra delay.
    before.append(np.zeros((480, 2), np.float32), status(frame=96480, first=96000))
    result = AnalysisNodes.align(before, after)
    assert result["available"] and result["lag_ms"] == pytest.approx(20, abs=.18)
    after.clock_valid = False
    assert not AnalysisNodes.align(before, after)["available"]


def test_recent_audio_stays_ordered_across_ring_wrap_and_returns_an_independent_copy():
    stream = Stream(1)
    s = status()
    s.sample_rate = 8000
    for start in range(0, 30000, 3000):
        block = np.repeat(np.arange(start, start + 3000, dtype=np.float32)[:, None], 2, axis=1)
        s = status(frame=start + 3000, first=start)
        s.sample_rate = 8000
        stream.append(block, s)
    for frames in (0, 1000, 9000, 24000, 30000):
        recent = stream.recent(frames)
        expected = np.arange(30000 - min(frames, 24000), 30000, dtype=np.float32)
        np.testing.assert_array_equal(recent[:, 0], expected)
    recent[:] = 0
    assert stream.recent(1)[0, 0] == 29999


@pytest.mark.parametrize("frames", [0, 1, 59, 60, 61, 4000, 24000])
def test_waveform_optimization_preserves_all_displayed_extrema(frames):
    wave = np.random.default_rng(19).normal(0, .1, (frames, 2)).astype(np.float32)
    envelope = waveform_envelope(wave)
    # Preserve the established API's bin boundaries, including uneven and empty bins.
    bins = np.array_split(wave, 60)
    for ch in (0, 1):
        for actual, bucket in zip(envelope[ch], bins, strict=True):
            assert actual == {"min": float(np.min(bucket[:, ch])) if len(bucket) else 0,
                              "max": float(np.max(bucket[:, ch])) if len(bucket) else 0}


class FakeBridge:
    def __init__(self):
        self.frame = 480
        self.epoch = 1
        self.closed = []

    def discover(self):
        return ["a"]

    def open(self, _):
        return 1

    def read(self, _, cursor):
        first = min(cursor, self.frame)
        return np.full((self.frame-first, 2), .1, np.float32), status(frame=self.frame, first=first, epoch=self.epoch)

    def close(self, handle):
        self.closed.append(handle)

    def clock_ns(self):
        return 1_020_000_000


def test_collector_no_driver_dependency_and_passive_endpoint():
    bridge = FakeBridge()
    nodes = AnalysisNodes(lambda: bridge)
    nodes.tick()
    view = nodes.snapshot()
    assert view["available"] and len(view["streams"]) == 1
    assert view["streams"][0]["peak_dbfs"][0] == pytest.approx(-20)
    assert view["streams"][0]["capture_age_ms"] == pytest.approx(20)
    assert len(view["streams"][0]["waveform"][0]) == 60
    view["streams"][0]["label"] = "client edit"
    assert nodes.snapshot()["streams"][0]["label"] == "RED"
    bridge.epoch, bridge.frame = 2, 240
    nodes.tick()
    assert nodes.streams["a"].cursor == 240 and nodes.streams["a"].epoch == 2
    server = EmulatorServer(SimpleNamespace(nodes=nodes))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    descriptor = {"port": server.server_port, "token": server.token}
    try:
        assert request("GET", "/api/nodes", descriptor=descriptor)["available"]
        with pytest.raises(RuntimeError, match="authorization"):
            request("GET", "/api/nodes", descriptor={**descriptor, "token": "wrong"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        nodes.close()
    assert bridge.closed == [1]
