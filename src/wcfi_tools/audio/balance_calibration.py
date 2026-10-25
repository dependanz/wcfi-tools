"""Jointly calibrated source-balance alerts; no universal spectral targets.

Full-band feed balance is separated from spectral colour. Source activity is
fixed from raw signals. Calibration is at song level, including the maximum
over roles; this controls multiplicity only under exchangeability assumptions.
It cannot validate intentional arrangements, bleed, or professional quality.
"""
from __future__ import annotations

import itertools
import math

ROLES = ('vocals', 'drums', 'bass', 'other')
POLICY = {
    'kind': 'wcfi_joint_source_balance_v2', 'roles': list(ROLES), 'frame_seconds': .25,
    'activity_floor_dbfs': -70., 'activity_below_p90_db': 18., 'minimum_activity_seconds': 1.,
    'scale': 'Training median and IQR/1.349, minimum 2 dB; full-band role versus rest only',
    'calibration': 'Maximum absolute standardized role deviation per original song; split-conformal order statistic ceil((n+1)*(1-alpha))',
    'alpha': .1, 'minimum_fit_songs': 20, 'minimum_calibration_songs': 20,
    'contexts': 'Every >=2-role subset of training sources; omissions simulated in RAW context, never inferred from a turned-down candidate',
    'spectral_bands': 'Descriptive tonal evidence only, not independent balance alarms',
    'safety': 'No input relative levels, candidate identity, gain labels or test outcomes used to predict target balance',
    'claims': 'Anomaly relative to research music; no human quality labels. Nominal calibration not guaranteed under domain shift.',
    'auto_apply_allowed': False,
}


def frame_evidence(raw, rate):
    """Cache actual source energy, with channel power retained and bounded memory."""
    import numpy as np

    if set(raw)-set(ROLES) or not raw or not 16000 <= rate <= 96000:
        raise ValueError('Supply known vocals/drums/bass/other sources at 16–96 kHz')
    arrays = {}
    shape = None
    width = round(rate*POLICY['frame_seconds'])
    for role, samples in raw.items():
        x = np.asarray(samples, dtype='float32')
        if x.ndim == 1:
            x = x[:,None]
        if x.ndim != 2 or x.shape[1] not in (1,2) or not np.isfinite(x).all():
            raise ValueError('Use finite mono/stereo raw audio')
        if shape is None:
            shape = x.shape
        if x.shape != shape or not rate <= len(x) <= 120*rate:
            raise ValueError('Sources must be synchronized, 1–120 s')
        count = len(x)//width
        arrays[role] = np.mean(x[:count*width].reshape(count,width,-1).astype('float64')**2,axis=(1,2))
    powers = np.column_stack([arrays.get(r,np.zeros(count)) for r in ROLES])
    db = 10*np.log10(np.maximum(powers,1e-20))
    active = db >= np.maximum(POLICY['activity_floor_dbfs'],np.quantile(db,.9,axis=0)-POLICY['activity_below_p90_db'])
    present = [r for i,r in enumerate(ROLES) if active[:,i].sum()*POLICY['frame_seconds'] >= POLICY['minimum_activity_seconds']]
    return {'powers':powers.tolist(),'activity':active.tolist(),'present':present,
        'frames':count,'sample_rate':rate,'meaning':'Raw signal energy support; not confirmed performer activity'}


def vector(evidence, *, powers=None, context=None):
    """Actual candidate powers or raw powers. No candidate gain is accepted here."""
    import numpy as np

    raw = np.asarray(evidence['powers'],dtype=float)
    p = raw if powers is None else np.asarray(powers,dtype=float)
    activity = np.asarray(evidence['activity'],dtype=bool)
    context = tuple(r for r in ROLES if r in (evidence['present'] if context is None else context))
    if p.shape != raw.shape or p.ndim != 2 or p.shape[1] != len(ROLES) or not np.isfinite(p).all() or np.any(p<0):
        raise ValueError('Invalid candidate frame energies')
    if activity.shape != p.shape or len(context)<2 or set(context)-set(evidence['present']):
        raise ValueError('Use at least two supported RAW roles; muting a candidate must not change context')
    indices = [ROLES.index(r) for r in context]
    values=[]
    for index in indices:
        other = np.sum(p[:,[i for i in indices if i!=index]],axis=1)
        ratio=10*np.log10(np.maximum(p[:,index],1e-20))-10*np.log10(np.maximum(other,1e-20))
        values.append(float(np.median(np.clip(ratio[activity[:,index]],-60,60))))
    return {'context':list(context),'relative_db':values}


def nonconformity(values, centre, scale):
    import numpy as np

    z=(np.asarray(values)-np.asarray(centre))/np.asarray(scale)
    return float(np.max(np.abs(z))),z.tolist()


def conformal_threshold(scores, alpha):
    """Conservative finite-sample order statistic; never interpolate the tail."""
    if not scores or not 0<alpha<1 or any(not math.isfinite(x) for x in scores):
        raise ValueError('Use finite calibration scores and 0 < alpha < 1')
    index=math.ceil((len(scores)+1)*(1-alpha))
    return sorted(scores)[index-1] if index<=len(scores) else None


def _embedding(values):
    import numpy as np

    x=np.asarray(values,dtype=float)
    log_share=-10*np.log10(1+10**(-x/10))
    return log_share-np.mean(log_share,axis=-1,keepdims=True)


def _score(values, entry):
    import numpy as np

    geometry=entry.get('geometry','axis')
    if geometry=='axis':
        return nonconformity(values,entry['centre'],entry['scale'])
    x=_embedding(values)
    if geometry=='covariance':
        delta=x-np.asarray(entry['embedding_centre'])
        score=float(np.sqrt(max(float(delta@np.asarray(entry['precision'])@delta),0)))
        return score,(delta/entry['embedding_scale']).tolist()
    reference=np.asarray(entry['reference'])
    difference=(x-reference)/np.asarray(entry['embedding_scale'])
    distance=np.sqrt(np.mean(difference**2,axis=1))
    nearest=np.argsort(distance)[:3]
    return float(np.mean(distance[nearest])),np.mean(difference[nearest],axis=0).tolist()


def fit(training, calibration, *, geometry='axis', alpha=.1):
    """Different complete songs AND artists in fit/calibration; final tests excluded."""
    import numpy as np

    ids=[r['id'] for r in training+calibration]
    if geometry not in ('axis','covariance','neighbors'):
        raise ValueError('Unknown joint geometry')
    if len(ids)!=len(set(ids)):
        raise ValueError('Song leakage between fit and calibration')
    if {r['artist'] for r in training} & {r['artist'] for r in calibration}:
        raise ValueError('Artist leakage between fit and calibration')
    contexts={}
    for n in range(2,len(ROLES)+1):
        for context in itertools.combinations(ROLES,n):
            available=lambda rows,c=context:[r for r in rows if set(c)<=set(r['evidence']['present'])]
            train,calib=available(training),available(calibration)
            if len(train)<POLICY['minimum_fit_songs'] or len(calib)<POLICY['minimum_calibration_songs']:
                continue
            x=np.array([vector(r['evidence'],context=context)['relative_db'] for r in train])
            centre=np.median(x,axis=0)
            scale=np.maximum((np.quantile(x,.75,axis=0)-np.quantile(x,.25,axis=0))/1.349,2.)
            embedding=_embedding(x)
            covariance=np.atleast_2d(np.cov(embedding,rowvar=False))
            covariance=.8*covariance+.2*np.eye(len(context))*max(float(np.trace(covariance)/len(context)),4.)
            entry={'roles':list(context),'centre':centre.tolist(),'scale':scale.tolist(),
                'geometry':geometry,'embedding_centre':np.mean(embedding,axis=0).tolist(),
                'precision':np.linalg.inv(covariance).tolist(),
                'embedding_scale':np.maximum(np.std(embedding,axis=0),2.).tolist(),
                'reference':embedding.tolist()}
            scores=[_score(vector(r['evidence'],context=context)['relative_db'],entry)[0] for r in calib]
            threshold=conformal_threshold(scores,alpha)
            if threshold is None:
                continue
            contexts['|'.join(context)]=entry | {
                'threshold':threshold,'calibration_scores':scores,'training_ids':[r['id'] for r in train],
                'calibration_ids':[r['id'] for r in calib]}
    if not contexts:
        raise ValueError('Not enough complete independent songs for joint calibration')
    return {'kind':POLICY['kind'],'policy':POLICY | {'alpha':alpha,'geometry':geometry},'contexts':contexts,
        'training_ids':[r['id'] for r in training],'calibration_ids':[r['id'] for r in calibration],
        'auto_apply_allowed':False}


def assess(report, model):
    key='|'.join(report['context'])
    entry=model['contexts'].get(key)
    if entry is None:
        return {'status':'uncalibrated_context','score':None,'alert':None,'alerts':[],
            'reason':'No supported training/calibration context; not a clean bill of health','auto_apply_allowed':False}
    score,z=_score(report['relative_db'],entry)
    scores=entry['calibration_scores']
    p=(1+sum(s>=score for s in scores))/(len(scores)+1)
    alerts=[]
    for role,value,normalized in zip(report['context'],report['relative_db'],z,strict=True):
        if score>entry['threshold'] and abs(normalized)>=.6*max(abs(v) for v in z):
            alerts.append({'role':role,'direction':'dominant' if normalized>0 else 'buried',
                'relative_db':value,'standardized_deviation':normalized,
                'attribution':'Associated direction, not confirmed causal source'})
    return {'status':'anomaly' if alerts else 'within_calibrated_range','score':score,'alert':bool(alerts),
        'alerts':alerts,'joint_p_value':p,'threshold':entry['threshold'],
        'auto_apply_allowed':False,'meaning':'Unusual feed balance; not a human quality judgment'}
