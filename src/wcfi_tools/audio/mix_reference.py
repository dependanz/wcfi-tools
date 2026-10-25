"""Conservative, gain-invariant reference comparisons on saved CPU arrays.

This is a measurement policy, not a trained listener or a professional-quality
score. A matching reference and comparable musical context are prerequisites
that callers must establish separately. No sound, device or network is opened.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from itertools import pairwise

POLICY = {
    'kind': 'wcfi_joint_mix_reference_v1',
    'band_edges_hz': [50, 100, 200, 400, 800, 1600, 3200, 6400, 12000],
    'welch_samples': 4096,
    'welch_overlap': 2048,
    'envelope_seconds': 1.,
    'crest_seconds': .1,
    'crest_quantiles': [.5, .9],
    'dynamic_range_quantiles': [.1, .9],
    'relative_power_floor': 1e-12,
    'minimum_seconds': 3.,
    'maximum_seconds': 120.,
    'minimum_sample_rate': 24000,
    'maximum_sample_rate': 96000,
    'improvement': {
        'spectrum_db': .5, 'envelope_db': .25, 'crest_db': .5,
        'dynamic_range_db': .5, 'side_fraction': .02, 'lr_correlation': .02,
    },
    'maximum_regression': {
        'spectrum_db': .25, 'envelope_db': .125, 'crest_db': .25,
        'dynamic_range_db': .25, 'side_fraction': .01, 'lr_correlation': .01,
    },
    'numeric_tolerance': 1e-8,
    'selection': 'Meaningful improvement and no material regression against baseline; '
                 'unique exact Pareto frontier only, otherwise abstain',
    'stereo_dependence': 'Side fraction and L/R correlation are related measurements, '
                         'not independent votes or separately weighted quality scores',
    'meaning': 'Engineering tolerances, not perceptual thresholds, human preference '
               'labels or a probability of professional quality',
    'limits': 'Cannot identify source balance or intent from a stereo reference. '
              'Comparable passages and source activity require separate qualification. '
              'Do not validate this selector using its own objective as ground truth.',
}


def _unsupported(reason):
    return {'kind': POLICY['kind'], 'status': 'unsupported', 'reasons': [reason]}


def describe(audio, sample_rate):
    """Describe finite mono/stereo PCM with bounded, short-window CPU transforms.

    A trailing partial second is excluded from envelope statistics and a trailing
    partial 100 ms is excluded from crest quantiles. Spectrum and stereo use the
    complete available signal. Silent crest frames are excluded and counted.
    Absolute level is intentionally unavailable: multiply by any nonzero scalar
    without changing these descriptors. Input clips must be checked separately
    for headroom and audibility.
    """
    import numpy as np

    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float))
            or not math.isfinite(sample_rate) or int(sample_rate) != sample_rate
            or not POLICY['minimum_sample_rate'] <= sample_rate <= POLICY['maximum_sample_rate']):
        return _unsupported('sample_rate_outside_supported_range')
    rate = int(sample_rate)
    try:
        x = np.asarray(audio)
    except (TypeError, ValueError):
        return _unsupported('invalid_audio_array')
    if (x.ndim not in (1, 2) or (x.ndim == 2 and x.shape[1] not in (1, 2))
            or x.dtype.kind not in 'iuf'):
        return _unsupported('finite_mono_or_stereo_pcm_required')
    if not POLICY['minimum_seconds'] <= len(x)/rate <= POLICY['maximum_seconds']:
        return _unsupported('duration_outside_supported_range')
    x = x.reshape(len(x), -1)
    # Scan in small blocks: do not materialize an entire float64 copy or STFT.
    peak = 0.
    for start in range(0, len(x), 65536):
        block = x[start:start+65536].astype(np.float64)
        if not np.isfinite(block).all():
            return _unsupported('nonfinite_audio')
        peak = max(peak, float(np.max(np.abs(block))))
    if peak == 0:
        return _unsupported('silent_audio')

    def normalized(start, size):
        return x[start:start+size].astype(np.float64)/peak

    sums = np.zeros(2)
    squares = np.zeros(2)
    cross = 0.
    for start in range(0, len(x), 65536):
        block = normalized(start, 65536)
        left, right = block[:, 0], block[:, -1]
        sums += [left.sum(), right.sum()]
        squares += [np.dot(left, left), np.dot(right, right)]
        cross += float(np.dot(left, right))
    variance = squares-sums*sums/len(x)
    if np.any(variance <= 0):
        return _unsupported('zero_channel_variance')
    correlation = (cross-sums[0]*sums[1]/len(x))/math.sqrt(float(np.prod(variance)))
    side_fraction = (squares.sum()-2*cross)/(2*squares.sum())

    nfft = POLICY['welch_samples']
    window = np.hanning(nfft+1)[:-1]
    power = np.zeros(nfft//2+1)
    count = 0
    for start in range(0, len(x)-nfft+1, nfft-POLICY['welch_overlap']):
        block = normalized(start, nfft)
        block -= block.mean(axis=0)
        transform = np.fft.rfft(block*window[:, None], axis=0)
        power += np.mean(transform.real**2+transform.imag**2, axis=1)
        count += 1
    frequencies = np.fft.rfftfreq(nfft, 1/rate)
    edges = POLICY['band_edges_hz']
    bands = np.array([power[(frequencies >= lo) & (frequencies < hi)].sum()
                      for lo, hi in pairwise(edges)])
    if bands.sum() <= 0 or count == 0:
        return _unsupported('no_supported_spectral_energy')
    band_db = 10*np.log10(np.maximum(bands/bands.sum(), POLICY['relative_power_floor']))

    envelope = []
    for start in range(0, len(x)-rate+1, rate):
        block = normalized(start, rate)
        envelope.append(float(np.mean(block*block)))
    envelope = np.asarray(envelope)
    if envelope.mean() <= 0:
        return _unsupported('no_supported_envelope_energy')
    # Floor relative to overall RMS, not full-scale PCM; gain remains irrelevant.
    envelope = 10*np.log10(np.maximum(envelope/envelope.mean(),
                                     POLICY['relative_power_floor']))
    envelope -= envelope.mean()
    crest, silent_crest_frames = [], 0
    frame = round(rate*POLICY['crest_seconds'])
    for start in range(0, len(x)-frame+1, frame):
        block = normalized(start, frame)
        rms = math.sqrt(float(np.mean(block*block)))
        if rms == 0:
            silent_crest_frames += 1
        else:
            crest.append(20*math.log10(float(np.max(np.abs(block)))/rms))
    if not crest:
        return _unsupported('no_supported_dynamic_frames')
    dynamic_quantiles = np.quantile(envelope, POLICY['dynamic_range_quantiles'])
    return {
        'kind': POLICY['kind'], 'status': 'supported', 'reasons': [],
        'duration_seconds': len(x)/rate, 'sample_rate': rate,
        'input_channels': x.shape[1], 'spectral_band_db': band_db.tolist(),
        'centered_envelope_db': envelope.tolist(),
        'crest_quantiles_db': np.quantile(crest, POLICY['crest_quantiles']).tolist(),
        'dynamic_range_db': float(dynamic_quantiles[1]-dynamic_quantiles[0]),
        'side_energy_fraction': float(np.clip(side_fraction, 0, 1)),
        'lr_correlation': float(np.clip(correlation, -1, 1)),
        'spectral_windows': count, 'crest_frames': len(crest),
        'silent_crest_frames': silent_crest_frames,
    }


def compare(candidate_descriptor, reference_descriptor):
    """Compute six separate nonnegative errors for equal-length aligned excerpts."""
    import numpy as np

    descriptors = (candidate_descriptor, reference_descriptor)
    if any(not isinstance(d, Mapping) or d.get('kind') != POLICY['kind']
           or d.get('status') != 'supported' for d in descriptors):
        return _unsupported('unsupported_or_incompatible_descriptor')
    try:
        a, b = descriptors
        for d in descriptors:
            scalar_keys = ('duration_seconds', 'dynamic_range_db',
                           'side_energy_fraction', 'lr_correlation', 'sample_rate')
            if (any(not math.isfinite(d[k]) for k in scalar_keys)
                    or not POLICY['minimum_seconds'] <= d['duration_seconds']
                    <= POLICY['maximum_seconds'] or d['dynamic_range_db'] < 0
                    or not 0 <= d['side_energy_fraction'] <= 1
                    or not -1 <= d['lr_correlation'] <= 1
                    or not POLICY['minimum_sample_rate'] <= d['sample_rate']
                    <= POLICY['maximum_sample_rate']
                    or int(d['sample_rate']) != d['sample_rate']):
                return _unsupported('invalid_or_incompatible_descriptor_values')
        if a['sample_rate'] != b['sample_rate']:
            return _unsupported('equal_analysis_sample_rates_required')
        if abs(a['duration_seconds']-b['duration_seconds']) > .001:
            return _unsupported('aligned_equal_length_passages_required')
        features = {}
        for key, size in [('spectral_band_db', 8), ('centered_envelope_db', None),
                          ('crest_quantiles_db', 2)]:
            av, bv = np.asarray(a[key], dtype=float), np.asarray(b[key], dtype=float)
            if (av.ndim != 1 or av.shape != bv.shape or not len(av)
                    or (size is not None and len(av) != size)
                    or (key == 'centered_envelope_db'
                        and len(av) != int(a['duration_seconds']))
                    or not np.isfinite(av).all() or not np.isfinite(bv).all()):
                return _unsupported('invalid_or_incompatible_descriptor_values')
            features[key] = av-bv
        distances = {
            'spectrum_db': float(np.sqrt(np.mean(features['spectral_band_db']**2))),
            'envelope_db': float(np.mean(np.abs(features['centered_envelope_db']))),
            'crest_db': float(np.mean(np.abs(features['crest_quantiles_db']))),
            'dynamic_range_db': abs(float(a['dynamic_range_db']-b['dynamic_range_db'])),
            'side_fraction': abs(float(a['side_energy_fraction']-b['side_energy_fraction'])),
            'lr_correlation': abs(float(a['lr_correlation']-b['lr_correlation'])),
        }
        if not all(math.isfinite(v) for v in distances.values()):
            return _unsupported('nonfinite_descriptor_values')
    except (KeyError, TypeError, ValueError):
        return _unsupported('invalid_or_incompatible_descriptor_values')
    return {'kind': POLICY['kind'], 'status': 'supported', 'reasons': [], **distances}


def select_candidates(rows, baseline_id='unity'):
    """Select only an unambiguous joint improvement, without hiding tradeoffs.

    Each row is ``{'id': <opaque id>, 'distances': compare(...)}``. Unsupported
    rows never count as clean; their identities remain in the output. The result
    is reference-conditioned and cannot substitute for independent validation.
    """
    rows = list(rows)
    ids = [r.get('id') for r in rows if isinstance(r, Mapping)]
    if (len(ids) != len(rows) or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids) or baseline_id not in ids):
        raise ValueError('Use unique nonempty candidate IDs including the baseline')
    keys = tuple(POLICY['improvement'])
    valid, unknown = {}, []
    for row in rows:
        d = row.get('distances')
        if (not isinstance(d, Mapping) or d.get('kind') != POLICY['kind']
                or d.get('status') != 'supported'
                or any(not isinstance(d.get(k), (int, float)) or isinstance(d.get(k), bool)
                       or not math.isfinite(d[k]) or d[k] < 0 for k in keys)):
            unknown.append(row['id'])
        else:
            valid[row['id']] = {k: float(d[k]) for k in keys}
    result = {
        'kind': POLICY['kind'], 'status': 'abstain', 'baseline_id': baseline_id,
        'selected_id': None, 'eligible_ids': [], 'frontier_ids': [],
        'unknown_ids': sorted(unknown), 'rejected_ids': [], 'evidence': {},
        'auto_apply_allowed': False,
    }
    if baseline_id not in valid:
        return result | {'reason': 'unsupported_baseline'}
    baseline = valid[baseline_id]
    epsilon = POLICY['numeric_tolerance']
    for identifier, d in valid.items():
        if identifier == baseline_id:
            continue
        delta = {k: baseline[k]-d[k] for k in keys}
        improvements = [k for k in keys if delta[k]+epsilon >= POLICY['improvement'][k]]
        regressions = [k for k in keys if -delta[k] > POLICY['maximum_regression'][k]+epsilon]
        eligible = bool(improvements) and not regressions
        result['evidence'][identifier] = {
            'improvement_from_baseline': delta, 'meaningful_improvements': improvements,
            'material_regressions': regressions, 'eligible': eligible,
        }
        result['eligible_ids' if eligible else 'rejected_ids'].append(identifier)
    result['eligible_ids'].sort()
    result['rejected_ids'].sort()
    eligible = result['eligible_ids']
    # Exact Pareto ordering avoids trading an envelope/width loss for a spectral
    # win. Numeric-equal candidates both remain on the frontier: no ID tie-break.
    def dominates(first, second):
        return (all(valid[first][k] <= valid[second][k]+epsilon for k in keys)
                and any(valid[first][k] < valid[second][k]-epsilon for k in keys))

    frontier = [i for i in eligible if not any(dominates(j, i) for j in eligible if j != i)]
    result['frontier_ids'] = frontier
    if len(frontier) == 1:
        return result | {'status': 'selected', 'reason': 'unique_joint_improvement',
                         'selected_id': frontier[0]}
    reason = 'ambiguous_joint_improvements'
    if not frontier:
        reason = 'ambiguous_numerical_frontier' if eligible else 'no_joint_improvement'
    return result | {'reason': reason}
