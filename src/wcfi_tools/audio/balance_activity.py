"""Versioned shared-activity balance representation; saved-array CPU math only.

V2 is deliberately retained for frozen experiment replay. All coordinates here
describe the SAME raw-supported frames and one closed geometric composition.
Energy activity is not proof of performer activity; bleed remains a limitation.
"""
from __future__ import annotations

import itertools

from . import balance_calibration as previous
from .balance_calibration import ROLES, conformal_threshold, frame_evidence

__all__ = ['POLICY', 'ROLES', 'assess', 'fit', 'frame_evidence', 'vector']

POLICY = previous.POLICY | {
    'kind': 'wcfi_joint_source_balance_v3',
    'activity': 'Intersection of RAW active frames for all roles in the context',
    'minimum_shared_seconds': 1.,
    'pooling': 'Mean frame log powers on shared frames, normalized geometric composition',
    'candidate_floor': '1e-12 of total context power per frame; common-gain invariant',
    'alpha': .25, 'geometry': 'covariance',
    'calibration': 'Joint score per original song; independent calibration order statistic',
    'insufficient_support': 'Unknown, never clean; no fallback to different per-role windows',
}


def vector(evidence, *, powers=None, context=None):
    """Use candidate energies with immutable raw support, never a target raw mix."""
    import numpy as np

    raw = np.asarray(evidence['powers'], dtype=float)
    p = raw if powers is None else np.asarray(powers, dtype=float)
    active = np.asarray(evidence['activity'], dtype=bool)
    requested = evidence['present'] if context is None else context
    if set(requested)-set(ROLES) or len(requested) != len(set(requested)):
        raise ValueError('Use distinct known RAW roles')
    context = [r for r in ROLES if r in requested]
    if (raw.ndim != 2 or raw.shape[1] != len(ROLES) or len(raw) == 0
            or p.shape != raw.shape or active.shape != raw.shape
            or not np.isfinite(p).all() or np.any(p < 0)):
        raise ValueError('Invalid candidate frame energies or RAW activity shape')
    if len(context) < 2 or set(context)-set(evidence['present']):
        raise ValueError('Use at least two supported RAW roles; candidate muting cannot change context')
    indices = [ROLES.index(r) for r in context]
    support = np.all(active[:, indices], axis=1)
    count = int(support.sum())
    seconds = count*POLICY['frame_seconds']
    result = {'context': context, 'shared_frames': count, 'shared_seconds': seconds,
        'shared_fraction': count/len(p),
        'per_role_active_seconds': {r: float(active[:, i].sum()*POLICY['frame_seconds'])
            for r, i in zip(context, indices, strict=True)},
        'support_basis': 'RAW energy intersection, not confirmed performer activity',
        'relative_db': None, 'status': 'insufficient_coactivity'}
    if seconds < POLICY['minimum_shared_seconds']:
        return result
    selected = p[support][:, indices]
    totals = selected.sum(axis=1, keepdims=True)
    # With all candidates silent there is no relative mix. Do not fabricate a
    # uniform composition or drop silent candidate frames from the RAW support.
    if np.any(totals <= 0):
        return result | {'status': 'silent_candidate_frames'}
    shares = selected/totals
    log_power = np.log(np.maximum(shares, 1e-12)).mean(axis=0)
    composition = np.exp(log_power-log_power.max())
    composition /= composition.sum()
    relative = [float(10*np.log10(composition[i]/np.sum(np.delete(composition, i))))
        for i in range(len(context))]
    return result | {'status': 'supported', 'relative_db': relative,
        'composition': composition.tolist()}


def fit(training, calibration, *, geometry='covariance', alpha=.25):
    """Fixed v2 geometry/calibration with corrected coordinates and explicit coverage."""
    import numpy as np

    ids = [r['id'] for r in training+calibration]
    if len(ids) != len(set(ids)):
        raise ValueError('Song leakage between fit and calibration')
    if {r['artist'] for r in training} & {r['artist'] for r in calibration}:
        raise ValueError('Artist leakage between fit and calibration')
    if geometry not in ('axis', 'covariance', 'neighbors'):
        raise ValueError('Unknown joint geometry')
    contexts, coverage = {}, {}
    for size in range(2, len(ROLES)+1):
        for context in itertools.combinations(ROLES, size):
            def supported(rows, requested=context):
                eligible = [r for r in rows if set(requested) <= set(r['evidence']['present'])]
                measurements = [(r, vector(r['evidence'], context=requested)) for r in eligible]
                return [(r, v) for r, v in measurements if v['status'] == 'supported']
            train, calib = supported(training), supported(calibration)
            key = '|'.join(context)
            coverage[key] = {'fit_supported': len(train), 'calibration_supported': len(calib)}
            if (len(train) < POLICY['minimum_fit_songs']
                    or len(calib) < POLICY['minimum_calibration_songs']):
                continue
            x = np.array([v['relative_db'] for _, v in train])
            centre = np.median(x, axis=0)
            scale = np.maximum((np.quantile(x, .75, axis=0)-np.quantile(x, .25, axis=0))/1.349, 2.)
            embedding = previous._embedding(x)
            covariance = np.atleast_2d(np.cov(embedding, rowvar=False))
            covariance = .8*covariance+.2*np.eye(size)*max(float(np.trace(covariance)/size), 4.)
            entry = {'roles': list(context), 'centre': centre.tolist(), 'scale': scale.tolist(),
                'geometry': geometry, 'embedding_centre': np.mean(embedding, axis=0).tolist(),
                'precision': np.linalg.inv(covariance).tolist(),
                'embedding_scale': np.maximum(np.std(embedding, axis=0), 2.).tolist(),
                'reference': embedding.tolist()}
            scores = [previous._score(v['relative_db'], entry)[0] for _, v in calib]
            threshold = conformal_threshold(scores, alpha)
            if threshold is None:
                continue
            contexts[key] = entry | {'threshold': threshold, 'calibration_scores': scores,
                'training_ids': [r['id'] for r, _ in train],
                'calibration_ids': [r['id'] for r, _ in calib]}
    if not contexts:
        raise ValueError('Not enough independent songs with shared activity for calibration')
    return {'kind': POLICY['kind'], 'policy': POLICY | {'alpha': alpha, 'geometry': geometry},
        'contexts': contexts, 'context_coverage': coverage,
        'training_ids': [r['id'] for r in training],
        'calibration_ids': [r['id'] for r in calibration], 'auto_apply_allowed': False}


def assess(report, model):
    if model.get('kind') != POLICY['kind']:
        raise ValueError('Shared-activity representation requires a v3 model')
    if report['status'] != 'supported':
        return {'status': report['status'], 'score': None, 'alert': None, 'alerts': [],
            'reason': 'Insufficient simultaneous RAW support or silent candidate frames',
            'evidence': report, 'auto_apply_allowed': False}
    return previous.assess(report, model) | {'evidence': report}
