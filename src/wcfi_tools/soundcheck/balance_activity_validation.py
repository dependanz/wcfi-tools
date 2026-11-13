"""Frozen saved-source tests of shared activity, with explicit unknown coverage.

This runner reads local research audio only. It does not connect to an audio
device, a DAW, the desktop, a network service, or a GPU runtime.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from .profiles import file_hash, write_json

NAMES = ('legacy', 'v2', 'v3')
CONTROLS = (-60., -12., -6., 6., 12.)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _path(value, base):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base/path).resolve()


def _mean(values):
    return sum(values)/len(values) if values else None


def known(assessment):
    return assessment.get('alert') is not None and assessment.get('score') is not None


def control_result(original, changed, role, gain):
    """An unknown original OR candidate cannot be credited as a detection/win."""
    supported = known(original) and known(changed)
    direction = 'dominant' if gain > 0 else 'buried'
    return changed | {
        'unknown': not supported,
        'unknown_original': not known(original),
        'unknown_candidate': not known(changed),
        'any_alert': bool(supported and changed['alert']),
        'affected_alert': bool(supported and any(
            a['role'] == role and a['direction'] == direction for a in changed['alerts'])),
        'strict_original_win': bool(supported and changed['score'] > original['score']+1e-9),
    }


def summarize(records, name):
    originals = [r[name] for r in records]
    assessed = [r for r in originals if known(r)]
    eligible = [r for r in records if r['eligible']]
    controls = [v for r in records for v in r['variants']]
    severe = [v for v in controls if abs(v['gain_db']) == 12]

    def metrics(rows):
        return {'controls': len(rows), 'unknowns': sum(v[name]['unknown'] for v in rows),
            'any_alert_recall': _mean([v[name]['any_alert'] for v in rows]),
            'affected_role_recall': _mean([v[name]['affected_alert'] for v in rows]),
            'strict_original_win': _mean([v[name]['strict_original_win'] for v in rows])}

    return {'recordings': len(records), 'assessed_originals': len(assessed),
        'unknown_originals': len(originals)-len(assessed),
        'original_alerts': sum(bool(r['alert']) for r in assessed),
        'original_alert_fraction': _mean([bool(r['alert']) for r in assessed]),
        'eligible_originals': len(eligible),
        'eligible_assessed_originals': sum(known(r[name]) for r in eligible),
        'eligible_coverage': _mean([known(r[name]) for r in eligible]),
        'severe': metrics(severe),
        'by_gain': {str(g): metrics([v for v in controls if v['gain_db'] == g]) for g in CONTROLS}}


def paired_comparison(records, comparator):
    """Keep switched misses visible even when aggregate recall is unchanged."""
    severe = [(r, v) for r in records for v in r['variants'] if abs(v['gain_db']) == 12]
    changes = {}
    for metric in ('any_alert', 'affected_alert', 'strict_original_win'):
        missed, recovered = [], []
        for record, variant in severe:
            entry = {'id': record['id'], 'role': variant['role'], 'gain_db': variant['gain_db']}
            if variant[comparator][metric] and not variant['v3'][metric]:
                missed.append(entry)
            if not variant[comparator][metric] and variant['v3'][metric]:
                recovered.append(entry)
        changes[metric] = {'new_misses': len(missed), 'recoveries': len(recovered),
            'new_miss_cases': missed, 'recovered_cases': recovered}
    clean = [r for r in records if known(r[comparator]) and known(r['v3'])
        and not r[comparator]['alert'] and not r['v3']['alert']]
    assessed = [r for r in records if known(r[comparator]) and known(r['v3'])]
    return {'severe_changes': changes,
        'paired_assessed_originals': len(assessed),
        'paired_original_alert_change': _mean([
            int(r['v3']['alert'])-int(r[comparator]['alert']) for r in assessed]),
        'matched_original_clean_recordings': len(clean),
        'matched_original_clean_ids': [r['id'] for r in clean],
        'matched_original_clean_controls': {name: summarize(clean, name)['severe']
            for name in (comparator, 'v3')},
        'meaning': 'Both models assessed the unchanged source mix as clean; not a human quality label'}


def results_for(records):
    summaries = {name: summarize(records, name) for name in NAMES}
    paired = {name: paired_comparison(records, name) for name in NAMES[:-1]}
    current = summaries['v3']
    checks = {'eligible_coverage_complete': current['eligible_coverage'] == 1.,
        'unchanged_alert_rate_at_most_20_percent': current['original_alert_fraction'] is not None
            and current['original_alert_fraction'] <= .2,
        'has_severe_controls': current['severe']['controls'] > 0}
    for name in NAMES[:-1]:
        for metric in ('any_alert_recall', 'affected_role_recall', 'strict_original_win'):
            a, b = current['severe'][metric], summaries[name]['severe'][metric]
            checks[f'{name}_{metric}_no_loss'] = a is not None and b is not None and a >= b
        checks[f'{name}_zero_new_severe_detection_misses'] = (
            paired[name]['severe_changes']['any_alert']['new_misses'] == 0)
    return {'all': summaries, 'paired': paired, 'gate_checks': checks,
        'gate_pass': all(checks.values()), 'auto_apply_allowed': False}


def acquisition_status(manifest, protocol):
    """A failed planned recording stays visible and prevents a successful gate."""
    failures = manifest.get('failures', [])
    if not isinstance(failures, list):
        raise TypeError('Manifest acquisition failures must be a list')
    expected = protocol.get('expected_recording_count')
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 1):
        raise ValueError('Expected recording count must be a positive integer')
    acquired = len(manifest['records'])
    return {'expected_recording_count': expected, 'acquired_recordings': acquired,
        'failed_recordings': len(failures), 'failures': failures,
        'complete': not failures and (expected is None or acquired == expected),
        'meaning': 'Failed acquisitions are not replaced or counted as clean; successful-file metrics exclude them'}


def _legacy_assessment(measurement, model):
    from ..audio.source_evaluation import assess_ranges

    result = assess_ranges(measurement, model)
    score = result['anomaly_cost']
    alerts = [a | {'role': a['feature'].split('_')[0]} for a in result['alerts']]
    return result | {'score': score, 'alert': bool(alerts) if score is not None else None,
        'alerts': alerts}


def _joint_assessment(module, evidence, model, powers=None):
    if len(evidence['present']) < 2:
        return {'status': 'unsupported_single_role', 'score': None, 'alert': None,
            'alerts': [], 'auto_apply_allowed': False}
    return module.assess(module.vector(evidence, powers=powers), model)


def _invariance_check(original, changed, tolerance):
    if known(original) != known(changed) or original['alert'] != changed['alert']:
        raise RuntimeError('Identity/common gain changed assessment support or alert')
    error = abs(original['score']-changed['score']) if known(original) else 0.
    if error > tolerance:
        raise RuntimeError('Identity/common gain changed balance score')
    return error


def _vector_error(a, b):
    import numpy as np

    if a['context'] != b['context'] or (a['relative_db'] is None) != (b['relative_db'] is None):
        raise RuntimeError('Power mutation changed fixed raw support/context')
    if a['relative_db'] is None:
        if a.get('status') != b.get('status'):
            raise RuntimeError('Power mutation changed unknown status')
        return 0.
    error = float(np.max(np.abs(np.array(a['relative_db'])-b['relative_db'])))
    if error > 1e-4:
        raise RuntimeError('Power mutation disagrees with actual waveform')
    return error


def _frozen_inputs(manifest_path, protocol_path):
    protocol = read(protocol_path)
    if file_hash(manifest_path) != protocol['manifest_sha256']:
        raise ValueError('Frozen source manifest changed')
    if tuple(protocol['controls']) != CONTROLS:
        raise ValueError('Use the fixed source gain controls [-60,-12,-6,6,12]')
    # Freeze the runner and all three evaluator implementations, not just weights.
    required = [Path(__file__)] + [Path(__file__).parent.parent/'audio'/name for name in (
        'source_evaluation.py', 'balance_calibration.py', 'balance_activity.py')]
    code = {_path(p, protocol_path.parent): h for p, h in protocol['code_sha256'].items()}
    if not {p.resolve() for p in required} <= set(code):
        raise ValueError('Protocol must freeze the runner and all evaluator implementations')
    for path, expected in code.items():
        if file_hash(path) != expected:
            raise ValueError('Frozen evaluator/runner code changed: '+str(path))
    models = {}
    for name in NAMES:
        path = _path(protocol['model_paths'][name], protocol_path.parent)
        if file_hash(path) != protocol['model_sha256'][name]:
            raise ValueError('Frozen model changed: '+name)
        models[name] = read(path)
    manifest = read(manifest_path)
    ids = [r['id'] for r in manifest['records']]
    if (not ids and not manifest.get('failures')) or len(ids) != len(set(ids)):
        raise ValueError('Use unique recording IDs and preserve every failed planned acquisition')
    acquisition_status(manifest, protocol)
    return protocol, manifest, models


def run(manifest, output, protocol_file, *, progress=print):
    """Evaluate frozen models on saved files; immutable output and no playback."""
    from .master_benchmark_cpu import _deadline, configure_cpu, offline_connections

    configure_cpu()
    import numpy as np
    import soundfile as sf

    from ..audio import balance_activity as v3
    from ..audio import balance_calibration as v2
    from ..audio.source_evaluation import SourceBank
    from .mix_preference import resource_checks

    manifest_path, output, protocol_path = (Path(p).resolve()
        for p in (manifest, output, protocol_file))
    if output.exists():
        raise ValueError('Choose a new immutable validation output directory')
    protocol, source_manifest, models = _frozen_inputs(manifest_path, protocol_path)
    output.mkdir(parents=True)
    timer = _deadline(1800, output, 'balance-activity-validation')
    start, cpu = time.monotonic(), time.process_time()
    records = []
    try:
        with offline_connections() as attempts:
            resource_checks()
            for entry in source_manifest['records']:
                raw, audio_format = {}, None
                if not entry['sources'] or set(entry['sources'])-set(v2.ROLES):
                    raise ValueError('Use explicit vocals/drums/bass/other sources')
                for role, source in entry['sources'].items():
                    path = _path(source['file'], manifest_path.parent)
                    info = sf.info(path)
                    fmt = (info.samplerate, info.frames, info.channels)
                    if (not 16000 <= info.samplerate <= 96000 or info.channels not in (1, 2)
                            or not info.samplerate <= info.frames <= info.samplerate*120):
                        raise ValueError('Use finite aligned mono/stereo excerpts of 1–120 seconds')
                    if audio_format is not None and fmt != audio_format:
                        raise ValueError('Sources must share sample rate, frame count and channels')
                    for key, actual in zip(('sample_rate', 'frames', 'channels'), fmt, strict=True):
                        if key in source and source[key] != actual:
                            raise ValueError('Source audio metadata changed: '+key)
                    if file_hash(path) != source['sha256']:
                        raise ValueError('Saved source hash changed: '+str(path))
                    samples, rate = sf.read(path, dtype='float32', always_2d=True)
                    if not np.isfinite(samples).all():
                        raise ValueError('Source contains nonfinite PCM')
                    raw[role], audio_format = samples, fmt
                evidence = v2.frame_evidence(raw, rate)
                eligible = len(evidence['present']) >= 2
                bank = SourceBank(raw, {r: r for r in raw}, rate)

                def evaluate(pcm, powers, bank=bank, evidence=evidence):
                    return {'legacy': _legacy_assessment(bank.measure(pcm), models['legacy']),
                        'v2': _joint_assessment(v2, evidence, models['v2'], powers),
                        'v3': _joint_assessment(v3, evidence, models['v3'], powers)}

                baseline = evaluate(raw, evidence['powers'])
                identity = evaluate({r: a.copy() for r, a in raw.items()},
                    np.array(evidence['powers']).copy())
                common_pcm = {r: a*.25 for r, a in raw.items()}
                common_powers = v2.frame_evidence(common_pcm, rate)['powers']
                common = evaluate(common_pcm, common_powers)
                checks = {'identity_score_error': {}, 'common_gain_score_error': {},
                    'common_gain_feature_error_db': {}, 'pcm_power_feature_max_error_db': 0.,
                    'pcm_power_controls_checked': 0}
                for name in NAMES:
                    checks['identity_score_error'][name] = _invariance_check(baseline[name], identity[name], 1e-10)
                    checks['common_gain_score_error'][name] = _invariance_check(baseline[name], common[name], 1e-4)
                if eligible:
                    for name, module in (('v2', v2), ('v3', v3)):
                        checks['common_gain_feature_error_db'][name] = _vector_error(
                            module.vector(evidence), module.vector(evidence, powers=common_powers))
                record = {'id': entry['id'], 'dataset': entry['dataset'], 'eligible': eligible,
                    'present': evidence['present'], 'audio_format': dict(zip(
                        ('sample_rate', 'frames', 'channels'), audio_format, strict=True)),
                    'sources': entry['sources'], 'selection': entry.get('selection'),
                    'raw_activity': evidence['activity'], 'checks': checks, **baseline, 'variants': []}
                record.update({key: entry[key] for key in ('title', 'recording_protocol', 'reconstruction')
                    if key in entry})
                if eligible:
                    for role in evidence['present']:
                        for gain in protocol['controls']:
                            powers = np.array(evidence['powers'])
                            powers[:, v2.ROLES.index(role)] *= 10**(gain/10)
                            pcm = {r: a*(10**(gain/20) if r == role else 1.) for r, a in raw.items()}
                            changed = evaluate(pcm, powers)
                            direct = v2.frame_evidence(pcm, rate)['powers']
                            for module in (v2, v3):
                                error = _vector_error(module.vector(evidence, powers=powers),
                                    module.vector(evidence, powers=direct))
                                checks['pcm_power_feature_max_error_db'] = max(
                                    checks['pcm_power_feature_max_error_db'], error)
                            checks['pcm_power_controls_checked'] += 1
                            record['variants'].append({'role': role, 'gain_db': gain,
                                **{name: control_result(baseline[name], changed[name], role, gain) for name in NAMES}})
                records.append(record)
                write_json(output/f'record-{len(records):04d}.json', record)
                resource_checks()
                progress('Saved-source activity test: '+entry['id'])
            if attempts:
                raise RuntimeError('Unexpected network attempt in offline validation')
            results = results_for(records)
            results['acquisition'] = acquisition_status(source_manifest, protocol)
            results['gate_checks']['complete_planned_acquisition'] = results['acquisition']['complete']
            results['gate_pass'] = all(results['gate_checks'].values())
            results['by_dataset'] = {dataset: results_for([r for r in records if r['dataset'] == dataset])
                for dataset in sorted({r['dataset'] for r in records})}
            results['claims'] = ('Unchanged controls are not human-certified good mixes. Synthetic gain controls '
                'test anomaly detection, not professional quality. Unknowns count as missed controls and '
                'are excluded from assessed-original alert rates; coverage is reported separately.')
            audit = {'wall_seconds': round(time.monotonic()-start, 2),
                'CPU_seconds': round(time.process_time()-cpu, 2), 'resources': resource_checks(),
                'network_attempts': len(attempts), 'audio_device_calls': 0, 'DAW_calls': 0,
                'desktop_calls': 0, 'protocol_sha256': file_hash(protocol_path),
                'manifest_sha256': file_hash(manifest_path),
                'maximum_pcm_power_feature_error_db': max(
                    (r['checks']['pcm_power_feature_max_error_db'] for r in records), default=0.),
                'pcm_power_controls_checked': sum(r['checks']['pcm_power_controls_checked'] for r in records)}
            write_json(output/'predictions.json', {'records': records})
            write_json(output/'results.json', results)
            write_json(output/'audit.json', audit)
            report = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Shared activity validation</title>'
                '<style>body{font:17px/1.6 system-ui;background:#142127;color:#eceee9;max-width:1100px;'
                'margin:40px auto;padding:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1{color:#e2bb77}</style>'
                '<h1>Shared activity: frozen source-balance validation</h1><p>'+html.escape(results['claims'])+
                '</p><p>No playback, desktop or DAW controls; CPU only. Automatic editing remains disabled.</p><pre>'+
                html.escape(json.dumps(results | {'audit': audit}, indent=2))+'</pre></html>')
            (output/'report.html').write_text(report, encoding='utf-8')
    finally:
        timer.cancel()
    return {'report': str(output/'report.html'), 'results': results, 'audit': audit}
