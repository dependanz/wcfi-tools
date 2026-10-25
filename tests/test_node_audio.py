import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest

from wcfi_tools.audio.analysis_nodes import Status
from wcfi_tools.audio.node_audio import HEADER, read_node_audio
from wcfi_tools.audio.service import EmulatorServer

IDENTITY = 'a' * 32


class AudioBridge:
    def __init__(self):
        self.end, self.epoch, self.age, self.channels = 100000, 3, 10, 2
        self.closed, self.cursors = [], []
        self.bad = False

    def open(self, identity):
        assert identity == IDENTITY
        return 1

    def close(self, handle):
        self.closed.append(handle)

    def clock_ns(self):
        return 1_000_000_000 + int(self.age * 1e6)

    def read(self, handle, cursor):
        self.cursors.append(cursor)
        first = min(self.end, max(cursor, self.end - 32768))
        samples = np.column_stack((np.arange(first, self.end, dtype=np.float32),
                                   -np.arange(first, self.end, dtype=np.float32)))
        if self.bad and len(samples):
            samples[-1] = [np.nan, np.inf]
        s = Status()
        s.sample_rate, s.channels, s.epoch, s.active = 48000, self.channels, self.epoch, 1
        s.first_frame, s.written_frame, s.callback_ns = first, self.end, 1_000_000_000
        return samples, s


def unpack(packet):
    header = HEADER.unpack_from(packet)
    audio = np.frombuffer(packet, dtype='<f4', offset=HEADER.size).reshape(-1, header[3])
    return header, audio


def test_initial_listen_starts_near_now_and_continues_without_replaying_samples():
    bridge = AudioBridge()
    header, audio = unpack(read_node_audio(IDENTITY, bridge_factory=lambda: bridge))
    assert HEADER.size == 48
    assert header[:5] == (b'WCFA', 1, 48000, 2, 3)
    assert header[5:7] == (94240, 100000) and header[-1] & 1
    np.testing.assert_array_equal(audio[:, 0], np.arange(94240, 100000))
    np.testing.assert_array_equal(audio[:, 1], -audio[:, 0])
    bridge.end += 512
    header, audio = unpack(read_node_audio(IDENTITY, cursor=100000, epoch=3, bridge_factory=lambda: bridge))
    assert header[5:7] == (100000, 100512) and not header[-1] & 1
    assert len(audio) == 512 and bridge.closed == [1, 1]


@pytest.mark.parametrize('cursor,epoch', [(1, 3), (999999, 3), (100000, 2)])
def test_overrun_counter_reset_and_new_epoch_discard_old_history(cursor, epoch):
    bridge = AudioBridge()
    header, audio = unpack(read_node_audio(IDENTITY, cursor=cursor, epoch=epoch, bridge_factory=lambda: bridge))
    assert header[-1] & 1 and header[5] == 94240
    assert len(audio) == 5760


def test_slow_client_packet_is_bounded_and_nonfinite_is_marked_not_amplified():
    bridge = AudioBridge()
    bridge.bad, bridge.channels = True, 1
    header, audio = unpack(read_node_audio(IDENTITY, cursor=80000, epoch=3, bridge_factory=lambda: bridge))
    assert len(audio) == 9600 and header[-1] == 3
    assert np.isfinite(audio).all() and audio[-1, 0] == 0
    # Finite floats above full scale stay unchanged; audition volume is downstream.
    assert audio[0, 0] > 1


def test_stale_node_is_refused_and_native_reader_is_closed():
    bridge = AudioBridge()
    bridge.age = 600
    with pytest.raises(RuntimeError, match='fresh'):
        read_node_audio(IDENTITY, bridge_factory=lambda: bridge)
    assert bridge.closed == [1]


def test_sequential_pull_batches_advancing_audio_without_a_browser_timer():
    class AdvancingBridge(AudioBridge):
        def read(self, handle, cursor):
            self.end += 512
            return super().read(handle, cursor)

    bridge = AdvancingBridge()
    header, audio = unpack(read_node_audio(IDENTITY, cursor=100000, epoch=3,
                                          bridge_factory=lambda: bridge))
    assert 1920 <= len(audio) <= 9600
    assert header[5] == 100000 and not header[-1] & 1
    np.testing.assert_array_equal(audio[:, 0], np.arange(100000, header[6]))


@pytest.mark.parametrize('arguments', [
    {'identity': '../private'}, {'identity': IDENTITY, 'cursor': -1, 'epoch': 3},
    {'identity': IDENTITY, 'cursor': 0}, {'identity': IDENTITY, 'cursor': 2**53, 'epoch': 3}])
def test_invalid_requests_never_open_a_native_reader(arguments):
    with pytest.raises(ValueError):
        read_node_audio(**arguments, bridge_factory=lambda: pytest.fail('Reader must not open'))


def test_pcm_endpoint_requires_the_same_local_authorization(monkeypatch):
    from wcfi_tools.audio import service
    payload = read_node_audio(IDENTITY, bridge_factory=AudioBridge)
    monkeypatch.setattr(service, 'read_node_audio', lambda *args, **kwargs: payload)
    server = EmulatorServer(SimpleNamespace())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        endpoint = server.origin + '/api/nodes/audio?id=' + IDENTITY
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(endpoint)
        assert error.value.code == 403
        req = urllib.request.Request(endpoint, headers={'Authorization': 'Bearer ' + server.token})
        with opener.open(req) as response:
            assert response.headers['Content-Type'] == 'application/octet-stream'
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.read() == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
