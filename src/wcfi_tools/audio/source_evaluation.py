"""Source-labelled balance measurements for aligned, measured contribution stems.

Roles come from the session/user, not a spectral guess. These descriptors and
training-distribution alerts are not ratings of musical or professional quality.
No audio device, DAW, network, model runtime or playback is accessed here.
"""
from __future__ import annotations

ROLES = ('vocals', 'backing_vocals', 'drums', 'bass', 'guitar', 'keys', 'other')
BANDS = {'full': None, 'body': (40, 250), 'low_mid': (150, 650), 'presence': (700, 3500)}
FEATURE_NAMES = tuple(f'{role}_{band}_relative_db' for role in ROLES for band in BANDS)
POLICY = {
    'version': 1, 'frame_seconds': .25, 'raw_activity_below_p90_db': 18.,
    'raw_floor_dbfs': -70., 'minimum_active_seconds': 1.,
    'ratio_floor_db': -60., 'range_quantiles': [.05, .95], 'range_margin_db': 3.,
    'correlated_sources_warning_above': .95,
    'scope': 'Known synchronized source contributions at Master pre-effects. VOCALS can be lead or a combined vocal stem; OTHER is not guitar-specific.',
    'meaning': 'Role energy relative to other role energies while the raw performer is active; not perceptual masking or an ideal mixing target.',
}


def _db(power):
    import numpy as np

    return 10*np.log10(np.maximum(power, 1e-20))


class SourceBank:
    """Cache raw-source activity; muted candidates cannot hide their performer.

    Supply matching source IDs for raw pre-FX and actual post-mixer contribution
    signals. A post-FX node before the fader is insufficient unless its mixer
    gain/pan is also applied. Do not pass separated estimates as measured stems.
    """
    def __init__(self, raw, roles, rate):
        import numpy as np

        if set(raw) != set(roles) or not raw or set(roles.values())-set(ROLES):
            raise ValueError('Every source needs one explicit supported role')
        if not isinstance(rate, int) or not 16000 <= rate <= 96000:
            raise ValueError('Use 16–96 kHz aligned audio')
        self.raw = {k: self._audio(v) for k, v in raw.items()}
        self.shape = next(iter(self.raw.values())).shape
        if any(v.shape != self.shape for v in self.raw.values()) or not rate <= self.shape[0] <= rate*120:
            raise ValueError('Sources must share channel count, start, rate and 1–120 s length')
        self.roles, self.rate = dict(roles), rate
        self.width = round(rate*POLICY['frame_seconds'])
        self.frames = self.shape[0]//self.width
        self.raw_roles = self._group(self.raw)
        self.activity = {}
        for role in ROLES:
            energy = self._frames(self.raw_roles[role])
            level = _db(energy)
            self.activity[role] = (level >= max(POLICY['raw_floor_dbfs'],
                float(np.quantile(level, .9))-POLICY['raw_activity_below_p90_db']))
        warnings = []
        names = list(self.raw)
        for i, a in enumerate(names):
            for b in names[i+1:]:
                if roles[a] == roles[b]:
                    continue
                x, y = self.raw[a][::16].ravel(), self.raw[b][::16].ravel()
                correlation = float(abs(x@y)/max(float(np.linalg.norm(x)*np.linalg.norm(y)), 1e-20))
                if correlation > POLICY['correlated_sources_warning_above']:
                    warnings.append({'source_a': a, 'source_b': b, 'correlation': correlation,
                        'reason': 'Highly correlated source signals; physical-source attribution may be ambiguous'})
        self.warnings = warnings

    @staticmethod
    def _audio(value):
        import numpy as np

        a = np.asarray(value, dtype='float32')
        if a.ndim == 1:
            a = a[:, None]
        if a.ndim != 2 or a.shape[1] not in (1, 2) or not np.isfinite(a).all():
            raise ValueError('Use finite mono/stereo source audio')
        return a

    def _group(self, sources):
        import numpy as np

        result = {r: np.zeros(self.shape, dtype='float32') for r in ROLES}
        for name, audio in sources.items():
            result[self.roles[name]] += audio
        return result

    def _frames(self, audio):
        import numpy as np

        x = audio[:self.frames*self.width].reshape(self.frames, self.width, -1)
        return np.mean(x.astype('float64')**2, axis=(1, 2))

    def measure(self, contributions, *, master_pre=None):
        """Measure candidate contributions; optionally enforce exact linear routing.

        Master post-FX audio cannot reconstruct per-source contributions after a
        nonlinear shared processor; use these as context, not post-FX attribution.
        """
        import numpy as np
        from scipy.signal import butter, sosfilt

        if set(contributions) != set(self.raw):
            raise ValueError('Candidate and raw source IDs differ')
        contributions = {k: self._audio(v) for k, v in contributions.items()}
        if any(a.shape != self.shape for a in contributions.values()):
            raise ValueError('Candidate contributions are not synchronized with raw sources')
        grouped = self._group(contributions)
        reconstruction = None
        if master_pre is not None:
            master = self._audio(master_pre)
            if master.shape != self.shape:
                raise ValueError('Master/source shape mismatch')
            summed = sum(grouped.values())
            reconstruction = float(np.mean((master-summed).astype('float64')**2)/
                max(float(np.mean(master.astype('float64')**2)), 1e-20))
            if reconstruction > 1e-5:
                raise ValueError('Sources do not reconstruct Master pre-FX; check routing, returns, latency and missing taps')
        per_role, vector, valid = {r: {} for r in ROLES}, [], []
        for band, limits in BANDS.items():
            sos = butter(2, limits, btype='bandpass', fs=self.rate, output='sos') if limits else None
            powers = np.stack([self._frames(sosfilt(sos, grouped[r], axis=0) if sos is not None
                else grouped[r]) for r in ROLES], axis=1)
            for index, role in enumerate(ROLES):
                mask = self.activity[role]
                supported = bool(mask.sum()*POLICY['frame_seconds'] >= POLICY['minimum_active_seconds'])
                other = np.sum(np.delete(powers, index, axis=1), axis=1)
                # Other-source silence is not evidence of a balance problem.
                supported = supported and bool(np.any(other[mask] > 1e-14))
                ratios = np.clip(_db(powers[:, index])-_db(other), -60, 60)
                median = float(np.median(ratios[mask])) if supported else None
                per_role[role][band] = {'relative_db': median, 'active_seconds': float(mask.sum()*POLICY['frame_seconds']),
                    'p10_db': float(np.quantile(ratios[mask], .1)) if supported else None,
                    'p90_db': float(np.quantile(ratios[mask], .9)) if supported else None,
                    'status': 'measured' if supported else 'insufficient_activity'}
        for role in ROLES:
            for band in BANDS:
                value = per_role[role][band]['relative_db']
                vector.append(0. if value is None else value)
                valid.append(value is not None)
        return {'kind': 'wcfi_measured_source_balance_v1', 'feature_names': list(FEATURE_NAMES),
            'features': vector, 'valid': valid, 'roles': per_role, 'warnings': self.warnings,
            'reconstruction_relative_power': reconstruction, 'stage': 'master_pre_fx_contributions',
            'professional_quality_judgment': None, 'auto_apply_allowed': False}


def fit_ranges(reports, *, training_groups):
    """Learn broad expected ranges from training songs only, never quality labels."""
    import numpy as np

    if len(reports) < 5 or len(reports) != len(set(training_groups)):
        raise ValueError('Use at least five distinct training songs, one report per song')
    if any(r['feature_names'] != list(FEATURE_NAMES) for r in reports):
        raise ValueError('Source feature schema mismatch')
    limits = []
    for i, name in enumerate(FEATURE_NAMES):
        values = [r['features'][i] for r in reports if r['valid'][i]]
        if len(values) < 5:
            limits.append({'name': name, 'lower': None, 'upper': None, 'count': len(values)})
            continue
        low, high = np.quantile(values, POLICY['range_quantiles'])
        limits.append({'name': name, 'lower': float(low-POLICY['range_margin_db']),
            'upper': float(high+POLICY['range_margin_db']), 'count': len(values)})
    return {'kind': 'wcfi_source_balance_ranges_v1', 'training_groups': sorted(training_groups),
        'limits': limits, 'policy': POLICY, 'auto_apply_allowed': False,
        'meaning': 'Deviation from a varied music training distribution; not a learned professional preference'}


def assess_ranges(report, model):
    """Return signed role-band deviations and an anomaly cost, not mix quality."""
    import numpy as np

    if report['feature_names'] != list(FEATURE_NAMES) or [v['name'] for v in model['limits']] != list(FEATURE_NAMES):
        raise ValueError('Source feature schema mismatch')
    alerts, costs = [], []
    for i, limit in enumerate(model['limits']):
        if not report['valid'][i] or limit['lower'] is None:
            continue
        value = report['features'][i]
        deviation = min(value-limit['lower'], 0.)+max(value-limit['upper'], 0.)
        costs.append(deviation*deviation)
        if deviation:
            alerts.append({'feature': limit['name'], 'relative_db': value,
                'direction': 'dominant' if deviation > 0 else 'buried', 'outside_range_db': abs(deviation),
                'expected_range_db': [limit['lower'], limit['upper']]})
    return {'anomaly_cost': float(np.mean(costs)) if costs else None, 'alerts': alerts,
        'status': 'source_attribution_uncertain' if report['warnings'] else ('measured' if costs else 'insufficient_activity'),
        'meaning': 'Review alerts, not proof of a bad mix; silence/solos/intentional arrangements can differ',
        'auto_apply_allowed': False}
