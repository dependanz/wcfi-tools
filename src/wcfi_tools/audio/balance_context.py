"""Conservative RAW-context guard, independent of the frozen balance models.

Energy support is not performer identification. This guard can veto a source-
prior recommendation but cannot establish an ideal fader setting or authorize
applying one. All thresholds are engineering heuristics, not calibrated
probabilities. Compute this once from RAW sources and reuse it for candidates.
"""
from __future__ import annotations

import itertools

from .balance_calibration import ROLES, frame_evidence

POLICY = {
    'kind': 'wcfi_raw_balance_context_v1',
    'minimum_active_seconds': 2.,
    'minimum_shared_seconds': 2.,
    'weak_mean_level_dbfs': -50.,
    'weak_role_to_rest_db': -25.,
    'minimum_active_modulation_db': 2.,
    'attack_step_db': 3.,
    'similar_waveform_correlation': .95,
    'activity_coordinates': 'Unchanged balance_calibration.frame_evidence RAW coordinates',
    'threshold_basis': 'Conservative fixed engineering objections; not learned or calibrated',
    'meaning': 'Supported means no observed objection, not confirmed performer identity',
    'candidate_policy': 'Reuse RAW result; never recompute context from a candidate mix',
}


def _correlation(left, right):
    """Zero-lag Pearson clue, retaining stereo channels and bounding temporary memory.

    Similarity can indicate bleed, duplicate routing, or musical unison. Lack of
    similarity does not prove independence; delayed/filtered bleed may evade it.
    """
    import numpy as np

    channels = max(left.shape[1], right.shape[1])
    count = len(left) * channels
    sx = sy = xx = yy = xy = 0.
    for start in range(0, len(left), 4096):
        left_block, right_block = left[start:start + 4096], right[start:start + 4096]
        shape = (len(left_block), channels)
        # Duplicate a mono channel only within this bounded block. Never fold
        # stereo to mono: opposing L/R polarity must retain its signal energy.
        x = np.broadcast_to(left_block, shape).astype('float64').reshape(-1)
        y = np.broadcast_to(right_block, shape).astype('float64').reshape(-1)
        sx += float(x.sum())
        sy += float(y.sum())
        xx += float(x @ x)
        yy += float(y @ y)
        xy += float(x @ y)
    vx, vy = max(0., xx - sx * sx / count), max(0., yy - sy * sy / count)
    if min(vx, vy) <= 1e-24:
        return None
    return float(np.clip((xy - sx * sy / count) / np.sqrt(vx * vy), -1., 1.))


def _raw_evidence(raw, sample_rate):
    """Assemble frozen energy columns for mixed mono/stereo without expanding audio.

    The original function requires matching channel counts. Its independent
    per-role coordinates are unchanged when computed separately; a duplicated
    mono channel has exactly the same mean channel power.
    """
    import numpy as np

    if not raw or set(raw) - set(ROLES):
        raise ValueError('Supply known vocals/drums/bass/other sources')
    arrays, evidence, present = {}, None, []
    length = None
    for role in ROLES:
        if role not in raw:
            continue
        column = frame_evidence({role: raw[role]}, sample_rate)
        x = np.asarray(raw[role], dtype='float32')
        if x.ndim == 1:
            x = x[:, None]
        if length is not None and len(x) != length:
            raise ValueError('Sources must be synchronized, 1–120 s')
        length = len(x)
        arrays[role] = x
        if evidence is None:
            evidence = column
        else:
            i = ROLES.index(role)
            for key in ('powers', 'activity'):
                for row, values in zip(evidence[key], column[key], strict=True):
                    row[i] = values[i]
        if role in column['present']:
            present.append(role)
    evidence['present'] = present
    return arrays, evidence


def assess_context(raw, sample_rate):
    """Qualify synchronized 1–120 s RAW role buses without changing their support.

    ``retained_roles`` includes every supplied role, even silent/weak ones.
    ``energy_supported_roles`` is the frozen model's original energy context.
    Per-role states are ``insufficient``, ``ambiguous``, or ``supported``; none
    assert that a performer is absent or present. ``automatic_balance_allowed``
    only means this guard has no veto. ``auto_apply_allowed`` is always false.
    """
    import numpy as np

    arrays, evidence = _raw_evidence(raw, sample_rate)  # validates before signal comparisons
    retained = list(arrays)
    powers = np.asarray(evidence['powers'], dtype=float)
    activity = np.asarray(evidence['activity'], dtype=bool)
    mean_powers = powers.mean(axis=0)
    seconds_per_frame = .25  # deliberately identical to the frozen coordinate system
    roles = {}
    for role in retained:
        i = ROLES.index(role)
        p = powers[:, i]
        active = activity[:, i]
        active_seconds = float(active.sum() * seconds_per_frame)
        level = float(10 * np.log10(max(mean_powers[i], 1e-20)))
        rest = float(mean_powers.sum() - mean_powers[i])
        relative = (float(10 * np.log10(max(mean_powers[i], 1e-20) / rest))
                    if rest > 1e-20 else None)
        db = 10 * np.log10(np.maximum(p, 1e-20))
        modulation = (float(np.quantile(db[active], .9) - np.quantile(db[active], .1))
                      if np.any(active) else 0.)
        steps = np.diff(db)
        attack_count = int(np.sum((steps >= POLICY['attack_step_db']) & active[1:]))
        structured = modulation >= POLICY['minimum_active_modulation_db'] or attack_count > 0
        reasons = []
        if active_seconds < POLICY['minimum_active_seconds']:
            reasons.append('insufficient_energy_duration')
        if level < POLICY['weak_mean_level_dbfs']:
            reasons.append('weak_absolute_energy')
        if relative is not None and relative < POLICY['weak_role_to_rest_db']:
            reasons.append('weak_energy_relative_to_other_raw_sources')
        if active_seconds and not structured:
            reasons.append('stationary_energy_or_sustained_arrangement')
        roles[role] = {
            'evidence_state': None, 'reasons': reasons,
            'energy_supported_by_frozen_model': role in evidence['present'],
            'active_seconds': active_seconds, 'mean_level_dbfs': level,
            'role_to_rest_db': relative, 'active_modulation_db': modulation,
            'upward_energy_steps': attack_count, 'temporal_structure_observed': structured,
            'similar_signals': [], 'performer_identity': 'unconfirmed',
        }

    similarities = []
    for left, right in itertools.combinations(retained, 2):
        correlation = _correlation(arrays[left], arrays[right])
        if correlation is not None and abs(correlation) >= POLICY['similar_waveform_correlation']:
            similarities.append({'roles': [left, right], 'zero_lag_correlation': correlation})
            for role, other in ((left, right), (right, left)):
                roles[role]['similar_signals'].append(other)
                if 'signal_similarity_needs_context' not in roles[role]['reasons']:
                    roles[role]['reasons'].append('signal_similarity_needs_context')

    for row in roles.values():
        row['evidence_state'] = ('insufficient'
            if 'insufficient_energy_duration' in row['reasons'] else
            'ambiguous' if row['reasons'] else 'supported')
    indices = [ROLES.index(r) for r in retained]
    shared_seconds = float(np.all(activity[:, indices], axis=1).sum() * seconds_per_frame)
    reasons = []
    if len(retained) < 2:
        reasons.append('fewer_than_two_supplied_roles')
    if shared_seconds < POLICY['minimum_shared_seconds']:
        reasons.append('insufficient_shared_raw_energy')
    if any(row['evidence_state'] != 'supported' for row in roles.values()):
        reasons.append('uncertain_role_context')
    return {
        'kind': POLICY['kind'], 'policy': POLICY.copy(), 'raw_evidence': evidence,
        'retained_roles': retained, 'energy_supported_roles': evidence['present'].copy(),
        'unsupplied_roles': [r for r in ROLES if r not in raw], 'roles': roles,
        'shared_seconds_all_supplied_roles': shared_seconds,
        'signal_similarity_clues': similarities, 'reasons': reasons,
        'automatic_balance_allowed': not reasons, 'auto_apply_allowed': False,
        'confidence': 'Uncalibrated engineering guard; no probability',
        'scope': ('Source-prior gain suggestions only. Candidate analysis remains available. '
                  'Do not discard weak sources, infer ideal levels from RAW gains, or '
                  'reinterpret frozen-model p-values as context confidence.'),
    }
