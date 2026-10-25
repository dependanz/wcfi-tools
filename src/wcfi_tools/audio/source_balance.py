"""Role-aware source-balance vetoes for synchronized saved multitracks.

These are configurable worship-band hypotheses, not psychoacoustic masking or
musical-preference measurements. Estimate sources from audio, never from a
candidate's renderer gains/labels. Correlated bleed can prevent attribution.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import nnls
from scipy.signal import butter, resample_poly, sosfilt

POLICY = {
    'version': 2, 'frame_seconds': .25, 'activity_below_p90_db': 18.,
    'minimum_unique_fraction': .04, 'minimum_evidence_seconds': 1.,
    'minimum_excess_seconds': .75, 'attribution_floor_dbfs': -65.,
    'bands': {'body': [40, 250], 'low_mid': [150, 650], 'presence': [700, 3500]},
    'rules': [
        {'id': 'acoustic_over_lead_body', 'band': 'low_mid', 'source': 'AC GUITAR', 'reference': 'RED', 'ceiling_db': 0.},
        {'id': 'acoustic_over_lead_presence', 'band': 'presence', 'source': 'AC GUITAR', 'reference': 'RED', 'ceiling_db': 0.},
        {'id': 'electric_over_lead', 'band': 'presence', 'source': 'EGUITAR', 'reference': 'RED', 'ceiling_db': 3.},
        {'id': 'keys_over_lead', 'band': 'low_mid', 'source': 'KEYS', 'reference': 'RED', 'ceiling_db': 3.},
        {'id': 'bass_over_kit', 'band': 'body', 'source': 'BASS', 'reference': 'KIT', 'ceiling_db': 9.},
        {'id': 'lead_over_band', 'band': 'presence', 'source': 'RED', 'reference': 'band', 'ceiling_db': 10.},
        {'id': 'kit_over_band', 'band': 'body', 'source': 'KIT', 'reference': 'band', 'ceiling_db': 9.},
        {'id': 'buried_lead', 'band': 'presence', 'source': 'band', 'reference': 'RED', 'ceiling_db': 10.},
        {'id': 'buried_kit', 'band': 'body', 'source': 'band', 'reference': 'KIT', 'ceiling_db': 15.},
    ],
    'priority': 'Balance violations veto selection; reference distance cannot compensate.',
    'scope': 'Lead-led full-band worship; not instrumental solos, sermons or universal mix targets.',
}


def _power(a):
    return float(np.mean(np.asarray(a, dtype=float)**2))


def _db(a):
    return 10*np.log10(np.maximum(a, 1e-30))


def _longest(mask):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return int(np.max(ends-starts)) if len(starts) else 0


class BalanceAnalyzer:
    """Windowed, per-channel NNLS in three bands, with identifiability checks."""
    def __init__(self, raw):
        self.names = [name for name, a in raw.items() if _power(a) > 10**(POLICY['attribution_floor_dbfs']/10)]
        if not {'RED', 'KIT'} <= set(self.names):
            raise ValueError('Source balance needs identifiable lead and KIT inputs')
        self.frames = len(raw['RED'])//12000
        if self.frames < 4 or any(np.asarray(a).shape != (self.frames*12000, 2) for a in raw.values()):
            raise ValueError('Use synchronized stereo 48 kHz sources in whole quarter-second windows')
        self.banks = {}
        for band, limits in POLICY['bands'].items():
            sos = butter(2, limits, fs=12000, btype='bandpass', output='sos')
            columns = []
            for name in self.names:
                a = sosfilt(sos, resample_poly(raw[name], 1, 4, axis=0), axis=0)
                columns.append(a)
            x = np.stack(columns, axis=2).reshape(self.frames, 3000, 2, len(self.names))
            # Source normalization is global per band. Stereo fits are independent,
            # so changing pan alone is not interpreted as changing source level.
            rms = np.sqrt(np.mean(x*x, axis=(0, 1, 2)))
            normalized = x/np.maximum(rms, 1e-15)
            flat = normalized.reshape(-1, len(self.names))
            gram = flat.T@flat/len(flat)
            inverse = np.linalg.pinv(gram, rcond=1e-7)
            unique = np.clip(1/np.maximum(np.diag(inverse)*np.diag(gram), 1), 0, 1)
            # Zero/near-duplicate columns must not acquire false confidence from
            # the pseudoinverse's treatment of a singular Gram matrix.
            for i in range(len(self.names)):
                others = np.delete(flat, i, axis=1)
                fitted = others@np.linalg.lstsq(others[::12], flat[::12, i], rcond=1e-6)[0]
                unique[i] = min(unique[i], _power(flat[:, i]-fitted)/max(_power(flat[:, i]), 1e-30))
            self.banks[band] = {'sos': sos, 'x': normalized, 'unique': dict(zip(self.names, unique.tolist(), strict=True)),
                                'raw_power': np.mean(x*x, axis=(1, 2))}

    def evaluate(self, audio):
        audio = np.asarray(audio)
        if audio.shape != (self.frames*12000, 2) or not np.isfinite(audio).all():
            raise ValueError('Mix and source context must be synchronized')
        estimates, residuals = {}, {}
        for band, bank in self.banks.items():
            y = sosfilt(bank['sos'], resample_poly(audio, 1, 4, axis=0), axis=0).reshape(self.frames, 3000, 2)
            energies = np.zeros((self.frames, len(self.names)))
            residual = []
            for index in range(self.frames):
                for channel in (0, 1):
                    x = bank['x'][index, :, channel]
                    coefficients, error = nnls(x, y[index, :, channel])
                    energies[index] += np.mean(x*x, axis=0)*coefficients**2/2
                    residual.append(error**2/max(float(y[index, :, channel]@y[index, :, channel]), 1e-30))
            estimates[band] = energies
            residuals[band] = float(np.median(residual))
        rules, violations, uncertain = [], [], []
        for rule in POLICY['rules']:
            bank = self.banks[rule['band']];e = estimates[rule['band']]
            involved = [n for n in (rule['source'], rule['reference']) if n != 'band']
            if any(n not in self.names for n in involved):
                rules.append(rule | {'status': 'not_applicable', 'reason': 'Source below raw signal gate'})
                continue
            # The reference performer must be active in RAW audio even if buried
            # in the candidate. Candidate silence cannot disable its own test.
            reference_name = rule['reference'] if rule['reference'] != 'band' else rule['source']
            ri = self.names.index(reference_name)
            raw_level = _db(bank['raw_power'][:, ri])
            active = raw_level >= np.percentile(raw_level, 90)-POLICY['activity_below_p90_db']
            confidence = min(bank['unique'][n] for n in involved)
            seconds = float(active.sum()*.25)
            if confidence < POLICY['minimum_unique_fraction'] or seconds < POLICY['minimum_evidence_seconds']:
                uncertain.append(rule['id'])
                rules.append(rule | {'status': 'indeterminate', 'unique_fraction': confidence, 'evidence_seconds': seconds})
                continue
            def source_energy(name, e=e, ri=ri):
                if name == 'band':
                    return e.sum(axis=1)-e[:, ri]
                return e[:, self.names.index(name)]
            numerator, denominator = source_energy(rule['source']), source_energy(rule['reference'])
            floor = np.maximum(e.sum(axis=1)*1e-12, 1e-30)
            ratio = _db(np.maximum(numerator, floor))-_db(np.maximum(denominator, floor))
            excess = active & (ratio > rule['ceiling_db'])
            excess_seconds = float(excess.sum()*.25)
            longest = _longest(excess)*.25
            # A short transient does not veto a mix; persistent domination does.
            violated = longest >= POLICY['minimum_excess_seconds'] or (
                excess_seconds >= POLICY['minimum_excess_seconds'] and float(excess.mean()) >= .2)
            if violated:
                violations.append(rule['id'])
            rules.append(rule | {'status': 'violation' if violated else 'within_bounds',
                'unique_fraction': confidence, 'evidence_seconds': seconds,
                'median_db': float(np.median(ratio[active])), 'p90_db': float(np.percentile(ratio[active], 90)),
                'excess_seconds': excess_seconds, 'longest_excess_seconds': longest})
        return {'policy_version': 2, 'violations': violations, 'indeterminate_rules': uncertain,
                'eligible': not violations and not uncertain, 'rules': rules,
                'median_band_residuals': residuals,
                'confidence_scope': 'Linear band-source attribution under possible bleed; not perceptual certainty'}


def veto(evaluation, balance):
    """Lexicographic safety: a style improvement cannot buy back bad balance."""
    failures = list(evaluation['guardrail_failures'])
    failures.extend('source balance: '+name for name in balance['violations'])
    if balance['indeterminate_rules']:
        failures.append('source attribution uncertain')
    return evaluation | {'eligible': not failures, 'guardrail_failures': failures, 'source_balance': balance}
