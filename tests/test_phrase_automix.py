"""Causal framing and unchanged native gain-stage integration; no playback."""
import os
from pathlib import Path

import numpy as np
import pytest

from wcfi_tools.audio.phrase_automix import PhraseControlDsp


class FakeNative:
    def __init__(self, _path, settings, **_format):
        self.settings = settings
        self.count = 0
        self.configurations = [settings]

    def configure(self, settings):
        self.settings = settings
        self.configurations.append(settings)

    def process(self, values):
        self.count += len(values)
        return (values * 10 ** (self.settings.trim_db / 20)).astype(np.float32)

    def status(self):
        return {"samples_processed": self.count}

    def close(self):
        pass


def render(values, partitions, *, native_factory=FakeNative, dll_path="unused", rate=8000):
    outputs, decisions = [], []
    with PhraseControlDsp(dll_path, rate=rate, channels=values.shape[1],
                          native_factory=native_factory) as dsp:
        start = index = 0
        while start < len(values):
            count = min(partitions[index % len(partitions)], len(values) - start)
            outputs.append(dsp.process(values[start:start + count]))
            decisions.extend(dsp.decisions)
            start += count
            index += 1
        status = dsp.status()
    return np.concatenate(outputs), decisions, status


def test_completed_bins_apply_only_to_following_samples_and_partial_bin_is_not_evidence():
    values = np.full((800 * 6 + 17, 1), 10 ** (-50 / 20), np.float32)
    output, decisions, status = render(values, [len(values)])
    np.testing.assert_array_equal(output[:4000], values[:4000] * np.float32(10 ** (18 / 20)))
    np.testing.assert_allclose(output[4000:4800], values[4000:4800] * 10 ** (18.1 / 20), rtol=1e-7)
    assert len(decisions) == 6 and status['partial_bin_frames'] == 17
    assert status['policy_offset_db'] == pytest.approx(.2)


def test_partition_and_future_independence_with_partial_final_bin():
    levels = [-80] * 12 + [-50] * 30 + [-38] * 2 + [-50] * 20 + [-80] * 12
    values = np.repeat(np.asarray([10 ** (x / 20) for x in levels], np.float32), 800)[:, None]
    values = np.concatenate([values, np.full((317, 1), .002, np.float32)])
    whole = render(values, [len(values)])
    partitioned = render(values, [1, 137, 800, 2003, 51])
    np.testing.assert_array_equal(whole[0], partitioned[0])
    assert whole[1:] == partitioned[1:]
    boundary = 800 * 42 + 17
    altered = values.copy(); altered[boundary:] = .1
    different_future = render(altered, [113, 1409])
    np.testing.assert_array_equal(whole[0][:boundary], different_future[0][:boundary])
    assert whole[1][:42] == different_future[1][:42]


def test_native_settings_have_zero_internal_auto_and_invalid_block_is_transactional():
    with PhraseControlDsp('unused', native_factory=FakeNative, rate=8000, channels=1) as dsp:
        dsp.process(np.full((4000, 1), .003, np.float32))
        before = dsp.status()
        with pytest.raises(ValueError):
            dsp.process(np.asarray([[.1], [np.nan]], np.float32))
        assert dsp.status() == before
        assert all(x.auto_enabled and x.max_boost_db == x.max_cut_db == 0
                   for x in dsp._native.configurations)
        assert dsp.status()['native']['samples_processed'] == 4000


@pytest.mark.skipif(not os.environ.get('WCFI_PHRASE_TEST_DLL'), reason='Explicit offline DLL required')
def test_real_native_stage_keeps_guard_smoothing_and_partition_continuity():
    from wcfi_tools.audio.lapel_automix import NativeControlDsp
    dll = Path(os.environ['WCFI_PHRASE_TEST_DLL']).resolve(strict=True)
    values = np.concatenate([np.full((16000, 2), .003, np.float32),
                             np.full((8000, 2), .9, np.float32),
                             np.zeros((8000, 2), np.float32),
                             np.full((16017, 2), .003, np.float32)])
    a = render(values, [800], native_factory=NativeControlDsp, dll_path=dll)
    b = render(values, [1, 113, 4096], native_factory=NativeControlDsp, dll_path=dll)
    np.testing.assert_array_equal(a[0], b[0])
    assert a[1:] == b[1:]
    assert np.isfinite(a[0]).all()
    assert np.max(np.abs(a[0])) <= 10 ** (-1 / 20) + 1e-7
    native = a[2]['native']
    assert native['auto_gain_db'] == 0 and native['samples_processed'] == len(values)
    assert native['headroom_restraints'] > 0
    # Repeated settings must not restart manual smoothing at each 100ms boundary.
    boundary = 800
    assert abs(float(a[0][boundary, 0] - a[0][boundary - 1, 0])) < 1e-5
