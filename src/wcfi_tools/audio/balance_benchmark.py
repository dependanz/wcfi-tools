"""Fresh source-balance challenges with a frozen, CPU-only paired comparison.

The v1 audio and scores remain intact. New controls use the same development
recordings, not unseen performances. Passing is not professional-quality proof.
"""
from __future__ import annotations

import hashlib
import inspect
import random
import shutil
import time
from pathlib import Path

import numpy as np

from . import full_mix_benchmark as base
from . import source_balance
from .role_processing import _bell

CONTROLS = {
    'identical_new': ('Identical short-room control', 'null'),
    'gain_new': ('Whole mix ×0.37, then loudness matched', 'null'),
    'ac_small_up': ('Acoustic +2 dB', 'benign'),
    'ac_small_down': ('Acoustic −3 dB', 'benign'),
    'ac_pan_swap': ('Acoustic pan swapped, same channel power', 'benign'),
    'ac_up14': ('Acoustic +14 dB', 'fault'),
    'ac_up21': ('Acoustic +21 dB', 'fault'),
    'ac_body': ('Acoustic +18 dB bell at 350 Hz, Q 0.7', 'fault'),
    'ac_surge': ('Acoustic +24 dB for two seconds', 'fault'),
    'electric_up18': ('Electric guitar +18 dB', 'fault'),
    'keys_up20': ('Keys +20 dB', 'fault'),
    'bass_up18': ('Bass +18 dB', 'fault'),
    'lead_down12': ('RED −12 dB', 'fault'),
    'kit_down12': ('KIT −12 dB', 'fault'),
    'lead_up14': ('RED +14 dB', 'fault'),
    'kit_up18': ('KIT +18 dB', 'fault'),
}
POLICY = {'version': 2, 'kind': 'source_balance_veto_benchmark', 'source_balance': source_balance.POLICY,
          'style': base.POLICY, 'tie_tolerance': .005, 'audition_target_lufs': -30.,
          'gate': 'All anchor, new-control, null, repeat and order checks must pass. Fault detection requires an actual balance violation, not mere uncertainty.',
          'professional_quality_judgment_validated': False, 'automatic_host_apply': False}


def implementation_digest():
    parts = [Path(source_balance.__file__).read_bytes(),
             Path(base.__file__).read_bytes(), inspect.getsource(new_controls).encode('utf-8'),
             inspect.getsource(match_audio).encode('utf-8')]
    return hashlib.sha256(b'\n'.join(parts)).hexdigest()


def new_controls(shaped):
    dry = sum(shaped.values());anchor = dry+base.ambience(shaped, .7, -16)
    def change(name, gain):
        return anchor+shaped[name]*(10**(gain/20)-1)
    surge = np.zeros(len(anchor));surge[round(4.25*base.RATE):round(6.25*base.RATE)] = 24
    # Smooth 10 ms changes avoid making a click the evidence for a balance fault.
    ramp = 480
    start, end = round(4.25*base.RATE), round(6.25*base.RATE)
    surge[start:start+ramp] = np.linspace(0, 24, ramp)
    surge[end-ramp:end] = np.linspace(24, 0, ramp)
    return {'identical_new': anchor, 'gain_new': anchor*.37,
        'ac_small_up': change('AC GUITAR', 2), 'ac_small_down': change('AC GUITAR', -3),
        'ac_pan_swap': anchor-shaped['AC GUITAR']+shaped['AC GUITAR'][:, ::-1],
        'ac_up14': change('AC GUITAR', 14), 'ac_up21': change('AC GUITAR', 21),
        'ac_body': anchor-shaped['AC GUITAR']+base.filter_periodic(shaped['AC GUITAR'], _bell(350, 18, .7)),
        'ac_surge': anchor+shaped['AC GUITAR']*(10**(surge[:, None]/20)-1),
        'electric_up18': change('EGUITAR', 18), 'keys_up20': change('KEYS', 20),
        'bass_up18': change('BASS', 18), 'lead_down12': change('RED', -12),
        'kit_down12': change('KIT', -12), 'lead_up14': change('RED', 14), 'kit_up18': change('KIT', 18)}


def match_audio(a, path, ffmpeg):
    """One quieter common target preserves extreme-control peak headroom."""
    a = np.asarray(a, dtype=float);a = a/max(base.power(a), 1e-24)**.5*.05
    base.sf.write(path, a, base.RATE, subtype='FLOAT')
    observed = base.scan_loudness(path, ffmpeg);target = POLICY['audition_target_lufs']
    gain = target-observed['integrated_lufs']
    if observed['true_peak_dbtp']+gain > -3:
        raise ValueError('Balance controls exceed common audition peak headroom')
    for _ in range(3):
        base.sf.write(path, a*10**(gain/20), base.RATE, subtype='FLOAT')
        checked = base.scan_loudness(path, ffmpeg)
        if abs(checked['integrated_lufs']-target) <= .11:
            break
        gain += target-checked['integrated_lufs']
    if abs(checked['integrated_lufs']-target) > .15 or checked['true_peak_dbtp'] > -2.85:
        raise ValueError('Balance controls failed common loudness verification')
    return {k: checked[k] for k in ('integrated_lufs', 'true_peak_dbtp')}


def prepare(previous, output, *, ffmpeg, seed=2026102202, progress=None):
    previous = Path(previous).resolve();output = Path(output)
    if output.exists():
        raise ValueError('Use a new immutable balance benchmark directory')
    old = base.load(previous/'blind-inputs.json');key = base.load(previous/'private-key.json')
    freeze = base.load(previous/'freeze.json')
    if (base.digest(previous/'blind-inputs.json') != freeze['inputs_sha256']
            or base.digest(previous/'private-key.json') != freeze['key_sha256']):
        raise ValueError('Previous frozen benchmark changed')
    output.mkdir(parents=True);assets = output/'assets';assets.mkdir()
    base.save(output/'policy.json', POLICY)
    public = {'kind': 'wcfi_balance_blind_inputs', 'items': [], 'sessions': [], 'references': {},
              'policy_sha256': base.digest(output/'policy.json'), 'implementation_sha256': implementation_digest(),
              'parent_inputs_sha256': freeze['inputs_sha256']}
    private = {'items': {}, 'sources': key['sources'], 'plans': key['plans']}
    rng = random.Random(seed)
    for session in old['sessions']:
        sid = session['id'];raw, levels, entries = {}, {}, {}
        for name, entry in session['sources'].items():
            a = base.checked_asset(previous, entry);raw[name] = a
            shutil.copyfile(previous/'assets'/entry['file'], assets/entry['file']);entries[name] = entry.copy()
            levels[name] = (base.scan_loudness(assets/entry['file'], ffmpeg)
                            if base.power(a) > 1e-10 else {'integrated_lufs': None})
        _, shaped, _ = base.source_versions(raw, levels)
        public['sessions'].append({'id': sid, 'sources': entries})
        old_items = {key['items'][e['id']]['variant']: e for e in old['items'] if e['session'] == sid}
        for name in base.CANDIDATES:
            entry = old_items[name];base.checked_asset(previous, entry)
            identifier = f'{rng.getrandbits(96):024x}';filename = f'MIX-fmb_{identifier}.wav'
            matching = match_audio(base.checked_asset(previous, entry), assets/filename, ffmpeg)
            public['items'].append({'id': identifier, 'session': sid, 'file': filename,
                'sha256': base.digest(assets/filename), 'matching': matching})
            private['items'][identifier] = {'variant': name, 'label': base.LABELS[name], 'candidate': True, 'expectation': 'candidate'}
        for name, a in new_controls(shaped).items():
            identifier = f'{rng.getrandbits(96):024x}';filename = f'MIX-fmb_{identifier}.wav'
            matching = match_audio(a, assets/filename, ffmpeg)
            public['items'].append({'id': identifier, 'session': sid, 'file': filename,
                'sha256': base.digest(assets/filename), 'matching': matching})
            private['items'][identifier] = {'variant': name, 'label': CONTROLS[name][0],
                                            'candidate': False, 'expectation': CONTROLS[name][1]}
        if progress:
            progress(f'Prepared {sid}: 8 unchanged candidates and 16 fresh balance controls')
    for name, entry in old['references'].items():
        matching = match_audio(base.checked_asset(previous, entry), assets/entry['file'], ffmpeg)
        public['references'][name] = {'file': entry['file'], 'sha256': base.digest(assets/entry['file']), 'matching': matching}
    rng.shuffle(public['items'])
    base.save(output/'blind-inputs.json', public);base.save(output/'private-key.json', private)
    frozen = {'inputs_sha256': base.digest(output/'blind-inputs.json'),
              'policy_sha256': base.digest(output/'policy.json'), 'key_sha256': base.digest(output/'private-key.json'),
              'implementation_sha256': public['implementation_sha256'], 'test_controls_scored_before_freeze': False}
    base.save(output/'freeze.json', frozen)
    return {'versions': len(public['items']), 'new_controls': 48, 'candidates': 24, 'references': 2, **frozen}


def run(directory, output, *, progress=None):
    """No private-key access: compute old scores and new vetoes on the same files."""
    directory = Path(directory);output = Path(output);start = time.monotonic();cpu_start = time.process_time()
    if output.exists():
        raise ValueError('Use a new immutable CPU run directory')
    public = base.load(directory/'blind-inputs.json')
    if (base.load(directory/'policy.json') != POLICY or base.digest(directory/'policy.json') != public['policy_sha256']
            or implementation_digest() != public['implementation_sha256']):
        raise ValueError('Frozen evaluator policy or implementation changed')
    output.mkdir(parents=True)
    references = {k: base.observations(base.checked_asset(directory, e)) for k, e in public['references'].items()}
    rows, repeats = [], []
    for session in public['sessions']:
        raw = {k: base.checked_asset(directory, e) for k, e in session['sources'].items()}
        old = base.SourceProjection(raw);analyzer = source_balance.BalanceAnalyzer(raw)
        for index, entry in enumerate(e for e in public['items'] if e['session'] == session['id']):
            a = base.checked_asset(directory, entry);metrics = base.observations(a)
            legacy = base.score(metrics, old.evaluate(a), references, base.POLICY)
            balance = analyzer.evaluate(a);evaluation = source_balance.veto(legacy, balance)
            rows.append({'id': entry['id'], 'session': session['id'], 'sha256': entry['sha256'],
                         'metrics': metrics, 'legacy_evaluation': legacy, 'evaluation': evaluation})
            if index == 0:
                repeats.append({'session': session['id'], 'exact_repeat': analyzer.evaluate(a) == balance})
        if progress:
            progress(f'Scored {session["id"]}: 24 opaque mixes with both evaluators')
    result = {'kind': 'wcfi_balance_blind_run', 'rows': rows, 'references': references, 'repeats': repeats,
        'policy_sha256': public['policy_sha256'], 'implementation_sha256': public['implementation_sha256'],
        'inputs_sha256': base.digest(directory/'blind-inputs.json'),
        'order_invariance': [r['id'] for r in base.rank(rows)] == [r['id'] for r in base.rank(rows[::-1])],
        'runtime': {'wall_seconds': time.monotonic()-start, 'process_cpu_seconds': time.process_time()-cpu_start,
                    'gpu_used': False, 'audio_models_run': 0, 'audio_uploads': 0,
                    'device_or_daw_connections': 0, 'cpu_worker_limit': 1}, 'private_key_read': False}
    base.save(output/'blind-results.json', result)
    return {k: v for k, v in result.items() if k not in ('rows', 'references')}


def summarize(directory, run_directory):
    directory = Path(directory);run_directory = Path(run_directory)
    frozen = base.load(directory/'freeze.json');result = base.load(run_directory/'blind-results.json')
    for key, name in (('inputs_sha256', 'blind-inputs.json'), ('policy_sha256', 'policy.json'), ('key_sha256', 'private-key.json')):
        if base.digest(directory/name) != frozen[key] or key in result and result[key] != frozen[key]:
            raise ValueError('Frozen inputs, key or policy changed')
    if result['implementation_sha256'] != frozen['implementation_sha256']:
        raise ValueError('Run used a different evaluator implementation')
    key = base.load(directory/'private-key.json');public = base.load(directory/'blind-inputs.json')
    entries = {e['id']: e for e in public['items']};checks, sessions, audio = [], [], {}
    for sid in base.SESSIONS:
        rows = [r | key['items'][r['id']] for r in result['rows'] if r['session'] == sid]
        by_name = {r['variant']: r for r in rows};anchor = by_name['room']
        checks.append({'session': sid, 'test': 'anchor_accepted', 'passed': anchor['evaluation']['source_balance']['eligible']})
        counts = {'new_faults': 0, 'new_faults_caught': 0, 'old_faults_rejected': 0,
                  'benign_controls': 0, 'false_balance_alarms': 0, 'indeterminate_controls': 0}
        for row in rows:
            entry = entries[row['id']]
            if row['sha256'] != entry['sha256']:
                raise ValueError('Run and prepared audio differ')
            base.checked_asset(directory, entry)
            audio[f'fmb_{row["id"]}'] = entry['file']
            if row['candidate']:
                continue
            balance = row['evaluation']['source_balance'];expectation = row['expectation']
            delta = row['evaluation']['cost']-anchor['evaluation']['cost']
            if expectation == 'fault':
                counts['new_faults'] += 1;counts['new_faults_caught'] += bool(balance['violations'])
                counts['old_faults_rejected'] += (not row['legacy_evaluation']['eligible'] or delta >= base.POLICY['improvement_margin'])
                passed = bool(balance['violations'])
            else:
                counts['benign_controls'] += 1;counts['false_balance_alarms'] += bool(balance['violations'])
                passed = balance['eligible']
                if expectation == 'null':
                    passed = passed and abs(delta) <= POLICY['tie_tolerance']
            counts['indeterminate_controls'] += bool(balance['indeterminate_rules'])
            checks.append({'session': sid, 'test': row['variant'], 'passed': passed, 'delta': delta,
                           'expectation': expectation, 'violations': balance['violations']})
        ordered = base.rank([r for r in rows if r['candidate']])
        single = {}
        for ref in ('bethel', 'maverick'):
            rescored = [r | {'evaluation': source_balance.veto(base.score(r['metrics'], r['legacy_evaluation']['projection'],
                {ref: result['references'][ref]}, base.POLICY), r['evaluation']['source_balance'])} for r in ordered]
            single[ref] = base.rank(rescored)[0]['variant']
        # Reference disagreement is disclosed separately from the balance gate.
        sessions.append({'id': sid, 'source': next(s for s in key['sources']['sessions'] if s['id'] == sid),
            'ranked_candidates': ordered, 'controls': [r for r in rows if not r['candidate']],
            'best_candidate': ordered[0]['variant'], 'automatic_recommendation': 'withheld',
            'improvement_over_current': by_name['current']['evaluation']['cost']-ordered[0]['evaluation']['cost'],
            'single_reference_winners': single, 'counts': counts, 'plan': key['plans'][sid]})
    checks.extend({'session': r['session'], 'test': 'repeat', 'passed': r['exact_repeat']} for r in result['repeats'])
    checks.append({'session': 'all', 'test': 'order_invariance', 'passed': result['order_invariance']})
    for name, entry in public['references'].items():
        audio[f'fmb_{name}'] = entry['file']
    passed = all(c['passed'] for c in checks)
    report = {'kind': 'wcfi_source_balance_benchmark', 'title': 'Source balance · fresh controls across three sessions',
        'context': {'start_seconds': 0}, 'duration_seconds': 12, 'sessions': sessions, 'checks': checks,
        'control_gate_passed': passed, 'balance_gate_passed': passed, 'professional_quality_judgment_validated': False,
        'full_mix_quality_gate_revalidated': False, 'processing_applied': False, 'audio_models_run': 0,
        'audio': audio, 'runtime': result['runtime'], 'freeze': frozen, 'policy': POLICY,
        'references': key['sources']['references'], 'reference_metrics': result['references'],
        'limits': ['This gate tests role-based source-balance faults, not professional mixing quality. Automatic mix recommendations remain withheld.',
            'Nine source-balance rules use 250 ms stereo fits in three frequency bands. Persistent violations veto selection regardless of reference similarity.',
            'Raw synchronized inputs and declared track intent are required. No candidate fader, processing recipe, label or answer key is available to the evaluator.',
            'Attribution confidence screens correlated bleed. Uncertain fits abstain; uncertainty does not count as successful fault detection.',
            'Three dates are existing development recordings. These are new perturbations, not unseen musicians, sessions or independently labeled preferences.',
            'Eight original candidates and both original references retain the same processing, scalar rematched to -30 LUFS for extreme-control headroom. Sixteen new controls per date include nulls, benign changes and eleven deliberate balance faults.',
            'The legacy evaluator is scored on the exact same files for comparison. Its style weights, hum rule and technical gates are unchanged.',
            'The previous full-mix benchmark and its failures remain intact; no claim is made that its hum/overall-quality gate now passes.',
            'Filtered synthetic ambience is preserved across source-level changes; candidate sends are not remixed. Clipped raw recordings remain clipped.',
            'No host apply, playback, desktop interaction, device connection, GPU or upload occurs.']}
    base.save(run_directory/'results.json', report);base.write_html(report, run_directory/'report.html')
    return report


def export_package(directory, run_directory, output):
    directory = Path(directory);output = Path(output);r = summarize(directory, run_directory)
    if output.exists():
        raise ValueError('Use a new app package directory')
    output.mkdir(parents=True);files = {}
    for filename in r['audio'].values():
        shutil.copyfile(directory/'assets'/filename, output/filename);files[filename] = base.digest(output/filename)
    package = {'kind': 'wcfi_source_balance_benchmark_package', 'schema_version': 1, 'reports': {'MIX': r}, 'files': files}
    base.save(output/'study.json', package)
    return package


def validate_package(folder, package, raw):
    folder = Path(folder).resolve();r = package['reports']['MIX']
    if (package.get('kind') != 'wcfi_source_balance_benchmark_package' or package.get('schema_version') != 1
            or set(package['reports']) != {'MIX'} or r.get('kind') != 'wcfi_source_balance_benchmark'
            or r.get('policy') != POLICY or r.get('duration_seconds') != 12
            or r.get('processing_applied') is not False or r.get('professional_quality_judgment_validated') is not False
            or r.get('full_mix_quality_gate_revalidated') is not False or r.get('audio_models_run') != 0
            or r.get('runtime', {}).get('gpu_used') is not False or r.get('runtime', {}).get('audio_uploads') != 0
            or [s['id'] for s in r['sessions']] != list(base.SESSIONS) or len(r['checks']) != 55
            or r.get('control_gate_passed') is not all(c['passed'] for c in r['checks'])
            or r.get('balance_gate_passed') is not r['control_gate_passed']
            or len(r['audio']) != 74 or set(package['files']) != set(r['audio'].values())):
        raise ValueError('Invalid source-balance benchmark scope, gate or assets')
    for name, filename in r['audio'].items():
        path = (folder/filename).resolve()
        if (not base.re.fullmatch(r'fmb_([0-9a-f]{24}|bethel|maverick)', name) or filename != f'MIX-{name}.wav'
                or path.parent != folder or base.digest(path) != package['files'][filename]):
            raise ValueError('Source-balance asset changed or escaped its root')
        a = base.read_audio(path)
        if a.shape != (12*base.RATE, 2) or np.max(np.abs(a)) >= .999:
            raise ValueError('Invalid source-balance audio format or headroom')
    refs = {name: base.observations(base.read_audio(folder/f'MIX-fmb_{name}.wav')) for name in ('bethel', 'maverick')}
    if refs != r['reference_metrics']:
        raise ValueError('Source-balance reference observations changed')
    for session in r['sessions']:
        if (len(session['ranked_candidates']) != 8 or len(session['controls']) != 16
                or session['automatic_recommendation'] != 'withheld'
                or {v['variant'] for v in session['ranked_candidates']} != set(base.CANDIDATES)
                or {v['variant'] for v in session['controls']} != set(CONTROLS)
                or base.rank(session['ranked_candidates']) != session['ranked_candidates']):
            raise ValueError('Invalid balance recommendation or candidate sets')
        for row in session['ranked_candidates']+session['controls']:
            a = base.read_audio(folder/f'MIX-fmb_{row["id"]}.wav');m = base.observations(a)
            old = base.score(m, row['legacy_evaluation']['projection'], refs, base.POLICY)
            if (m != row['metrics'] or old != row['legacy_evaluation']
                    or source_balance.veto(old, row['evaluation']['source_balance']) != row['evaluation']):
                raise ValueError('Balance ranking does not match frozen rules or saved audio')
    return package, hashlib.sha256(raw).hexdigest()
