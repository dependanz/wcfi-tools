"""Experimental grouped CPU training, with a separate ODAQ fidelity model.

Every reported prediction is out of song/reference; no random clip/window split.
No DAW, device, subprocess, playback, upload or pretrained network is used.
"""
from __future__ import annotations

import hashlib
import html
import itertools
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from .preference_features import ARTIFACT_NAMES, FEATURE_NAMES
from .profiles import file_hash, write_json

POLICY = {
    "kind": "wcfi_targeted_mix_preference", "version": 1, "seed": 20261022,
    "preference_features": list(FEATURE_NAMES), "artifact_features": list(ARTIFACT_NAMES),
    "audio": "Complete saved excerpts, stereo preserved, scipy polyphase 24 kHz; no absolute level preference feature",
    "MED_target": "Paired listener fraction preferring A, ties half; exclude either mix author; >=8 shared raters",
    "model": "Linear pairwise soft-label logistic ranker; no intercept; equal total pair weight per training content",
    "outer_test": "Leave one entire MED song / ODAQ reference content out",
    "inner_selection": "Leave one training content out, minimum macro soft-label log loss; ties prefer stronger regularization",
    "regularization_grid": [1., .1, .01, .001],
    "scaler": "Training clips only, std floor 1e-6 replaced with 1; z clip +/-8; no global normalization or feature selection",
    "artifact_model": "Separate ODAQ full-reference nonnegative-error ranker; quality = negative weighted errors; requires aligned clean reference; not a creative-change veto",
    "human_decisive": "95% paired-listener bootstrap mean difference excludes zero; 10000 draws; no multiple-comparison correction",
    "evaluation": "Equal weight per held-out song/reference, not pairs/windows; 95% content bootstrap 10000 draws",
    "gates": {"minimum_groups": 6, "minimum_decisive_pairs": 12,
              "MED_accuracy": .75, "ODAQ_accuracy": .80, "ODAQ_processed_accuracy": .70,
              "accuracy_lower_ci_above": .5, "MED_gain_over_rms_lower_ci_above": 0},
    "gate_status": "Prospective engineering screen, not an industry or perceptual certification",
    "previous_exposure": "These songs were previously inspected for SongBench. Held out from fitting, not a pristine never-seen research test set.",
    "deployment": "Experimental JSON weights only; no automatic Ableton changes, no professional-quality claim",
    "budget": {"logical_cpus": 1, "threads": 1, "priority": "idle", "gpu": False,
               "maximum_excerpt_seconds": 120, "wall_deadline_seconds": 1800,
               "maximum_working_set_mib": 2048},
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def implementation_hashes():
    root = Path(__file__).parent
    return {n: file_hash(root/n) for n in ("mix_preference.py", "preference_features.py")}


def resource_checks():
    """Measure this worker's limits; never change another app or system setting."""
    from .master_benchmark_cpu import _memory

    gpu_modules = sorted(set(sys.modules) & {'torch', 'tensorflow', 'cupy', 'jax'})
    if gpu_modules:
        raise RuntimeError('Unexpected GPU-capable runtime imported: '+','.join(gpu_modules))
    threads = {name: os.environ.get(name) for name in
               ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')}
    if set(threads.values()) != {'1'}:
        raise RuntimeError('Numerical thread budget was changed')
    if os.name == 'nt':
        import ctypes as ct

        kernel = ct.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ct.c_void_p
        kernel.GetProcessAffinityMask.argtypes = (ct.c_void_p, ct.POINTER(ct.c_size_t), ct.POINTER(ct.c_size_t))
        kernel.GetPriorityClass.argtypes = (ct.c_void_p,)
        handle = kernel.GetCurrentProcess()
        process_mask, system_mask = ct.c_size_t(), ct.c_size_t()
        if not kernel.GetProcessAffinityMask(handle, ct.byref(process_mask), ct.byref(system_mask)):
            raise RuntimeError('Cannot verify CPU affinity')
        cpus = [i for i in range(64) if process_mask.value & (1 << i)]
        idle = kernel.GetPriorityClass(handle) == 0x40
    else:
        cpus = sorted(os.sched_getaffinity(0))
        idle = os.getpriority(os.PRIO_PROCESS, 0) >= 19
    if len(cpus) != 1 or not idle:
        raise RuntimeError('Worker left its one-CPU idle-priority budget')
    memory = _memory()
    if memory.get('working_set_mib', 0) > POLICY['budget']['maximum_working_set_mib']:
        raise RuntimeError('Worker exceeded its memory budget')
    return {'cpu_affinity': cpus, 'idle_priority_verified': idle,
            'thread_environment': threads, 'GPU_runtime_modules': gpu_modules, **memory}


def prepare(assets_root, expanded_root, output):
    """Freeze source hashes, ratings, grouped folds and code before extraction/training."""
    root, expanded, output = [Path(p).resolve() for p in (assets_root, expanded_root, output)]
    if output.exists():
        raise ValueError("Choose a new protocol directory")
    from .songbench_external import _med_ratings

    metadata = read_json(root/'assets/med-data.json')
    selection = read_json(expanded/'selection.json')
    if file_hash(root/'assets/med-data.json') != selection['metadata_sha256']:
        raise ValueError("Changed MED metadata")
    songs = {s['song_id']: s for s in metadata['songs']}
    records = []
    for item in selection['clips']:
        song = songs[item['song']]
        if 'Creative Commons' not in str(song['license']):
            raise ValueError("Only licensed CC cohorts are eligible")
        mix = next(m for m in song['mixes'] if m['mix_code'] == item['mix'])
        name = item['song']+'__'+item['mix']+'.mp3'
        path = root/'med-audio'/name
        if not path.is_file():
            path = expanded/'med-audio'/name
        records.append({'dataset': 'MED', 'group': item['song'], 'name': item['mix'],
                        'ratings': _med_ratings(mix), 'source_file': str(path), 'license': song['license']})
    records.extend({k: row[k] for k in ('dataset', 'group', 'name', 'ratings', 'source_file', 'source_sha256', 'method')}
                   for row in read_json(root/'benchmark-01/answer-key.json')['rows'] if row['dataset'] == 'ODAQ')
    seen, reference_hashes = set(), set()
    for record in records:
        identifier = hashlib.sha256((record['dataset']+'/'+record['group']+'/'+record['name']).encode()).hexdigest()[:20]
        if identifier in seen:
            raise ValueError("Duplicate source identity")
        seen.add(identifier)
        digest = file_hash(Path(record['source_file']))
        if record.get('source_sha256', digest) != digest:
            raise ValueError('Changed original ODAQ source')
        record.update(id=identifier, source_sha256=digest)
        if record['dataset'] == 'ODAQ' and record['name'] == 'reference':
            if record['source_sha256'] in reference_hashes:
                raise ValueError("Repeated ODAQ reference must be grouped together")
            reference_hashes.add(record['source_sha256'])
    output.mkdir(parents=True)
    write_json(output/'sources.json', {'records': records, 'selection': selection})
    folds = {dataset: [{'test': group, 'train': sorted({r['group'] for r in records
             if r['dataset'] == dataset and r['group'] != group})}
             for group in sorted({r['group'] for r in records if r['dataset'] == dataset})]
             for dataset in ('MED', 'ODAQ')}
    write_json(output/'protocol.json', POLICY | {'implementation_sha256': implementation_hashes(),
               'sources_sha256': file_hash(output/'sources.json'), 'folds': folds,
               'MED_metadata_sha256': selection['metadata_sha256']})
    return {'protocol': str(output/'protocol.json'),
            'MED_mixes': sum(r['dataset'] == 'MED' for r in records),
            'MED_songs': len(folds['MED']), 'ODAQ_conditions': sum(r['dataset'] == 'ODAQ' for r in records),
            'ODAQ_references': len(folds['ODAQ'])}


def listener_differences(a, b):
    import numpy as np

    listeners = sorted(set(a['ratings']) & set(b['ratings']))
    if a['dataset'] == 'MED':
        owners = {a['name'], b['name']}
        listeners = [r for r in listeners if re.sub(r'_(?:\d{4}|unknown)$', '', r) not in owners]
    return np.array([a['ratings'][r]-b['ratings'][r] for r in listeners], dtype='float64')


def make_pairs(records):
    import numpy as np

    groups = defaultdict(list)
    for index, record in enumerate(records):
        groups[record['group']].append(index)
    pairs = []
    for group, indices in sorted(groups.items()):
        for a, b in itertools.combinations(indices, 2):
            difference = listener_differences(records[a], records[b])
            if len(difference) < 8:
                continue
            target = float(np.mean((difference > 0) + .5*(difference == 0)))
            pairs.append({'a': a, 'b': b, 'group': group, 'target': target,
                          'differences': difference.tolist()})
    return pairs


def fit_ranker(x, records, pairs, groups, regularization, *, artifact=False):
    """Fit preprocessing and coefficients using exactly the supplied content groups."""
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import expit

    groups = set(groups)
    indices = [i for i, r in enumerate(records) if r['group'] in groups]
    training = [p for p in pairs if p['group'] in groups]
    if not indices or not training:
        raise ValueError("No training clips or paired labels")
    mean = np.zeros(x.shape[1]) if artifact else np.mean(x[indices], axis=0)
    std = np.std(x[indices], axis=0)
    scale = np.where(std > 1e-6, std, 1.)
    z = np.clip((x-mean)/scale, -8, 8)
    if artifact:
        z = -z
    design = np.array([z[p['a']]-z[p['b']] for p in training])
    target = np.array([p['target'] for p in training])
    counts = {g: sum(p['group'] == g for p in training) for g in groups}
    weights = np.array([1/counts[p['group']] for p in training])
    weights /= weights.sum()

    def objective(beta):
        logits = design @ beta
        value = np.sum(weights*(np.logaddexp(0, logits)-target*logits)) + regularization*np.dot(beta, beta)/2
        gradient = design.T @ (weights*(expit(logits)-target)) + regularization*beta
        return value, gradient

    result = minimize(objective, np.zeros(x.shape[1]), jac=True, method='L-BFGS-B',
                      bounds=[(0, None)]*x.shape[1] if artifact else None,
                      options={'maxiter': 500, 'ftol': 1e-12, 'gtol': 1e-8})
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError('Ranker failed to converge: '+str(result.message))
    return {'mean': mean.tolist(), 'scale': scale.tolist(), 'coefficient': result.x.tolist(),
            'regularization': regularization, 'artifact': artifact,
            'training_groups': sorted(groups), 'training_ids': [records[i]['id'] for i in indices],
            'training_pairs': len(training), 'optimizer_iterations': int(result.nit)}


def predict(model, x):
    import numpy as np

    z = np.clip((np.asarray(x)-model['mean'])/model['scale'], -8, 8)
    if model['artifact']:
        z = -z
    return z @ np.asarray(model['coefficient'])


def pair_loss(model, x, pairs, test_group):
    import numpy as np

    scores = predict(model, x)
    losses = [float(np.logaddexp(0, scores[p['a']]-scores[p['b']])
                    - p['target']*(scores[p['a']]-scores[p['b']]))
              for p in pairs if p['group'] == test_group]
    if not losses:
        raise ValueError('Held-out group has no eligible pair labels')
    return float(np.mean(losses))


def select_regularization(x, records, pairs, groups, *, artifact=False):
    import numpy as np

    groups = sorted(groups)
    candidates = []
    for regularization in POLICY['regularization_grid']:
        losses = []
        for validation in groups:
            training = [g for g in groups if g != validation]
            model = fit_ranker(x, records, pairs, training, regularization, artifact=artifact)
            losses.append(pair_loss(model, x, pairs, validation))
        candidates.append({'regularization': regularization, 'macro_log_loss': float(np.mean(losses)),
                           'folds': [{'test': g, 'train': [s for s in groups if s != g], 'loss': loss}
                                     for g, loss in zip(groups, losses)]})
    # Grid order prefers stronger regularization for exact ties, no outer-test metrics used.
    selected = min(candidates, key=lambda item: item['macro_log_loss'])['regularization']
    return selected, candidates


def nested_predictions(x, records, pairs, *, artifact=False, progress=print):
    import numpy as np

    groups = sorted({r['group'] for r in records})
    if len(groups) < 4:
        raise ValueError('Nested content testing needs at least four groups')
    scores = np.full(len(records), np.nan)
    folds = []
    for test in groups:
        training = [g for g in groups if g != test]
        regularization, search = select_regularization(x, records, pairs, training, artifact=artifact)
        model = fit_ranker(x, records, pairs, training, regularization, artifact=artifact)
        indices = [i for i, r in enumerate(records) if r['group'] == test]
        scores[indices] = predict(model, x[indices])
        folds.append({'test_group': test, 'test_ids': [records[i]['id'] for i in indices],
                      'model': model, 'inner_search': search,
                      'outer_log_loss': pair_loss(model, x, pairs, test)})
        progress(f"{'ODAQ' if artifact else 'MED'} held out {test}: fitted on {len(training)} other contents")
    if not np.isfinite(scores).all():
        raise ValueError('Incomplete out-of-fold predictions')
    regularization, search = select_regularization(x, records, pairs, groups, artifact=artifact)
    final = fit_ranker(x, records, pairs, groups, regularization, artifact=artifact)
    return scores, folds, final | {'final_selection_inner_search': search}


def bootstrap(values):
    import numpy as np

    if not values:
        return None
    rng = np.random.default_rng(POLICY['seed'])
    return np.quantile(rng.choice(values, (10000, len(values))).mean(axis=1), [.025, .975]).tolist()


def score_predictions(records, pairs, scores, rms):
    import numpy as np
    from scipy.stats import spearmanr

    group_results, pair_results = [], []
    for group in sorted({r['group'] for r in records}):
        decisions = []
        for pair in (p for p in pairs if p['group'] == group):
            differences = np.array(pair['differences'])
            interval = bootstrap(differences.tolist())
            human = float(differences.mean())
            delta = float(scores[pair['a']]-scores[pair['b']])
            rms_delta = rms[pair['a']]-rms[pair['b']]

            def credit(value, human_difference=human):
                return .5 if abs(value) <= 1e-6 else float((value > 0) == (human_difference > 0))

            decisive = bool(interval[0] > 0 or interval[1] < 0)
            processed = all(records[pair[k]]['name'] not in ('reference', 'LP35', 'LP70') for k in ('a', 'b'))
            result = {k: v for k, v in pair.items() if k != 'differences'} | {
                'a_name': records[pair['a']]['name'], 'b_name': records[pair['b']]['name'],
                'raters': len(differences), 'human_mean_difference': human, 'human_95_ci': interval,
                'decisive': decisive, 'processed_only': processed, 'model_difference': delta,
                'credit': credit(delta), 'rms_credit': credit(rms_delta)}
            pair_results.append(result)
            if decisive:
                decisions.append(result)
        indices = [i for i, r in enumerate(records) if r['group'] == group]
        # Descriptive only: exclude each mix's author; pairwise targets also exclude the opponent.
        means = [float(np.mean([v for k, v in records[i]['ratings'].items()
                  if records[i]['dataset'] != 'MED' or re.sub(r'_(?:\d{4}|unknown)$', '', k) != records[i]['name']]))
                 for i in indices]
        chosen = int(np.argmax(scores[indices]))
        processed = [p for p in decisions if p['processed_only']]
        rho = float(spearmanr(means, scores[indices]).statistic)
        group_results.append({'group': group, 'versions': len(indices), 'decisive_pairs': len(decisions),
            'accuracy': float(np.mean([p['credit'] for p in decisions])) if decisions else None,
            'rms_accuracy': float(np.mean([p['rms_credit'] for p in decisions])) if decisions else None,
            'processed_pairs': len(processed),
            'processed_accuracy': float(np.mean([p['credit'] for p in processed])) if processed else None,
            'spearman': rho if math.isfinite(rho) else None,
            'selected': records[indices[chosen]]['name'], 'descriptive_human_regret': max(means)-means[chosen],
            'selections': [{'name': records[i]['name'], 'oof_score': float(scores[i]), 'human_mean': mean}
                           for i, mean in zip(indices, means)]})
    eligible = [g for g in group_results if g['accuracy'] is not None]
    accuracies = [g['accuracy'] for g in eligible]
    processed = [g['processed_accuracy'] for g in group_results if g['processed_accuracy'] is not None]
    differences = [g['accuracy']-g['rms_accuracy'] for g in eligible]
    return {'groups': group_results, 'pairs': pair_results,
        'groups_with_decisive_pairs': len(eligible), 'decisive_pairs': sum(g['decisive_pairs'] for g in eligible),
        'macro_accuracy': float(np.mean(accuracies)) if accuracies else None,
        'content_95_ci': bootstrap(accuracies),
        'macro_rms_accuracy': float(np.mean([g['rms_accuracy'] for g in eligible])) if eligible else None,
        'gain_over_rms_95_ci': bootstrap(differences),
        'processed_macro_accuracy': float(np.mean(processed)) if processed else None,
        'processed_95_ci': bootstrap(processed),
        'mean_spearman': float(np.mean([g['spearman'] for g in group_results if g['spearman'] is not None])),
        'mean_descriptive_human_regret': float(np.mean([g['descriptive_human_regret'] for g in group_results]))}


def run(directory, output, *, progress=print):
    """Train only from saved audio, deny networking and retain all outer fold models."""
    from .master_benchmark_cpu import _deadline, _memory, configure_cpu, offline_connections

    configure_cpu()
    import numpy as np

    from .preference_features import artifact_errors, extract, load_audio

    directory, output = Path(directory).resolve(), Path(output).resolve()
    protocol = read_json(directory/'protocol.json')
    if (protocol['implementation_sha256'] != implementation_hashes()
            or protocol['sources_sha256'] != file_hash(directory/'sources.json')
            or any(protocol[k] != v for k, v in POLICY.items())):
        raise ValueError('Frozen protocol, sources or code changed')
    if output.exists():
        raise ValueError('Choose a new immutable run directory')
    records = read_json(directory/'sources.json')['records']
    output.mkdir(parents=True)
    timer = _deadline(POLICY['budget']['wall_deadline_seconds'], output, 'training')
    started, cpu_started = time.monotonic(), time.process_time()
    try:
        with offline_connections() as attempts:
            initial_resources = resource_checks()
            feature_rows, repeats = [], []
            refs = {r['group']: r for r in records if r['dataset'] == 'ODAQ' and r['name'] == 'reference'}
            for index, record in enumerate(records):
                resource_checks()
                path = Path(record['source_file'])
                if file_hash(path) != record['source_sha256']:
                    raise ValueError('Changed audio: '+str(path))
                audio, rate = load_audio(path)
                features, health = extract(audio, rate)
                row = {'id': record['id'], 'features': features, 'health': health}
                if record['dataset'] == 'ODAQ':
                    ref_path = Path(refs[record['group']]['source_file'])
                    if file_hash(ref_path) != refs[record['group']]['source_sha256']:
                        raise ValueError('Changed ODAQ reference')
                    reference, ref_rate = load_audio(ref_path)
                    if rate != ref_rate:
                        raise ValueError('Rate mismatch')
                    row['artifact_errors'] = artifact_errors(audio, reference, rate)
                feature_rows.append(row)
                if index == 0 or (record['dataset'] == 'ODAQ' and not repeats[1:]):
                    duplicate, _ = extract(audio.copy(), rate)
                    repeats.append({'id': record['id'], 'exact_features_equal': duplicate == features})
                progress(f"Features {index+1}/{len(records)}: {record['dataset']} {record['group']}/{record['name']}")
                del audio
                # Short yield between files keeps this low-priority worker unobtrusive.
                time.sleep(.1)
            write_json(output/'features.json', {'rows': feature_rows, 'repeat_checks': repeats})
            summaries, fold_audits, models = {}, {}, {}
            for dataset in ('MED', 'ODAQ'):
                positions = [i for i, r in enumerate(records) if r['dataset'] == dataset]
                selected = [records[i] for i in positions]
                field, names = ('features', FEATURE_NAMES) if dataset == 'MED' else ('artifact_errors', ARTIFACT_NAMES)
                x = np.array([[feature_rows[i][field][n] for n in names] for i in positions])
                rms = [feature_rows[i]['health']['rms_dbfs'] for i in positions]
                pairs = make_pairs(selected)
                scores, folds, model = nested_predictions(x, selected, pairs, artifact=dataset == 'ODAQ', progress=progress)
                summaries[dataset] = score_predictions(selected, pairs, scores, rms)
                fold_audits[dataset] = folds
                models[dataset] = model | {'feature_names': list(names), 'experimental': True,
                    'requires_aligned_clean_reference': dataset == 'ODAQ',
                    'professional_church_mixing_validated': False, 'auto_apply_allowed': False}
            gate = POLICY['gates']
            med, odaq = summaries['MED'], summaries['ODAQ']
            common = lambda d: (d['groups_with_decisive_pairs'] >= gate['minimum_groups']
                and d['decisive_pairs'] >= gate['minimum_decisive_pairs']
                and d['content_95_ci'][0] > gate['accuracy_lower_ci_above'])
            med['screening_passed'] = bool(common(med) and med['macro_accuracy'] >= gate['MED_accuracy']
                and med['gain_over_rms_95_ci'][0] > gate['MED_gain_over_rms_lower_ci_above'])
            odaq['screening_passed'] = bool(common(odaq) and odaq['macro_accuracy'] >= gate['ODAQ_accuracy']
                and odaq['processed_macro_accuracy'] >= gate['ODAQ_processed_accuracy']
                and odaq['processed_95_ci'][0] > .5)
            checks = {'identical_feature_controls_passed': all(r['exact_features_equal'] for r in repeats),
                      'network_connection_attempts': len(attempts), 'GPU_runtime_imported': False,
                      'initial_resources': initial_resources, 'final_resources': resource_checks(),
                      'audio_uploads': 0, 'DAW_operations': 0, 'audio_device_operations': 0,
                      'held_out_content_is_absent_from_all_training_ids': all(
                          not set(f['test_ids']) & set(f['model']['training_ids'])
                          and f['test_group'] not in f['model']['training_groups']
                          and all(f['test_group'] not in s['train'] and f['test_group'] != s['test']
                                  for candidate in f['inner_search'] for s in candidate['folds'])
                          for folds in fold_audits.values() for f in folds)}
            if not checks['held_out_content_is_absent_from_all_training_ids'] or not checks['identical_feature_controls_passed']:
                raise RuntimeError('Evaluation integrity check failed')
            runtime = {'wall_seconds': time.monotonic()-started, 'cpu_seconds': time.process_time()-cpu_started,
                       'device': 'CPU', 'logical_cpus_allowed': 1, 'numerical_threads': 1,
                       'priority': 'idle', **_memory()}
            result = {'kind': 'wcfi_targeted_preference_results', 'protocol_sha256': file_hash(directory/'protocol.json'),
                      'datasets': summaries, 'checks': checks, 'runtime': runtime,
                      'professional_church_mixing_validated': False, 'processing_applied': False,
                      'limitations': [POLICY['previous_exposure'], 'Only nine MED songs and six ODAQ reference contents; wide uncertainty.',
                        'No independent church-specific preference labels. Generalization to WCFI, subtle edits and live use is untested.',
                        'MED published MP3 excerpts and original listener presentation levels; not a loudness-matched listening experiment.',
                        'ODAQ full-reference results require a clean aligned target; the unprocessed church mix is not automatically such a target.',
                        'Paired listener labels overlap across pairs; content, not pair count, is the resampling unit.']}
            write_json(output/'fold-models.json', fold_audits)
            write_json(output/'models.json', models)
            write_json(output/'results.json', result)
            (output/'report.html').write_text(render_report(result), encoding='utf-8')
            write_json(output/'evidence.json', {'protocol_sha256': result['protocol_sha256'],
                'files_sha256': {n: file_hash(output/n) for n in ('features.json', 'fold-models.json', 'models.json', 'results.json', 'report.html')}})
            return {'report': str(output/'report.html'), 'datasets': {k: {n: v for n, v in d.items() if n not in ('groups', 'pairs')}
                    for k, d in summaries.items()}, 'runtime': runtime, 'checks': checks}
    finally:
        timer.cancel()


def render_report(result):
    def percent(value):
        return 'n/a' if value is None else f'{value:.1%}'

    def interval(value):
        return 'n/a' if value is None else '–'.join(percent(v) for v in value)

    rows, groups = [], []
    for dataset, d in result['datasets'].items():
        rows.append(f"<tr><td>{dataset}</td><td>{d['groups_with_decisive_pairs']}</td><td>{d['decisive_pairs']}</td><td>{percent(d['macro_accuracy'])}</td><td>{interval(d['content_95_ci'])}</td><td>{percent(d['macro_rms_accuracy'])}</td><td>{d['screening_passed']}</td></tr>")
        for g in d['groups']:
            groups.append(f"<tr><td>{dataset}</td><td>{html.escape(g['group'])}</td><td>{g['versions']}</td><td>{percent(g['accuracy'])}</td><td>{percent(g['processed_accuracy']) if dataset == 'ODAQ' else '—'}</td><td>{html.escape(g['selected'])}</td></tr>")
    odaq = result['datasets']['ODAQ']
    limits = ''.join('<li>'+html.escape(v)+'</li>' for v in result['limitations'])
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>WCFI · Targeted mix preference experiment</title><style>
body{{background:#111622;color:#e7e8ed;font:16px/1.6 system-ui;max-width:1150px;margin:40px auto;padding:0 24px}}
h1,h2{{color:#f0b68c}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #404758}}
.note{{background:#252d3b;padding:18px;border-left:4px solid #f0b68c}}a{{color:#a8d5f0}}code{{overflow-wrap:anywhere}}</style>
<h1>Targeted mix preference · held-out content</h1><p class="note">Experimental: every reported score came from a model that excluded that entire song/reference from training and hyperparameter selection. Final all-data weights are saved separately and are not test predictions. No professional-mix certification or automatic processing.</p>
<p>MED: level-independent stereo/dynamics descriptors → regularized paired preference ranker.<br>ODAQ: separate aligned-reference error descriptors → constrained artifact ranker. ODAQ is fidelity, not musical taste.</p>
<h2>Out-of-fold results</h2><table><tr><th>Task</th><th>Contents</th><th>Decisive pairs</th><th>Accuracy</th><th>95% content interval</th><th>Higher RMS baseline</th><th>Screen passed</th></tr>{''.join(rows)}</table>
<p>ODAQ without reference or low-pass anchors: {percent(odaq['processed_macro_accuracy'])}, 95% interval {interval(odaq['processed_95_ci'])}. Chance: 50%. Gates were frozen before this training run; they are engineering screening requirements.</p>
<h2>Each held-out song/reference</h2><table><tr><th>Task</th><th>Held-out content</th><th>Versions</th><th>Accuracy</th><th>Processed only</th><th>Selected condition</th></tr>{''.join(groups)}</table>
<h2>Limits</h2><ul>{limits}</ul><h2>Reproducible evidence</h2><p>Nested leave-content-out tuning; scalers use training clips only. Equal weight per content in fitting, inner tuning and reporting. Listener ties receive half weight. No reviewer listening is requested.</p>
<p>CPU: {result['runtime']['wall_seconds']:.1f} wall seconds, {result['runtime']['cpu_seconds']:.1f} CPU seconds. Peak working set: {result['runtime'].get('peak_working_set_mib', 'unavailable')} MiB. One logical CPU, idle priority, one numeric thread. No GPU, audio-device, DAW or network operations.</p>
<p><a href="results.json">Full results</a> · <a href="fold-models.json">Every fold and scaler</a> · <a href="models.json">Experimental final weights</a> · <a href="features.json">Features and repeat controls</a> · <a href="evidence.json">Hashes</a></p>
<p>Sources: <a href="https://github.com/rd-conx/med-web">MED</a> · <a href="https://github.com/Fraunhofer-IIS/ODAQ">ODAQ</a>. Local research assets retain source licenses. Protocol SHA256: <code>{result['protocol_sha256']}</code></p></html>'''
