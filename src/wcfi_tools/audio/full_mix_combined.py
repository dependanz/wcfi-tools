"""Combined full-mix controls, balance veto, new mains check and selection audit."""
from __future__ import annotations

import hashlib
import inspect
import random
import shutil
import time
from pathlib import Path

import numpy as np

from . import balance_benchmark as balance_suite
from . import full_mix_benchmark as base
from . import mains_hum, source_balance

HUM_CONTROLS = {
    'hum50_quiet': ('50.12 Hz comb · −14 dB relative RMS', 'hum_fault'),
    'hum60_quiet': ('59.82 Hz comb · −14 dB relative RMS', 'hum_fault'),
    'hum50_strong': ('50 Hz comb · −8 dB relative RMS', 'hum_fault'),
    'hum60_drift': ('60 Hz slowly drifting comb · −10 dB relative RMS', 'hum_fault'),
    'hum60_episode': ('60 Hz comb for final six seconds', 'hum_fault'),
    'bass_near50': ('49 Hz musical bass note in raw input and mix', 'benign'),
    'bass_near60': ('61.735 Hz musical bass note in raw input and mix', 'benign'),
    'bass_source60': ('60 Hz comb explained by the raw BASS input', 'benign'),
}
POLICY = {'version': 3, 'style': base.POLICY, 'balance': source_balance.POLICY, 'hum': mains_hum.POLICY,
          'audition_target_lufs': -30., 'tie_tolerance': .005, 'improvement_margin': .05,
          'selection_minimum_gap': .025, 'selection_half_improvement_margin': .025,
          'selection_scope': 'Experimental engineering choice only; not validated musical preference.',
          'gate': 'All original controls, fresh balance controls, new hum/benign controls, anchors, repeats, order and reference agreement must pass.',
          'professional_quality_judgment_validated': False, 'automatic_host_apply': False}


def implementation_digest():
    modules = (base, balance_suite, source_balance, mains_hum)
    return hashlib.sha256(b'\n'.join([Path(m.__file__).read_bytes() for m in modules]
        +[inspect.getsource(new_hum_controls).encode(), inspect.getsource(evaluate_audio).encode()])).hexdigest()


def new_hum_controls(anchor, raw, bass_gain_db, anchor_gain=1.):
    n = len(anchor);t = np.arange(n)/base.RATE
    def tone(hz, level_db, *, drifting=False, episode=False):
        phase = 2*np.pi*hz*t
        if drifting:
            phase += .18*8*np.sin(2*np.pi*t/8)
        wave = np.column_stack([sum(np.sin(k*phase+c*.1)/k for k in (1, 2, 3)) for c in (0, 1)])
        wave *= (base.power(anchor)/max(base.power(wave), 1e-24))**.5*10**(level_db/20)
        if episode:
            envelope = np.zeros(n);envelope[6*base.RATE:] = 1
            envelope[6*base.RATE:6*base.RATE+960] = np.linspace(0, 1, 960)
            wave *= envelope[:, None]
        return wave
    arrays = {
        'hum50_quiet': anchor+tone(50.12, -14), 'hum60_quiet': anchor+tone(59.82, -14),
        'hum50_strong': anchor+tone(50, -8), 'hum60_drift': anchor+tone(60, -10, drifting=True),
        'hum60_episode': anchor+tone(60, -10, episode=True),
    }
    overrides = {}
    for name, hz in (('bass_near50', 49.), ('bass_near60', 61.735), ('bass_source60', 60.)):
        wave = tone(hz, -18)
        arrays[name] = anchor+wave
        # BASS has no EQ/dynamics in the retained renderer. The explicit alternate
        # raw context, not a label, lets inference explain its musical contribution.
        overrides[name] = {'BASS': raw['BASS']+wave/(10**(bass_gain_db/20)*anchor_gain)}
    return arrays, overrides


def _verified_inputs(root):
    root = Path(root).resolve();public = base.load(root/'blind-inputs.json');key = base.load(root/'private-key.json')
    frozen = base.load(root/'freeze.json')
    if base.digest(root/'blind-inputs.json') != frozen['inputs_sha256'] or base.digest(root/'private-key.json') != frozen['key_sha256']:
        raise ValueError('A parent frozen benchmark changed')
    return public, key


def prepare(original, balance_directory, output, *, ffmpeg, seed=2026102203, progress=None):
    original = Path(original).resolve();balance_directory = Path(balance_directory).resolve();output = Path(output)
    if output.exists():
        raise ValueError('Use a new immutable combined benchmark directory')
    old, old_key = _verified_inputs(original);balanced, balanced_key = _verified_inputs(balance_directory)
    output.mkdir(parents=True);assets = output/'assets';assets.mkdir();base.save(output/'policy.json', POLICY)
    public = {'kind': 'wcfi_combined_blind_inputs', 'sessions': [], 'items': [], 'references': {},
              'policy_sha256': base.digest(output/'policy.json'), 'implementation_sha256': implementation_digest()}
    key = {'items': {}, 'plans': balanced_key['plans'], 'sources': old_key['sources']};rng = random.Random(seed)
    for session in old['sessions']:
        sid = session['id'];raw = {name: base.checked_asset(original, e) for name, e in session['sources'].items()}
        for entry in session['sources'].values():
            shutil.copyfile(original/'assets'/entry['file'], assets/entry['file'])
        public['sessions'].append(session)
        for root, parent, private, mode in ((balance_directory, balanced, balanced_key, 'balance'),
                                           (original, old, old_key, 'original')):
            for entry in parent['items']:
                label = private['items'][entry['id']]
                if entry['session'] != sid or mode == 'original' and label['candidate']:
                    continue
                name = label['variant'];candidate = label['candidate']
                identifier = f'{rng.getrandbits(96):024x}';filename = f'MIX-fmb_{identifier}.wav'
                a = base.checked_asset(root, entry)
                matching = balance_suite.match_audio(a, assets/filename, ffmpeg)
                public['items'].append({'id': identifier, 'session': sid, 'file': filename,
                    'sha256': base.digest(assets/filename), 'matching': matching})
                expectation = label.get('expectation', 'null' if name in ('identical', 'gain_only') else 'fault')
                if mode == 'original' and name in ('buried_lead', 'buried_kit', 'dominant_acoustic'):
                    expectation = 'balance_fault'
                elif mode == 'original' and name == 'hum':
                    expectation = 'hum_fault'
                elif mode == 'balance' and expectation == 'fault':
                    expectation = 'balance_fault'
                variant = name if candidate else f'{mode}_{name}'
                key['items'][identifier] = {'variant': variant, 'label': label['label'], 'candidate': candidate,
                                            'expectation': expectation, 'group': mode}
        room = next(e for e in balanced['items'] if e['session'] == sid and balanced_key['items'][e['id']]['variant'] == 'room')
        anchor = base.checked_asset(balance_directory, room)
        levels = {name: (base.scan_loudness(original/'assets'/entry['file'], ffmpeg)
                        if base.power(raw[name]) > 1e-10 else {'integrated_lufs': None})
                  for name, entry in session['sources'].items()}
        _, shaped, _ = base.source_versions(raw, levels)
        unmatched = sum(shaped.values())+base.ambience(shaped, .7, -16)
        anchor_gain = float(np.sum(unmatched*anchor)/np.sum(unmatched**2))
        residual = float(np.mean((unmatched*anchor_gain-anchor)**2)/max(base.power(anchor), 1e-30))
        if anchor_gain <= 0 or residual > 1e-10:
            raise ValueError('Source-aware tone context cannot verify the original whole-mix gain')
        arrays, overrides = new_hum_controls(anchor, raw, balanced_key['plans'][sid]['BASS']['gain_db'], anchor_gain)
        for name, a in arrays.items():
            identifier = f'{rng.getrandbits(96):024x}';filename = f'MIX-fmb_{identifier}.wav'
            matching = balance_suite.match_audio(a, assets/filename, ffmpeg)
            override_entries = {}
            for role, samples in overrides.get(name, {}).items():
                f = f'{sid}-{name}-context-{role}.wav';base.sf.write(assets/f, samples, base.RATE, subtype='FLOAT')
                override_entries[role] = {'file': f, 'sha256': base.digest(assets/f)}
            public['items'].append({'id': identifier, 'session': sid, 'file': filename,
                'sha256': base.digest(assets/filename), 'matching': matching, 'source_overrides': override_entries})
            key['items'][identifier] = {'variant': name, 'label': HUM_CONTROLS[name][0],
                'expectation': HUM_CONTROLS[name][1], 'candidate': False, 'group': 'fresh_hum'}
        if progress:
            progress(f'Prepared {sid}: 8 retained candidates, 34 combined controls')
    for name, entry in balanced['references'].items():
        a = base.checked_asset(balance_directory, entry);f = entry['file']
        matching = balance_suite.match_audio(a, assets/f, ffmpeg)
        public['references'][name] = {'file': f, 'sha256': base.digest(assets/f), 'matching': matching}
    rng.shuffle(public['items']);base.save(output/'blind-inputs.json', public);base.save(output/'private-key.json', key)
    frozen = {'inputs_sha256': base.digest(output/'blind-inputs.json'), 'key_sha256': base.digest(output/'private-key.json'),
              'policy_sha256': public['policy_sha256'], 'implementation_sha256': public['implementation_sha256'],
              'controls_scored_before_freeze': False}
    base.save(output/'freeze.json', frozen)
    return {'mix_versions': 126, 'reference_files': 2, 'new_hum_controls': 24, **frozen}


def analyzers(raw):
    return base.SourceProjection(raw), source_balance.BalanceAnalyzer(raw), mains_hum.HumAnalyzer(raw)


def evaluate_audio(audio, context, references):
    old, balance, hum = context;m = base.observations(audio)
    legacy = base.score(m, old.evaluate(audio), references, base.POLICY)
    b = balance.evaluate(audio);h = hum.evaluate(audio)
    combined = mains_hum.veto(source_balance.veto(legacy, b), h)
    return {'metrics': m, 'legacy_evaluation': legacy, 'evaluation': combined}


def run(directory, output, *, progress=None):
    """Score opaque files with audio context; no answer-key or renderer metadata."""
    directory = Path(directory);output = Path(output);start = time.monotonic();cpu = time.process_time()
    if output.exists():
        raise ValueError('Use a new combined inference run directory')
    public = base.load(directory/'blind-inputs.json')
    if (base.load(directory/'policy.json') != POLICY or base.digest(directory/'policy.json') != public['policy_sha256']
            or implementation_digest() != public['implementation_sha256']):
        raise ValueError('Frozen combined policy or implementation changed')
    output.mkdir(parents=True);references = {k: base.observations(base.checked_asset(directory, e)) for k, e in public['references'].items()}
    rows, repeats = [], []
    for session in public['sessions']:
        raw = {k: base.checked_asset(directory, e) for k, e in session['sources'].items()};context = analyzers(raw)
        for index, entry in enumerate(e for e in public['items'] if e['session'] == session['id']):
            a = base.checked_asset(directory, entry);selected_context = context
            if entry.get('source_overrides'):
                changed = raw | {k: base.checked_asset(directory, e) for k, e in entry['source_overrides'].items()}
                selected_context = analyzers(changed)
            value = evaluate_audio(a, selected_context, references)
            rows.append({'id': entry['id'], 'session': session['id'], 'sha256': entry['sha256'], **value})
            if index == 0:
                repeats.append({'session': session['id'], 'exact_repeat': evaluate_audio(a, selected_context, references) == value})
        if progress:
            progress(f'Scored {session["id"]}: 42 opaque mixes, combined balance/hum checks')
        base.save(output/'progress.json', {'completed_sessions': len(repeats), 'items': len(rows)})
    result = {'kind': 'wcfi_combined_blind_run', 'rows': rows, 'references': references, 'repeats': repeats,
        'order_invariance': [r['id'] for r in base.rank(rows)] == [r['id'] for r in base.rank(rows[::-1])],
        'inputs_sha256': base.digest(directory/'blind-inputs.json'), 'policy_sha256': public['policy_sha256'],
        'implementation_sha256': public['implementation_sha256'], 'private_key_read': False,
        'runtime': {'wall_seconds': time.monotonic()-start, 'process_cpu_seconds': time.process_time()-cpu,
            'gpu_used': False, 'audio_models_run': 0, 'audio_uploads': 0, 'device_or_daw_connections': 0, 'cpu_worker_limit': 1}}
    base.save(output/'blind-results.json', result)
    return {k: v for k, v in result.items() if k not in ('rows', 'references')}


def selection_audit(directory, public, session, winner, baseline, references):
    """Predeclared post-selection check, using each six-second half separately."""
    raw = {k: base.checked_asset(directory, e) for k, e in session['sources'].items()}
    files = {e['id']: e for e in public['items']}
    chosen = base.checked_asset(directory, files[winner['id']]);current = base.checked_asset(directory, files[baseline['id']])
    halves = []
    for index in (0, 1):
        region = slice(index*6*base.RATE, (index+1)*6*base.RATE)
        context = analyzers({k: a[region] for k, a in raw.items()})
        a = evaluate_audio(chosen[region], context, references);b = evaluate_audio(current[region], context, references)
        delta = b['evaluation']['cost']-a['evaluation']['cost']
        halves.append({'half': index+1, 'candidate_eligible': a['evaluation']['eligible'],
                       'candidate_failures': a['evaluation']['guardrail_failures'], 'cost_improvement': delta,
                       'passed': a['evaluation']['eligible'] and delta >= POLICY['selection_half_improvement_margin']})
    return halves


def selection_checks(winner, stable, improvement, gap, halves):
    return (winner['evaluation']['eligible'] and stable and improvement >= POLICY['improvement_margin']
            and gap >= POLICY['selection_minimum_gap'] and all(h['passed'] for h in halves)
            and len(halves) == 2)


def summarize(directory, run_directory):
    directory = Path(directory);run_directory = Path(run_directory);frozen = base.load(directory/'freeze.json')
    result = base.load(run_directory/'blind-results.json');public = base.load(directory/'blind-inputs.json')
    for k, filename in (('inputs_sha256', 'blind-inputs.json'), ('policy_sha256', 'policy.json'), ('key_sha256', 'private-key.json')):
        if frozen[k] != base.digest(directory/filename) or k in result and result[k] != frozen[k]:
            raise ValueError('Frozen combined assets or run changed')
    if result['implementation_sha256'] != frozen['implementation_sha256']:
        raise ValueError('Run used a different combined evaluator')
    private = base.load(directory/'private-key.json');entries = {e['id']: e for e in public['items']}
    checks, sessions, audio = [], [], {}
    audit_start = time.monotonic()
    for sid in base.SESSIONS:
        rows = [r | private['items'][r['id']] for r in result['rows'] if r['session'] == sid]
        by_name = {r['variant']: r for r in rows};anchor = by_name['room']
        checks.append({'session': sid, 'test': 'clean_anchor_accepted', 'passed': anchor['evaluation']['eligible']})
        counts = {'faults': 0, 'faults_rejected': 0, 'hum_faults': 0, 'hum_detected': 0,
                  'benign_null': 0, 'false_alarms': 0}
        for row in rows:
            entry = entries[row['id']];base.checked_asset(directory, entry)
            if row['sha256'] != entry['sha256']:
                raise ValueError('Combined run does not match audio')
            audio[f'fmb_{row["id"]}'] = entry['file']
            if row['candidate']:
                continue
            ev = row['evaluation'];expectation = row['expectation'];delta = ev['cost']-anchor['evaluation']['cost']
            if expectation == 'balance_fault':
                passed = bool(ev['source_balance']['violations'])
            elif expectation == 'hum_fault':
                passed = ev['mains_hum']['violation'];counts['hum_faults'] += 1;counts['hum_detected'] += passed
            elif expectation in ('benign', 'null'):
                passed = ev['eligible']
                if expectation == 'null':
                    passed = passed and abs(delta) <= POLICY['tie_tolerance']
                counts['benign_null'] += 1;counts['false_alarms'] += not passed
            else:
                passed = anchor['evaluation']['eligible'] and (not ev['eligible'] or delta >= POLICY['improvement_margin'])
            if expectation not in ('benign', 'null'):
                counts['faults'] += 1;counts['faults_rejected'] += passed
            checks.append({'session': sid, 'test': row['variant'], 'expectation': expectation, 'passed': passed, 'delta': delta})
        ordered = base.rank([r for r in rows if r['candidate']]);winner = ordered[0]
        single = {}
        for ref in ('bethel', 'maverick'):
            rescored = []
            for r in ordered:
                old = base.score(r['metrics'], r['legacy_evaluation']['projection'], {ref: result['references'][ref]}, base.POLICY)
                ev = mains_hum.veto(source_balance.veto(old, r['evaluation']['source_balance']), r['evaluation']['mains_hum'])
                rescored.append(r | {'evaluation': ev})
            single[ref] = base.rank(rescored)[0]['variant']
        stable = all(v == winner['variant'] for v in single.values())
        checks.append({'session': sid, 'test': 'reference_choice_agreement', 'passed': stable})
        source = next(s for s in public['sessions'] if s['id'] == sid)
        halves = selection_audit(directory, public, source, winner, by_name['current'], result['references'])
        gap = ordered[1]['evaluation']['cost']-winner['evaluation']['cost']
        improvement = by_name['current']['evaluation']['cost']-winner['evaluation']['cost']
        eligible = [v for v in ordered if v['evaluation']['eligible']]
        if len(eligible) > 1:
            gap = eligible[1]['evaluation']['cost']-eligible[0]['evaluation']['cost']
        sessions.append({'id': sid, 'source': next(s for s in private['sources']['sessions'] if s['id'] == sid),
            'ranked_candidates': ordered, 'controls': [r for r in rows if not r['candidate']], 'counts': counts,
            'best_candidate': winner['variant'], 'single_reference_winners': single,
            'improvement_over_current': improvement, 'plan': private['plans'][sid],
            'selection_audit': {'halves': halves, 'gap_to_next_eligible': gap, 'reference_agreement': stable,
                'selection_checks_passed': selection_checks(winner, stable, improvement, gap, halves)}})
    checks.extend({'session': r['session'], 'test': 'repeat', 'passed': r['exact_repeat']} for r in result['repeats'])
    checks.append({'session': 'all', 'test': 'order_invariance', 'passed': result['order_invariance']})
    gate = all(c['passed'] for c in checks)
    for s in sessions:
        s['automatic_recommendation'] = 'experimental_engineering_candidate' if gate and s['selection_audit']['selection_checks_passed'] else 'withheld'
    for name, e in public['references'].items():
        audio[f'fmb_{name}'] = e['file']
    r = {'kind': 'wcfi_full_mix_combined_benchmark', 'title': 'Full-mix benchmark · balance + hum + selection',
        'context': {'start_seconds': 0}, 'duration_seconds': 12, 'sessions': sessions, 'checks': checks,
        'control_gate_passed': gate, 'professional_quality_judgment_validated': False, 'processing_applied': False,
        'audio_models_run': 0, 'runtime': result['runtime'] | {'post_selection_audit_seconds': time.monotonic()-audit_start},
        'policy': POLICY, 'freeze': frozen, 'audio': audio, 'references': private['sources']['references'],
        'reference_metrics': result['references'],
        'limits': ['Combined control success establishes engineered fault rejection, not validated professional musical judgment.',
            'Automatic choices are explicitly experimental. A passing full control gate, eligible candidate, reference agreement, cost/gap margins and separately checked six-second halves are required.',
            'All three dates and references are development recordings. The new hum transformations are fresh; no unseen-session or expert preference validation is available.',
            'Hum checks persistent 50/60 Hz residual combs after cross-fitting known raw sources. Source-explained tones can be musical or upstream hum; this layer cannot distinguish identical waveforms by intent.',
            'Fresh negative controls explicitly include the added bass note in both raw source context and the mix. Context is supplied as audio, not as a good/bad label.',
            'Original full-mix controls, all source-balance controls and eight new hum/negative controls per date are retained; failed tests are never hidden.',
            'Eight candidates retain their existing processing. All complete files are scalar matched to -30 LUFS; no limiter repairs extreme controls.',
            'Saved Master baseline is an earlier chain, not the historical broadcast or latest native Ableton state. Local processing/ambience is not claimed to emulate native plugins.',
            'Policy, core evaluator/renderer implementation and opaque inputs were frozen before inference; scoring does not read the answer key. Post-selection temporal verification is a separate predeclared stage.',
            'No host processing applied, Play/Record, real playback, desktop interaction, GPU, model call, new download or cloud upload.']}
    base.save(run_directory/'results.json', r);base.write_html(r, run_directory/'report.html')
    return r


def export_package(directory, run_directory, output):
    directory = Path(directory);output = Path(output);r = summarize(directory, run_directory)
    if output.exists():
        raise ValueError('Use a new combined app package directory')
    output.mkdir(parents=True);files = {}
    for filename in r['audio'].values():
        shutil.copyfile(directory/'assets'/filename, output/filename);files[filename] = base.digest(output/filename)
    p = {'kind': 'wcfi_full_mix_combined_package', 'schema_version': 1, 'reports': {'MIX': r}, 'files': files}
    base.save(output/'study.json', p)
    return p


def validate_package(folder, package, raw):
    folder = Path(folder).resolve();r = package['reports']['MIX']
    if (package.get('kind') != 'wcfi_full_mix_combined_package' or package.get('schema_version') != 1
            or set(package['reports']) != {'MIX'} or r.get('kind') != 'wcfi_full_mix_combined_benchmark'
            or r.get('policy') != POLICY or r.get('professional_quality_judgment_validated') is not False
            or r.get('processing_applied') is not False or r.get('audio_models_run') != 0
            or r.get('runtime', {}).get('gpu_used') is not False or r.get('runtime', {}).get('audio_uploads') != 0
            or len(r['checks']) != 112 or r.get('control_gate_passed') is not all(c['passed'] for c in r['checks'])
            or [s['id'] for s in r['sessions']] != list(base.SESSIONS) or len(r['audio']) != 128
            or set(package['files']) != set(r['audio'].values())):
        raise ValueError('Invalid combined benchmark scope, gate or assets')
    for key, filename in r['audio'].items():
        path = (folder/filename).resolve()
        if (not base.re.fullmatch(r'fmb_([0-9a-f]{24}|bethel|maverick)', key) or filename != f'MIX-{key}.wav'
                or path.parent != folder or base.digest(path) != package['files'][filename]):
            raise ValueError('Combined benchmark asset changed or escaped root')
        a = base.read_audio(path)
        if a.shape != (12*base.RATE, 2) or np.max(np.abs(a)) >= .999:
            raise ValueError('Invalid combined benchmark audio')
    refs = {k: base.observations(base.read_audio(folder/f'MIX-fmb_{k}.wav')) for k in ('bethel', 'maverick')}
    if refs != r['reference_metrics']:
        raise ValueError('Combined reference observations changed')
    for s in r['sessions']:
        audit = s['selection_audit']
        if ([h['half'] for h in audit['halves']] != [1, 2]
                or any(h['passed'] is not (h['candidate_eligible'] and h['cost_improvement'] >= POLICY['selection_half_improvement_margin'])
                       for h in audit['halves'])):
            raise ValueError('Temporal selection evidence is inconsistent')
        verified = selection_checks(s['ranked_candidates'][0], audit['reference_agreement'],
                                    s['improvement_over_current'], audit['gap_to_next_eligible'], audit['halves'])
        expected = 'experimental_engineering_candidate' if r['control_gate_passed'] and s['selection_audit']['selection_checks_passed'] else 'withheld'
        if (len(s['ranked_candidates']) != 8 or len(s['controls']) != 34 or s['automatic_recommendation'] != expected
                or audit['selection_checks_passed'] is not verified
                or {v['variant'] for v in s['ranked_candidates']} != set(base.CANDIDATES)
                or base.rank(s['ranked_candidates']) != s['ranked_candidates']):
            raise ValueError('Invalid combined ranking or recommendation')
        for row in s['ranked_candidates']+s['controls']:
            a = base.read_audio(folder/f'MIX-fmb_{row["id"]}.wav');m = base.observations(a)
            old = base.score(m, row['legacy_evaluation']['projection'], refs, base.POLICY)
            combined = mains_hum.veto(source_balance.veto(old, row['evaluation']['source_balance']), row['evaluation']['mains_hum'])
            if m != row['metrics'] or old != row['legacy_evaluation'] or combined != row['evaluation']:
                raise ValueError('Combined scores do not match saved audio and frozen rules')
    return package, hashlib.sha256(raw).hexdigest()
