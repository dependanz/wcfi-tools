"""Fresh-recording validation of frozen balance models, saved files and one CPU."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from .profiles import file_hash, write_json


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def summarize(records, name):
    import numpy as np

    originals=[r[name] for r in records]
    supported=[r for r in originals if r['alert'] is not None]
    severe=[v for r in records for v in r['variants'] if abs(v['gain_db'])==12]
    def metric(v,key):
        return float(np.mean([x[name][key] for x in v])) if v else None
    return {'recordings':len(records),'assessed_originals':sum(r['alert'] is not None for r in originals),
        'original_alert_fraction':float(np.mean([bool(r['alert']) for r in supported])) if supported else None,
        'unknown_originals':sum(r['alert'] is None for r in originals),
        'severe_controls':len(severe),'severe_strict_original_win':metric(severe,'strict_original_win'),
        'severe_any_alert_recall':metric(severe,'any_alert'),
        'severe_affected_role_recall':metric(severe,'affected_alert'),
        'severe_unknowns':sum(v[name].get('unknown',False) for v in severe),
        'by_gain':{str(g):{'controls':sum(v['gain_db']==g for r in records for v in r['variants']),
            'any_alert_recall':metric([v for r in records for v in r['variants'] if v['gain_db']==g],'any_alert'),
            'strict_original_win':metric([v for r in records for v in r['variants'] if v['gain_db']==g],'strict_original_win')}
            for g in (-60.,-12.,-6.,6.,12.)}}


def run(directory, output, *, protocol_file=None, progress=print):
    from .master_benchmark_cpu import _deadline, configure_cpu, offline_connections

    configure_cpu()
    import numpy as np
    import soundfile as sf

    from ..audio import balance_calibration as new
    from ..audio.source_evaluation import SourceBank, assess_ranges
    from .mix_preference import bootstrap, resource_checks

    directory,output=Path(directory).resolve(),Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new immutable validation output directory')
    protocol_path=Path(protocol_file).resolve() if protocol_file else directory/'final-protocol.json'
    protocol=read(protocol_path)
    for p,expected in protocol['code_sha256'].items():
        if file_hash(Path(p))!=expected:
            raise ValueError('Frozen evaluator/runner code changed')
    model=read(directory/'sensitivity-model.json')
    if file_hash(directory/'sensitivity-model.json')!=protocol['primary_model_sha256']:
        raise ValueError('Frozen model changed')
    old_model=read(directory.parent/'source-aware-2026-10-22-01/run-01/source-controls/model.json')
    if file_hash(directory.parent/'source-aware-2026-10-22-01/run-01/source-controls/model.json')!=protocol['legacy_model_sha256']:
        raise ValueError('Legacy model changed')
    output.mkdir(parents=True)
    timer=_deadline(1800,output,'balance-validation')
    start,cpu=time.monotonic(),time.process_time()
    records=[]
    try:
        with offline_connections() as attempts:
            acquired=read(directory/'fresh-audio.json')
            expected_source_protocol=protocol.get('parent_protocol_sha256',file_hash(protocol_path))
            if acquired['protocol_sha256']!=expected_source_protocol:
                raise ValueError('Fresh source selection protocol changed')
            manifest=acquired['records']
            for entry in manifest:
                raw={}
                for role,info in entry['sources'].items():
                    path=Path(info['file'])
                    if file_hash(path)!=info['sha256']:
                        raise ValueError('Fresh saved source changed')
                    a,rate=sf.read(path,dtype='float32',always_2d=True)
                    raw[role]=a
                evidence=new.frame_evidence(raw,rate)
                bank=SourceBank(raw,{r:r for r in raw},rate)
                original_old=assess_ranges(bank.measure(raw,master_pre=sum(raw.values())),old_model)
                try:
                    original=new.assess(new.vector(evidence),model)
                except ValueError:
                    original={'status':'unsupported_single_role','score':None,'alert':None,'alerts':[]}
                record={'id':entry['id'],'dataset':entry['dataset'],'present':evidence['present'],
                    'old':{'alert':bool(original_old['alerts']) if original_old['anomaly_cost'] is not None else None,'details':original_old},
                    'new':original,'variants':[],'selection':entry['selection']}
                if len(evidence['present'])>=2:
                    for role in evidence['present']:
                        for gain in protocol['controls']:
                            powers=np.array(evidence['powers']);powers[:,new.ROLES.index(role)]*=10**(gain/10)
                            changed=new.assess(new.vector(evidence,powers=powers),model)
                            # Legacy is measured from the actual PCM mutation.
                            mutated={r:a*(10**(gain/20) if r==role else 1.) for r,a in raw.items()}
                            measured=bank.measure(mutated)
                            legacy=assess_ranges(measured,old_model)
                            direct=new.frame_evidence(mutated,rate)
                            # Replace candidate activity with original raw activity:
                            # a quieter candidate must never erase the source.
                            check=new.vector(evidence,powers=direct['powers'])
                            expected=new.vector(evidence,powers=powers)
                            if not np.allclose(check['relative_db'],expected['relative_db'],atol=1e-4):
                                raise RuntimeError('Power mutation disagrees with actual waveform')
                            known=changed['score'] is not None
                            old_delta=legacy['anomaly_cost']-original_old['anomaly_cost']
                            record['variants'].append({'role':role,'gain_db':gain,
                                'new':changed | {'strict_original_win':bool(known and changed['score']>original['score']+1e-9),
                                    'any_alert':bool(changed['alert']), 'unknown':not known,
                                    'affected_alert':any(a['role']==role and a['direction']==('dominant' if gain>0 else 'buried') for a in changed['alerts'])},
                                'old':{'strict_original_win':bool(old_delta>1e-9),'any_alert':bool(legacy['alerts']),
                                    'affected_alert':any(a['feature'].startswith(role+'_') and a['direction']==('dominant' if gain>0 else 'buried') for a in legacy['alerts'])}})
                # Common gain and source mute cannot change context/support.
                gain=new.frame_evidence({r:a*.25 for r,a in raw.items()},rate)
                record['gain_invariance_error_db']=0.
                if len(evidence['present'])>=2:
                    base=new.vector(evidence)
                    quiet=new.vector(evidence,powers=gain['powers'])
                    record['gain_invariance_error_db']=float(np.max(np.abs(np.array(base['relative_db'])-quiet['relative_db'])))
                records.append(record)
                resource_checks()
                progress('Fresh saved recording: '+entry['id'])
            results={'all':{name:summarize(records,name) for name in ('old','new')},
                'by_dataset':{dataset:{name:summarize([r for r in records if r['dataset']==dataset],name)
                    for name in ('old','new')} for dataset in sorted({r['dataset'] for r in records})}}
            old,new_result=results['all']['old'],results['all']['new']
            gate=bool(new_result['unknown_originals']==0 and new_result['original_alert_fraction']<=.2 and
                new_result['severe_any_alert_recall']>=old['severe_any_alert_recall'] and
                new_result['severe_affected_role_recall']>=old['severe_affected_role_recall'] and
                new_result['severe_strict_original_win']>=old['severe_strict_original_win'])
            newmiss=sum(v['old']['any_alert'] and not v['new']['any_alert'] for r in records for v in r['variants'] if abs(v['gain_db'])==12)
            recovered=sum(not v['old']['any_alert'] and v['new']['any_alert'] for r in records for v in r['variants'] if abs(v['gain_db'])==12)
            differences=[float(bool(r['new']['alert']))-float(bool(r['old']['alert'])) for r in records
                if r['new']['alert'] is not None and r['old']['alert'] is not None]
            results |= {'gate_pass':gate,'new_misses_previously_caught':newmiss,'recovered_previously_missed':recovered,
                'paired_original_alert_change_CI':bootstrap(differences),'paired_assessed_originals':len(differences),
                'maximum_common_gain_feature_error_db':max(r['gain_invariance_error_db'] for r in records),
                'claims':'Unchanged controls are not certified flawless; synthetic controls have no human quality labels. Different arrangements and dataset shift limit calibration.',
                'auto_apply_allowed':False}
            write_json(output/'predictions.json',{'records':records})
            write_json(output/'results.json',results)
            audit={'wall_seconds':round(time.monotonic()-start,2),'CPU_seconds':round(time.process_time()-cpu,2),
                'resources':resource_checks(),'network_attempts':len(attempts),'audio_device_calls':0,'DAW_calls':0,
                'desktop_calls':0,'protocol_sha256':file_hash(protocol_path),
                'fresh_audio_manifest_sha256':file_hash(directory/'fresh-audio.json')}
            write_json(output/'audit.json',audit)
            report='<html lang="en"><meta charset="utf-8"><title>Fresh source-balance test</title><style>body{font:17px/1.6 system-ui;background:#142127;color:#eceee9;max-width:1000px;margin:40px auto;padding:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1{color:#e2bb77}</style><h1>New recordings: source-balance validation</h1><p>Two real sparse recordings and 20 synthesized instrumental recordings. No new full-band vocal corpus. Original controls are not human-certified flawless mixes. No playback or listening requested; no automatic edits.</p><pre>'+html.escape(json.dumps(results | {'audit':audit},indent=2))+'</pre></html>'
            (output/'report.html').write_text(report,encoding='utf-8')
    finally:
        timer.cancel()
    return {'report':str(output/'report.html'),'results':results,'audit':audit}
