"""Read an explicit saved-source manifest, with no DAW/device/network access."""
from __future__ import annotations

import json
from pathlib import Path

from .profiles import file_hash


def inspect(manifest, *, model=None):
    import numpy as np
    import soundfile as sf

    from ..audio.source_evaluation import SourceBank, assess_ranges

    manifest = Path(manifest).resolve()
    spec = json.loads(manifest.read_text(encoding='utf-8'))
    if spec.get('kind') != 'wcfi_source_contributions_v1' or spec.get('stage') != 'master_pre_fx':
        raise ValueError('Use a wcfi_source_contributions_v1 manifest of Master pre-FX contributions')
    if spec.get('alignment_verified') is not True:
        raise ValueError('Verify source start times and compensate processing latency before analysis')
    start, seconds = float(spec.get('start_seconds', 0)), float(spec.get('duration_seconds', 7))
    if not 0 <= start < 86400 or not 1 <= seconds <= 120:
        raise ValueError('Use a finite start and 1–120 second analysis window')
    sources = spec['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 64:
        raise ValueError('Supply 1–64 labelled sources')
    rate, evidence = None, []

    def read(name):
        nonlocal rate
        path = (manifest.parent/name).resolve()
        with sf.SoundFile(path) as stream:
            if rate is None:
                rate = stream.samplerate
            if stream.samplerate != rate or stream.channels not in (1,2):
                raise ValueError('Source files must have one common sample rate and mono/stereo channels')
            stream.seek(round(start*rate))
            audio = stream.read(round(seconds*rate),dtype='float32',always_2d=True)
            if len(audio) != round(seconds*rate):
                raise ValueError('Source shorter than requested synchronized window')
        evidence.append({'file': str(path), 'sha256': file_hash(path)})
        # Represent mono sources as dual mono; candidate stereo pan is retained.
        return audio if audio.shape[1]==2 else np.repeat(audio,2,axis=1)

    raw, contributions, roles = {}, {}, {}
    for row in sources:
        name = row['id']
        if not isinstance(name,str) or name in raw:
            raise ValueError('Source IDs must be distinct strings')
        roles[name] = row['role']
        raw[name] = read(row['raw'])
        contributions[name] = read(row['contribution'])
    master = read(spec['master_pre']) if spec.get('master_pre') else None
    bank = SourceBank(raw,roles,rate)
    report = bank.measure(contributions,master_pre=master)
    if model:
        fitted=json.loads(Path(model).read_text(encoding='utf-8'))
        if fitted.get('kind') in ('wcfi_joint_source_balance_v2','wcfi_joint_source_balance_v3'):
            from ..audio import balance_activity, balance_calibration

            evaluator = balance_activity if fitted['kind'].endswith('_v3') else balance_calibration

            mapping={'vocals':'vocals','backing_vocals':'vocals','drums':'drums','bass':'bass',
                'guitar':'other','keys':'other','other':'other'}
            grouped_raw,grouped_contributions={},{}
            for name,role in roles.items():
                target=mapping[role]
                grouped_raw[target]=grouped_raw.get(target,np.zeros_like(raw[name]))+raw[name]
                grouped_contributions[target]=grouped_contributions.get(target,np.zeros_like(contributions[name]))+contributions[name]
            context=evaluator.frame_evidence(grouped_raw,rate)
            candidate=evaluator.frame_evidence(grouped_contributions,rate)
            from ..audio.balance_context_v2 import assess_context

            qualification = assess_context(grouped_raw, rate)
            report['source_context_qualification'] = qualification
            report['source_prior_recommendation_status'] = (
                'context_has_no_observed_objection'
                if qualification['automatic_balance_allowed'] else 'withheld_uncertain_context'
            )
            if len(context['present'])>=2:
                report['joint_balance_assessment']=evaluator.assess(
                    evaluator.vector(context,powers=candidate['powers']),fitted)
            else:
                report['joint_balance_assessment']={'status':'unsupported_single_role','alert':None,'auto_apply_allowed':False}
            report['joint_evaluation_role_mapping']=mapping
            report['joint_evaluation_note']='Guitar/keys/other share OTHER; backing joins VOCALS. This model cannot diagnose them separately.'
            report['context_qualification_note'] = (
                'Frozen balance scores remain diagnostic. Energy support does not confirm performer '
                'activity; an uncertain RAW context withholds source-prior gain advice without '
                'dropping roles, changing candidate support, or recalibrating the saved model. '
                'Sustained spectral structure can resolve a steady-level objection for held '
                'vocals, bass and other instruments; this is not instrument identification.'
            )
        else:
            report['range_assessment'] = assess_ranges(report,fitted)
        report['model_sha256'] = file_hash(Path(model))
    return report | {'manifest_sha256': file_hash(manifest), 'evidence': evidence,
        'sample_rate': rate, 'start_seconds': start, 'duration_seconds': seconds,
        'alignment': 'Caller-verified alignment and processing-latency compensation',
        'routing_reconstruction_checked': master is not None}
