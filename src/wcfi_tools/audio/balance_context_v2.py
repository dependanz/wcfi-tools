"""Role-aware sustained-signal qualification, with unchanged RAW activity support.

Amplitude stationarity is compatible with a pad, chord, held vocal or bass note.
Narrow spectral structure outside mains-like harmonic concentrations is evidence
for that possibility; it is not performer identification. These fixed engineering
heuristics are uncalibrated, and ambiguity never makes a weak source disappear.
"""
from __future__ import annotations

import copy

from .balance_context import POLICY as PREVIOUS_POLICY
from .balance_context import assess_context as assess_previous_context

POLICY = PREVIOUS_POLICY | {
    'kind': 'wcfi_raw_balance_context_v2',
    'sustained_roles': ['vocals', 'bass', 'other'],
    'spectral_window_seconds': 1.,
    'maximum_spectral_windows': 8,
    'spectral_minimum_hz': 20.,
    'spectral_maximum_hz': 12000.,
    'local_median_width_hz': 31.,
    'tonal_excess_db': 12.,
    'minimum_tonal_power_fraction': .55,
    'minimum_sustained_window_fraction': .6,
    'mains_fundamentals_hz': [50., 60.],
    'mains_harmonic_maximum_hz': 2000.,
    'mains_half_width_hz': 1.1,
    'mains_dominant_power_fraction': .65,
    'stationarity_policy': ('Sustained spectral structure can resolve amplitude stationarity '
        'for vocals/bass/other. Stationary drums, noise-like or mains-dominated signals '
        'remain uncertain. Duration, weak-energy and similarity objections are unchanged.'),
}


def _sustained_evidence(samples, sample_rate):
    """At most eight one-second spectra; no whole-file spectrogram or model runtime."""
    import numpy as np
    from scipy.ndimage import median_filter

    x = np.asarray(samples, dtype='float32')
    if x.ndim == 1:
        x = x[:, None]
    width = round(sample_rate * POLICY['spectral_window_seconds'])
    count = min(len(x) // width, POLICY['maximum_spectral_windows'])
    starts = np.linspace(0, len(x) - width, count, dtype=int)
    window = np.hanning(width)
    frequency = np.fft.rfftfreq(width, d=1. / sample_rate)
    selected = ((frequency >= POLICY['spectral_minimum_hz'])
                & (frequency <= min(POLICY['spectral_maximum_hz'], sample_rate / 2)))
    frequencies = frequency[selected]
    resolution = sample_rate / width
    median_width = max(3, round(POLICY['local_median_width_hz'] / resolution) | 1)
    mains_masks = {}
    for fundamental in POLICY['mains_fundamentals_hz']:
        nearest = np.round(frequencies / fundamental)
        mains_masks[str(int(fundamental))] = (
            (nearest >= 1)
            & (nearest * fundamental <= POLICY['mains_harmonic_maximum_hz'])
            & (np.abs(frequencies - nearest * fundamental) <= POLICY['mains_half_width_hz'])
        )
    rows = []
    for start in starts:
        block = x[start:start + width].astype('float64')
        block -= block.mean(axis=0, keepdims=True)
        spectrum = np.fft.rfft(block * window[:, None], axis=0)
        power = np.mean(np.abs(spectrum) ** 2, axis=1)[selected]
        total = float(power.sum())
        if total <= 1e-24:
            rows.append({'start_seconds': float(start / sample_rate),
                'tonal_power_fraction': 0., 'mains_power_fraction': {'50': 0., '60': 0.},
                'spectral_flatness': None, 'centroid_hz': None,
                'sustained_structure_observed': False})
            continue
        local_floor = np.maximum(median_filter(power, size=median_width, mode='nearest'),
                                 total * 1e-12 / len(power))
        tonal = power >= local_floor * 10 ** (POLICY['tonal_excess_db'] / 10)
        tonal_fraction = float(power[tonal].sum() / total)
        mains_fractions = {key: float(power[mask].sum() / total)
                           for key, mask in mains_masks.items()}
        safe_power = np.maximum(power, total * 1e-12 / len(power))
        rows.append({'start_seconds': float(start / sample_rate),
            'tonal_power_fraction': tonal_fraction, 'mains_power_fraction': mains_fractions,
            'spectral_flatness': float(np.exp(np.log(safe_power).mean()) / safe_power.mean()),
            'centroid_hz': float(frequencies @ power / total),
            'sustained_structure_observed': (
                tonal_fraction >= POLICY['minimum_tonal_power_fraction']
                and max(mains_fractions.values()) < POLICY['mains_dominant_power_fraction'])})
    fraction = sum(r['sustained_structure_observed'] for r in rows) / len(rows)
    mains_fraction = sum(max(r['mains_power_fraction'].values()) >=
        POLICY['mains_dominant_power_fraction'] for r in rows) / len(rows)
    centroids = [r['centroid_hz'] for r in rows if r['centroid_hz'] is not None]
    return {
        'windows': rows, 'sampled_seconds': count * POLICY['spectral_window_seconds'],
        'sustained_structure_window_fraction': fraction,
        'mains_dominated_window_fraction': mains_fraction,
        'centroid_range_hz': max(centroids) - min(centroids) if centroids else None,
        'sustained_structure_supported': fraction >= POLICY['minimum_sustained_window_fraction'],
        'mains_like_dominance': mains_fraction >= POLICY['minimum_sustained_window_fraction'],
        'interpretation': ('Narrow spectral structure is compatible with sustained performance, '
            'not proof of a performer or desired tone. Mains-like notes can be ambiguous; '
            'filtered noise, feedback and non-mains oscillators can resemble musical tones.'),
    }


def assess_context(raw, sample_rate):
    """Qualify RAW context once; reuse it unchanged for every candidate's gains.

    This version only revises the amplitude-stationarity objection. A supported
    result does not establish source identity, ideal balance, or safe auto-apply.
    """
    result = assess_previous_context(raw, sample_rate)
    result['kind'] = POLICY['kind']
    result['policy'] = copy.deepcopy(POLICY)
    for role, row in result['roles'].items():
        row['sustained_signal_evidence'] = None
        row['activity_support_basis'] = 'RAW energy and existing temporal-structure evidence'
        if 'stationary_energy_or_sustained_arrangement' not in row['reasons']:
            continue
        evidence = _sustained_evidence(raw[role], sample_rate)
        row['sustained_signal_evidence'] = evidence
        row['reasons'].remove('stationary_energy_or_sustained_arrangement')
        if role not in POLICY['sustained_roles']:
            row['reasons'].append('stationary_drums_need_percussive_context')
        elif evidence['sustained_structure_supported']:
            row['activity_support_basis'] = 'RAW energy with sustained spectral structure'
        elif evidence['mains_like_dominance']:
            row['reasons'].append('stationary_mains_overlap_needs_context')
        else:
            row['reasons'].append('stationary_signal_without_clear_sustained_structure')
        row['evidence_state'] = ('insufficient'
            if 'insufficient_energy_duration' in row['reasons'] else
            'ambiguous' if row['reasons'] else 'supported')
    result['reasons'] = [r for r in result['reasons'] if r != 'uncertain_role_context']
    if any(row['evidence_state'] != 'supported' for row in result['roles'].values()):
        result['reasons'].append('uncertain_role_context')
    result['automatic_balance_allowed'] = not result['reasons']
    result['auto_apply_allowed'] = False
    return result
