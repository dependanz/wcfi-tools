"""Waveform transport verification must not disguise missing/altered samples."""
from __future__ import annotations

import numpy as np
import pytest

from wcfi_tools.audio.trial_verification import find_alignment, measure_alignment

RATE = 8000


def reference(seconds=1.):
    rng = np.random.default_rng(1597)
    return (.08 * rng.normal(size=(int(RATE * seconds), 2))).astype('float32')


def capture(x, offset=2371, gain=.25):
    y = np.zeros((len(x) + 4000, x.shape[1]), dtype='float32')
    y[offset:offset + len(x)] = x * gain
    return y


@pytest.mark.parametrize('offset,gain', [(0, .25), (2371, .125), (4000, 3.), (61, 1.1)])
def test_known_offset_and_common_gain_preserve_stereo(offset, gain):
    x = reference()
    result = find_alignment(x, capture(x, offset, gain), RATE)
    assert result['accepted'], result
    assert result['start_frame'] == offset
    assert result['fitted_scalar'] == pytest.approx(gain, rel=1e-6)
    assert result['gain_db'] == pytest.approx(20 * np.log10(gain), abs=1e-6)
    assert result['normalized_residual_db'] < -140
    assert result['correlation'] >= .999999
    assert result['qualifying_start_frames'] == [offset]


def test_identical_array_has_fixed_zero_start_and_finite_exact_null():
    x = reference()
    result = find_alignment(x, x.copy(), RATE)
    assert result['accepted']
    assert result['start_frame'] == 0
    assert result['normalized_residual_db'] == -300


@pytest.mark.parametrize('corruption', ['swap', 'noise', 'dropout', 'left_gain', 'polarity'])
def test_transparent_verification_rejects_corrupted_capture(corruption):
    x = reference()
    y = capture(x)
    if corruption == 'swap':
        y = y[:, ::-1]
    elif corruption == 'noise':
        y += np.random.default_rng(77).normal(0, .0002, y.shape)
    elif corruption == 'dropout':
        y[3400:3420] = 0
    elif corruption == 'left_gain':
        y[:, 0] *= 1.1
    else:
        y *= -1
    result = find_alignment(x, y, RATE)
    assert not result['accepted'], result
    assert result['normalized_residual_db'] is not None


def test_single_dropped_sample_is_visible_in_short_window():
    x = reference(2.)
    y = x.copy()
    y[900] = 0
    result = measure_alignment(x, y, window_frames=800)
    assert not result['accepted']
    assert result['worst_window_residual_db'] > result['normalized_residual_db']


def test_no_channel_remapping_or_per_channel_fit():
    x = reference()
    y = x * np.array([.5, .25], dtype='float32')
    result = measure_alignment(x, y)
    assert not result['accepted']
    assert result['channel_fitted_scalars'] == pytest.approx([.5, .25])


def test_duplicate_complete_occurrences_are_ambiguous():
    x = reference(.4)
    y = np.concatenate([np.zeros((733, 2)), x, np.zeros((311, 2)), x,
                        np.zeros((900, 2))]).astype('float32')
    result = find_alignment(x, y, RATE)
    assert not result['accepted'], result
    assert 'ambiguous_contiguous_match' in result['reasons']
    assert result['qualifying_start_frames'] == [733, 733 + len(x) + 311]


def test_periodic_waveform_cannot_establish_unique_timing():
    t = np.arange(16000) / RATE
    y = np.stack([np.sin(2 * np.pi * 100 * t), np.cos(2 * np.pi * 200 * t)], axis=1)
    result = find_alignment(y[:8000], y, RATE)
    assert not result['accepted']
    assert 'ambiguous_contiguous_match' in result['reasons']


def test_missing_tail_and_clock_drift_do_not_get_time_warped():
    x = reference()
    short = find_alignment(x, x[:-1], RATE)
    assert not short['accepted']
    assert short['reasons'] == ['insufficient_complete_window']
    y = capture(x)
    y[5000:-1] = y[5001:]
    shifted = find_alignment(x, y, RATE)
    assert not shifted['accepted']


def test_silence_rejected_and_invalid_arrays_fail_explicitly():
    x = np.zeros((8000, 2), dtype='float32')
    result = find_alignment(x, x, RATE)
    assert result['reasons'] == ['silent_signal']
    with pytest.raises(ValueError, match='channel'):
        find_alignment(x, x[:, 0], RATE)
    x[0, 0] = np.nan
    with pytest.raises(ValueError, match='Finite'):
        measure_alignment(x, x)


def test_configurable_residual_gate_retains_unconditional_metrics():
    x = reference()
    y = x + np.random.default_rng(49).normal(0, .00005, x.shape)
    strict = measure_alignment(x, y, max_residual_db=-80)
    loose = measure_alignment(x, y, max_residual_db=-50)
    assert not strict['accepted']
    assert loose['accepted']
    assert strict['normalized_residual_db'] == loose['normalized_residual_db']


def test_no_gain_search_can_hide_silence_or_inversion():
    x = reference()
    assert not measure_alignment(x, x * 0)['accepted']
    assert not measure_alignment(x, -x)['accepted']
    assert measure_alignment(x, x * 1e-5)['accepted']


def test_bounded_input_duration():
    x = reference()
    with pytest.raises(ValueError, match='maximum duration'):
        find_alignment(x, capture(x), RATE, maximum_seconds=1)
