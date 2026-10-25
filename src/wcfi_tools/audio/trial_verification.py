"""CPU-only contiguous waveform checks for emulator/DAW loop trials.

These checks establish signal identity up to one common positive gain, not mix
quality. Channels retain their order. There is no resampling to repair drift,
channel permutation, time warping, playback, file access or device control.
"""
from __future__ import annotations

import math


def _array(audio):
    import numpy as np

    x = np.asarray(audio)
    if x.ndim == 1:
        x = x[:, None]
    if (x.ndim != 2 or x.shape[1] not in (1, 2) or x.dtype.kind not in 'fiu'
            or len(x) < 64 or not np.isfinite(x).all()):
        raise ValueError('Finite mono/stereo PCM arrays with at least 64 frames are required')
    return x


def _db(value):
    # A finite floor keeps reports strict-JSON serializable, including exact nulls.
    return 20 * math.log10(max(float(value), 1e-15))


def measure_alignment(reference, captured, *, min_correlation=.999,
                      max_residual_db=-60., window_frames=4800):
    """Measure equal-length, same-time PCM using one gain for all channels.

    ``fitted_scalar`` maps reference to captured. Residuals are relative to
    captured energy. The worst non-silent short-window residual also must pass;
    this prevents a short dropout being diluted by a long recording. Absolute
    peaks and per-channel gains remain diagnostics, not separate fit controls.
    No lag search is performed here. A transparent common gain can be very
    large or small: the caller must check the intended gain separately.
    """
    import numpy as np

    x, y = _array(reference), _array(captured)
    if x.shape != y.shape:
        raise ValueError('Same-time arrays must have identical frame and channel counts')
    if (not 0 < min_correlation <= 1 or not math.isfinite(max_residual_db)
            or not isinstance(window_frames, int) or window_frames < 1):
        raise ValueError('Invalid verification thresholds or window size')
    xx = yy = xy = 0.
    channel_xx = np.zeros(x.shape[1], dtype='float64')
    channel_xy = channel_xx.copy()
    for first in range(0, len(x), window_frames):
        a = x[first:first + window_frames].astype('float64', copy=False)
        b = y[first:first + window_frames].astype('float64', copy=False)
        channel_xx += np.sum(a * a, axis=0)
        channel_xy += np.sum(a * b, axis=0)
        xx += float(np.sum(a * a))
        yy += float(np.sum(b * b))
        xy += float(np.sum(a * b))
    if xx <= 1e-24 or yy <= 1e-24:
        return {'status': 'rejected', 'accepted': False, 'reasons': ['silent_signal'],
                'frames': len(x), 'channels': x.shape[1], 'fitted_scalar': None,
                'gain_db': None, 'correlation': None, 'normalized_residual_db': None,
                'worst_window_residual_db': None}
    scalar = xy / xx
    correlation = max(-1., min(1., xy / math.sqrt(xx * yy)))
    error = 0.
    peak_error = 0.
    worst = -300.
    for first in range(0, len(x), window_frames):
        a = x[first:first + window_frames].astype('float64', copy=False)
        b = y[first:first + window_frames].astype('float64', copy=False)
        delta = b - scalar * a
        power = float(np.sum(delta * delta))
        error += power
        peak_error = max(peak_error, float(np.max(np.abs(delta))))
        # A dropout's observed power is zero; expected power supplies its denominator.
        denominator = max(float(np.sum(b * b)), scalar * scalar * float(np.sum(a * a)))
        if denominator > max(1e-24, yy * 1e-12):
            worst = max(worst, _db(math.sqrt(power / denominator)))
    residual = _db(math.sqrt(error / yy))
    reasons = []
    if scalar <= 0:
        reasons.append('nonpositive_common_gain')
    if correlation < min_correlation:
        reasons.append('correlation_below_requirement')
    if residual > max_residual_db:
        reasons.append('residual_above_requirement')
    if worst > max_residual_db:
        reasons.append('short_window_residual_above_requirement')
    return {
        'status': 'verified' if not reasons else 'rejected', 'accepted': not reasons,
        'reasons': reasons, 'frames': len(x), 'channels': x.shape[1],
        'fitted_scalar': scalar, 'gain_db': _db(abs(scalar)),
        'correlation': correlation, 'normalized_residual_db': residual,
        'worst_window_residual_db': worst, 'peak_residual': peak_error,
        'channel_fitted_scalars': [float(dot / power) if power > 1e-24 else None
                                   for dot, power in zip(channel_xy, channel_xx)],
        'min_correlation': min_correlation, 'max_residual_db': max_residual_db,
    }


def _correlations(reference, captured):
    """Cosine similarity at every valid lag; channel order remains fixed."""
    import numpy as np
    from scipy.signal import correlate

    dot = None
    for channel in range(reference.shape[1]):
        values = correlate(captured[:, channel], reference[:, channel],
                           mode='valid', method='fft')
        dot = values.astype('float64') if dot is None else dot + values
    energy = np.sum(captured.astype('float64') ** 2, axis=1)
    cumulative = np.concatenate(([0.], np.cumsum(energy)))
    local_energy = np.maximum(0., cumulative[len(reference):] - cumulative[:-len(reference)])
    reference_energy = float(np.sum(reference.astype('float64') ** 2))
    denominator = np.sqrt(local_energy * reference_energy)
    return np.divide(dot, denominator, out=np.full_like(dot, -1.), where=denominator > 1e-24)


def find_alignment(reference, captured, sample_rate, *, min_correlation=.999,
                   max_residual_db=-60., decimation=16, max_candidates=8,
                   minimum_seconds=.1, maximum_seconds=30.):
    """Locate a complete reference occurrence without changing timing or channels.

    An antialiased coarse search proposes at most ``max_candidates`` disjoint
    neighborhoods, each refined at full sample rate within +/-2 decimation
    steps. Full-rate metrics are returned even on rejection. Multiple qualifying
    sample offsets, or unsearched plausible coarse peaks, reject uniqueness.
    Coarse resampling is used only for search, never for the final null check.
    """
    import numpy as np
    from scipy.signal import find_peaks, resample_poly

    x, y = _array(reference), _array(captured)
    if (not isinstance(sample_rate, int) or isinstance(sample_rate, bool)
            or not 8000 <= sample_rate <= 192000):
        raise ValueError('sample_rate must be an integer from 8000 to 192000')
    if (not isinstance(decimation, int) or not 1 <= decimation <= 64
            or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 32
            or not 0 < minimum_seconds <= maximum_seconds <= 30):
        raise ValueError('Invalid bounded search settings')
    if x.shape[1] != y.shape[1]:
        raise ValueError('Reference and capture must preserve the same channel count')
    if max(len(x), len(y)) > maximum_seconds * sample_rate:
        raise ValueError('Audio exceeds the bounded maximum duration')
    if len(x) < minimum_seconds * sample_rate or len(y) < len(x):
        return {'status': 'rejected', 'accepted': False,
                'reasons': ['insufficient_complete_window'], 'start_frame': None}
    if len(x) == len(y):
        return {**measure_alignment(x, y, min_correlation=min_correlation,
                                    max_residual_db=max_residual_db,
                                    window_frames=max(1, sample_rate // 10)),
                'start_frame': 0, 'searched_candidate_count': 1, 'search_truncated': False}
    # float32 transforms keep large 48 kHz captures comfortably below 512 MiB.
    a = resample_poly(x.astype('float32', copy=False), 1, decimation, axis=0)
    b = resample_poly(y.astype('float32', copy=False), 1, decimation, axis=0)
    coarse = _correlations(a, b)
    peaks = {int(index) for index in find_peaks(coarse, distance=3)[0]}
    peaks.update((0, len(coarse) - 1, int(np.argmax(coarse))))
    peaks = sorted(peaks, key=lambda index: (-coarse[index], index))
    omitted = peaks[max_candidates:]
    truncated = any(coarse[index] >= min_correlation - .01 for index in omitted)
    best = None
    accepted_starts = set()
    measured_starts = set()
    refined_intervals = []
    for peak in peaks[:max_candidates]:
        low = max(0, peak * decimation - 2 * decimation)
        high = min(len(y) - len(x), peak * decimation + 2 * decimation)
        if any(left <= low and high <= right for left, right in refined_intervals):
            continue
        refined_intervals.append((low, high))
        fine = _correlations(x.astype('float32', copy=False),
                             y[low:high + len(x)].astype('float32', copy=False))
        # Inspect local maxima, including interval boundaries. Nearby offsets
        # may be equally valid for repetitive signals; never pick one silently.
        offsets = {int(index) for index in find_peaks(fine)[0]}
        offsets.update((0, len(fine) - 1, int(np.argmax(fine))))
        offsets = sorted(offsets, key=lambda index: (-fine[index], index))
        for offset in offsets:
            start = low + offset
            if start in measured_starts:
                continue
            # Keep the best numerical metrics, but skip clearly inferior peaks.
            if best is not None and fine[offset] < min_correlation - 1e-5:
                continue
            measured_starts.add(start)
            measurement = measure_alignment(
                x, y[start:start + len(x)], min_correlation=min_correlation,
                max_residual_db=max_residual_db, window_frames=max(1, sample_rate // 10))
            row = {**measurement, 'start_frame': start}
            score = measurement.get('correlation')
            if best is None or (score is not None and score > (best['correlation'] or -1.)):
                best = row
            if measurement['accepted']:
                accepted_starts.add(start)
    assert best is not None
    best['searched_candidate_count'] = len(measured_starts)
    best['search_truncated'] = bool(truncated)
    best['qualifying_start_frames'] = sorted(accepted_starts)
    if len(accepted_starts) > 1 or truncated:
        best['status'] = 'rejected'
        best['accepted'] = False
        best['reasons'] = [*best['reasons'], 'ambiguous_contiguous_match']
    return best
