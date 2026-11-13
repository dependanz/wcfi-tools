"""Source-aware, persistent mains-comb check; no audio or DAW devices.

This detects newly unexplained 50/60 Hz combs, not every possible ground-loop
fault. A source-explained tone can be an instrument or upstream hum; this layer
cannot distinguish identical waveforms by intent alone.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

POLICY = {
    'version': 3, 'rate': 2000, 'low_band_hz': [25, 420], 'ridge': .0001,
    'window_seconds': 2, 'hop_seconds': 1, 'minimum_consecutive_windows': 3,
    'families_hz': [50, 60], 'harmonics': [1, 2, 3],
    'minimum_harmonics': 2, 'minimum_prominence_db': 12.,
    'minimum_line_to_mix_db': -32., 'minimum_comb_to_mix_db': -18.,
    'scope': 'Persistent mains-family energy unexplained by synchronized raw sources; source-explained tones need separate input evaluation.',
}


def _db(a):
    return 10*np.log10(np.maximum(a, 1e-30))


def _longest(mask):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return int(np.max(ends-starts)) if len(starts) else 0


class HumAnalyzer:
    """Cross-fit low-band source prediction, followed by a residual comb test."""
    def __init__(self, raw):
        self.n = len(next(iter(raw.values())))
        if self.n % 48000 or self.n < 4*48000 or any(np.asarray(a).shape != (self.n, 2) for a in raw.values()):
            raise ValueError('Hum evaluation needs synchronized 48 kHz stereo sources, at least four whole seconds')
        self.sos = butter(2, POLICY['low_band_hz'], fs=2000, btype='bandpass', output='sos')
        columns = []
        for a in raw.values():
            b = self.convert(a)
            if np.mean(b*b) > 1e-14:
                # Local phase/EQ variation is represented by small lags. Fits on
                # alternating seconds predict the held-out seconds, not themselves.
                for lag in (-1, 0, 1):
                    columns.append(np.roll(b, lag, axis=0))
        if not columns:
            raise ValueError('Hum evaluator has no raw-source context')
        x = np.stack(columns, axis=2)
        self.x = x/np.maximum(np.sqrt(np.mean(x*x, axis=(0, 1))), 1e-12)
        self.parity = (np.arange(len(x))//2000) % 2

    def convert(self, a):
        return sosfilt(self.sos, resample_poly(a, 1, 24, axis=0), axis=0)

    def residual(self, a):
        y = self.convert(a);r = np.empty_like(y)
        for channel in (0, 1):
            x = self.x[:, channel]
            for parity in (0, 1):
                train = self.parity == parity;test = ~train
                xt = x[train];gram = xt.T@xt/len(xt)
                weight = np.linalg.solve(gram+np.eye(len(gram))*POLICY['ridge'], xt.T@y[train, channel]/len(xt))
                r[test, channel] = y[test, channel]-x[test]@weight
        return r

    def evaluate(self, audio):
        a = np.asarray(audio)
        if a.shape != (self.n, 2) or not np.isfinite(a).all():
            raise ValueError('Hum mix and raw inputs must be synchronized and finite')
        residual = self.residual(a)
        n = 4000;window = np.hanning(n);frequency = np.fft.rfftfreq(n, 1/2000)
        starts = range(0, len(residual)-n+1, 2000)
        spectra, total = [], []
        for start in starts:
            transformed = np.fft.rfft(residual[start:start+n]*window[:, None], axis=0)
            p = np.mean(np.abs(transformed)**2, axis=1)/(n*np.sum(window**2));p[1:-1] *= 2
            spectra.append(p)
            original = a[start*24:(start+n)*24]
            total.append(float(np.mean(original.astype(float)**2)))
        spectra = np.asarray(spectra);total = np.maximum(total, 1e-30)
        families = []
        for nominal in POLICY['families_hz']:
            line_power, prominent = [], []
            for harmonic in POLICY['harmonics']:
                hz = nominal*harmonic
                width = .6+harmonic*.2
                selected = np.abs(frequency-hz) <= width
                neighborhood = (np.abs(frequency-hz) >= 2.5) & (np.abs(frequency-hz) <= 8)
                energy = spectra[:, selected].sum(axis=1)
                background = np.median(spectra[:, neighborhood], axis=1)*selected.sum()
                prominence = _db(energy)-_db(background)
                strong = (prominence >= POLICY['minimum_prominence_db']) & (
                    _db(energy/total) >= POLICY['minimum_line_to_mix_db'])
                line_power.append(energy);prominent.append(strong)
            line_power = np.asarray(line_power).T;prominent = np.asarray(prominent).T
            combined = line_power.sum(axis=1)/total
            flagged = (prominent.sum(axis=1) >= POLICY['minimum_harmonics']) & (
                _db(combined) >= POLICY['minimum_comb_to_mix_db'])
            run = _longest(flagged)
            families.append({'nominal_hz': nominal, 'violation': run >= POLICY['minimum_consecutive_windows'],
                'flagged_windows': int(flagged.sum()), 'longest_run_windows': run,
                'longest_evidence_seconds': float(run+1 if run else 0),
                'median_residual_comb_to_mix_db': float(np.median(_db(combined))),
                'maximum_residual_comb_to_mix_db': float(np.max(_db(combined))),
                'windows': [{'start_seconds': i, 'harmonics': int(prominent[i].sum()),
                             'comb_to_mix_db': float(_db(combined[i])), 'flagged': bool(flagged[i])}
                            for i in range(len(flagged))]})
        return {'policy_version': 3, 'violation': any(f['violation'] for f in families), 'families': families,
                'residual_low_band_power': float(np.mean(residual**2)),
                'scope': 'Newly unexplained mains comb; source-explained instrument tones versus upstream hum remain ambiguous'}


def veto(evaluation, observation):
    # Replace the old mixed-spectrum-only hum rule; keep every other technical
    # and source-balance veto. Reference cost and weights remain unchanged.
    failures = [f for f in evaluation['guardrail_failures'] if f != 'tonal hum']
    if observation['violation']:
        failures.append('unexplained mains hum')
    return evaluation | {'eligible': not failures, 'guardrail_failures': failures, 'mains_hum': observation}
