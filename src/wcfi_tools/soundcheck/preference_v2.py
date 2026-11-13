"""Selection-aware CPU preference study with reference-free artifact assessment.

V1 sources/results are immutable. Development CV and three fresh-song tests are
separate; the fresh answer key is read only after final predictions are saved.
"""
from __future__ import annotations

import csv
import hashlib
import html
import itertools
import time
from collections import defaultdict
from pathlib import Path

from .mix_preference import bootstrap, listener_differences, read_json, resource_checks
from .preference_v2_features import ALL_NAMES
from .profiles import file_hash, write_json

CONFIGS = tuple({'representation': r, 'regularization': lam}
                for r in ('base_linear', 'extended_linear', 'extended_concave')
                for lam in (1., .1, .01))
POLICY = {
    'kind': 'wcfi_preference_selection_v2', 'seed': 20261022,
    'preference_features': list(ALL_NAMES), 'artifact_features': list(ALL_NAMES),
    'family_grid': list(CONFIGS), 'feature_scaling': 'Training clips only; std <=1e-6 ->1; z clipped +/-4',
    'preference_loss': 'Pairwise soft listener preferences; top-pair and agreement weighting; equal total weight per song',
    'MED_inner_selection': 'Minimum mean maximum paired rating regret of selected candidate; log loss tie break; entire songs held out',
    'artifact_loss': 'No-reference sigmoid quality regression to mean ODAQ score /100; equal weight per content',
    'signal_alerts': {'mains_concentration_log10_above': 2., 'relative_flat_top_fraction_above': .001,
                      'status': 'Fixed engineering suspicious-signal flags, not confirmed defects; validate clean false alarms'},
    'ODAQ_inner_selection': 'Mean content soft-label quality log loss; no references provided to model',
    'concavity': 'Diagonal standardized squared feature coefficients constrained <=0; cannot reward extremes without bound',
    'outer_evaluation': 'Whole song/content LOSO; ODAQ pairs stay within rated trial; repeated content grouped across methods/remixes',
    'fresh_holdouts': ['Lolita', 'Lush', 'OldTree'],
    'fresh_holdout_policy': '15 mixes excluded from fitting/scaling/selection; save fixed model and predictions before opening answer key; no retry/tuning',
    'raters': {'development_MED': 8, 'ODAQ': 8, 'fresh_MED': 3,
               'fresh_note': 'Only five original raters; exclusion of authors can leave three. Fresh results are exploratory, not same-power as development.'},
    'decision': 'Forced selections reported even when abstaining. Auto candidate recommendation requires all leave-training-content-out models to agree on winner over every alternative, z <=4, and ODAQ predicted quality >=.6.',
    'synthetic_controls': 'One stable-hash selected clip per fresh song; identity, -12dB gain, clipping +12dB drive, additive hum/noise at -12dB relative RMS, lowpass 3.5kHz; damage controls RMS matched. No synthetic training labels.',
    'metrics': 'Macro content decisive-pair accuracy, maximum paired human regret and decisive alternatives beating selection; all pairs shared raters excluding either MED author; paired listener bootstrap 10000; content intervals conditional on CV predictions',
    'engineering_gate': {'development_MED_accuracy_minimum': .75, 'development_selection_loss_songs_at_most': 3,
                         'no_reference_ODAQ_accuracy_minimum': .80, 'ODAQ_processed_accuracy_minimum': .70,
                         'ODAQ_processed_lower_ci_above': .5},
    'claims': 'Exploratory development after reviewing v1 errors; only three new songs are prospective. No professional church-quality claim; auto apply disabled.',
    'budget': {'cores': 1, 'threads': 1, 'priority': 'idle', 'GPU': False,
               'working_set_mib': 2048, 'deadline_seconds': 2400},
}


def content_group(trial):
    """Conservative underlying-source groups, never the degradation method."""
    name = trial.split('_', 1)[1]
    if trial.startswith('DE_'):
        return 'film_'+name.split('_', 1)[0] if not name.startswith('female_speech_music') else 'female_speech_music'
    if name in ('02_violin', '21_violin'):
        return 'violin'
    return name


def code_hashes():
    root = Path(__file__).parent
    return {name: file_hash(root/name) for name in ('preference_v2.py', 'preference_v2_features.py',
             'preference_features.py', 'mix_preference.py')}


def prepare(previous, assets, acquired, output):
    previous, assets, acquired, output = [Path(p).resolve() for p in (previous, assets, acquired, output)]
    if output.exists():
        raise ValueError('Choose a new immutable v2 protocol directory')
    development = [r | {'trial': r['group']} for r in read_json(previous/'protocol-01/sources.json')['records'] if r['dataset'] == 'MED']
    ratings = defaultdict(dict)
    methods = {}
    with (assets/'odaq/ODAQ/ODAQ_listening_test/ODAQ_results.csv').open(encoding='utf-8-sig') as stream:
        for row in csv.DictReader(stream):
            ratings[(row['item'], row['process'])][row['subject']] = float(row['score'])
            methods[row['item']] = row['method']
    for (trial, name), values in sorted(ratings.items()):
        relative = Path('odaq/ODAQ/ODAQ_listening_test')/trial/(name+'.wav')
        path = assets/relative
        if not path.exists():
            path = acquired/relative
        development.append({'dataset': 'ODAQ', 'group': content_group(trial), 'trial': trial,
            'name': name, 'method': methods[trial], 'ratings': values, 'source_file': str(path),
            'id': hashlib.sha256(('ODAQ/'+trial+'/'+name).encode()).hexdigest()[:20],
            'source_sha256': file_hash(path)})
    from .songbench_external import _med_ratings

    metadata = read_json(assets/'assets/med-data.json')
    holdouts, key = [], []
    for song in metadata['songs']:
        if song['song_id'] not in POLICY['fresh_holdouts']:
            continue
        for mix in song['mixes']:
            if mix['mix_type'] != 'human' or not mix['has_audio']:
                continue
            path = acquired/'med-holdout'/(song['song_id']+'__'+mix['mix_code']+'.mp3')
            record = {'dataset': 'MED', 'group': song['song_id'], 'trial': song['song_id'], 'name': mix['mix_code'],
                'id': hashlib.sha256(('MED/'+song['song_id']+'/'+mix['mix_code']).encode()).hexdigest()[:20],
                'source_file': str(path), 'source_sha256': file_hash(path), 'license': song['license']}
            holdouts.append(record)
            key.append(record | {'ratings': _med_ratings(mix)})
    if len(holdouts) != 15 or set(POLICY['fresh_holdouts']) & {r['group'] for r in development if r['dataset'] == 'MED'}:
        raise ValueError('Fresh held-out songs missing or leaked into development')
    for row in development:
        if file_hash(Path(row['source_file'])) != row['source_sha256']:
            raise ValueError('Changed source audio')
    if {r['source_sha256'] for r in holdouts} & {r['source_sha256'] for r in development}:
        raise ValueError('Fresh holdout audio duplicates a development source')
    audio_groups = defaultdict(set)
    for row in development:
        if row['dataset'] == 'ODAQ':
            audio_groups[row['source_sha256']].add(row['group'])
    if any(len(groups) > 1 for groups in audio_groups.values()):
        raise ValueError('Identical ODAQ audio crosses content groups; group it before testing')
    output.mkdir(parents=True)
    write_json(output/'development.json', {'records': development})
    write_json(output/'holdout-blind.json', {'records': holdouts})
    write_json(output/'holdout-answer-key.json', {'records': key})
    write_json(output/'protocol.json', POLICY | {'code_sha256': code_hashes(),
        'inputs_sha256': {n: file_hash(output/n) for n in ('development.json', 'holdout-blind.json', 'holdout-answer-key.json')},
        'v1_weights': str(previous/'run-01/models.json'), 'v1_weights_sha256': file_hash(previous/'run-01/models.json')})
    return {'protocol': str(output/'protocol.json'), 'MED_development_mixes': sum(r['dataset'] == 'MED' for r in development),
            'fresh_MED_mixes': len(holdouts), 'ODAQ_clips': len(ratings),
            'ODAQ_content_groups': len({r['group'] for r in development if r['dataset'] == 'ODAQ'})}


def pairs_for(records, minimum=8):
    import numpy as np

    trials = defaultdict(list)
    for i, row in enumerate(records):
        trials[row['trial']].append(i)
    pairs = []
    for trial, indices in sorted(trials.items()):
        for a, b in itertools.combinations(indices, 2):
            d = listener_differences(records[a], records[b])
            if len(d) < minimum:
                continue
            pairs.append({'a': a, 'b': b, 'group': records[a]['group'], 'trial': trial,
                'target': float(np.mean((d > 0)+.5*(d == 0))), 'human': float(np.mean(d)), 'differences': d.tolist()})
    return pairs


def design(x, model):
    import numpy as np

    width = 32 if model['representation'] == 'base_linear' else x.shape[1]
    z = np.clip((x[:, :width]-model['mean'])/model['scale'], -4, 4)
    return np.column_stack((z, z*z)) if model['representation'] == 'extended_concave' else z


def fit(x, records, pairs, groups, config, *, artifact=False):
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import expit

    groups = set(groups)
    indices = [i for i, r in enumerate(records) if r['group'] in groups]
    width = 32 if config['representation'] == 'base_linear' else x.shape[1]
    mean, scale = x[indices, :width].mean(axis=0), x[indices, :width].std(axis=0)
    scale = np.where(scale > 1e-6, scale, 1.)
    model = config | {'mean': mean.tolist(), 'scale': scale.tolist(), 'artifact': artifact,
        'training_groups': sorted(groups), 'training_ids': [records[i]['id'] for i in indices]}
    z = design(x, model)
    bounds = [(None, None)]*width
    if config['representation'] == 'extended_concave':
        bounds += [(None, 0)]*width
    if artifact:
        matrix = np.column_stack((z[indices], np.ones(len(indices))))
        target = np.array([np.mean(list(records[i]['ratings'].values()))/100 for i in indices])
        counts = {g: sum(records[i]['group'] == g for i in indices) for g in groups}
        weight = np.array([1/counts[records[i]['group']] for i in indices])
        bounds += [(None, None)]
    else:
        selected = [p for p in pairs if p['group'] in groups]
        # Topness is computed exclusively from pairs in the training contents.
        wins = defaultdict(list)
        for p in selected:
            wins[p['a']].append(p['target'])
            wins[p['b']].append(1-p['target'])
        topness = {i: float(np.mean(v)) for i, v in wins.items()}
        matrix = np.array([z[p['a']]-z[p['b']] for p in selected])
        target = np.array([p['target'] for p in selected])
        raw_weight = np.array([(1+2*max(topness[p['a']], topness[p['b']]))*max(.1, abs(2*p['target']-1)) for p in selected])
        totals = {g: sum(w for w, p in zip(raw_weight, selected) if p['group'] == g) for g in groups}
        weight = np.array([w/totals[p['group']] for w, p in zip(raw_weight, selected)])
    weight /= weight.sum()
    penalty = np.ones(matrix.shape[1])
    if artifact:
        penalty[-1] = 0

    def objective(beta):
        logits = matrix @ beta
        loss = np.sum(weight*(np.logaddexp(0, logits)-target*logits)) + config['regularization']*np.sum(penalty*beta**2)/2
        gradient = matrix.T @ (weight*(expit(logits)-target)) + config['regularization']*penalty*beta
        return loss, gradient

    result = minimize(objective, np.zeros(matrix.shape[1]), jac=True, method='L-BFGS-B', bounds=bounds,
                      options={'maxiter': 700, 'ftol': 1e-11, 'gtol': 1e-7})
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError('V2 optimization failed: '+str(result.message))
    return model | {'coefficient': result.x.tolist(), 'optimizer_iterations': int(result.nit)}


def predict(model, x):
    import numpy as np

    z = design(np.asarray(x), model)
    if model['artifact']:
        z = np.column_stack((z, np.ones(len(z))))
    return z @ np.asarray(model['coefficient'])


def selection_regret(scores, pairs, group):
    import numpy as np

    eligible = [p for p in pairs if p['group'] == group]
    indices = sorted({p[k] for p in eligible for k in ('a', 'b')})
    if not indices:
        raise ValueError('No evaluable candidate pairs in a test content')
    chosen = max(indices, key=lambda i: scores[i])
    regret = max([0.] + [-p['human'] if p['a'] == chosen else p['human']
                         for p in eligible if chosen in (p['a'], p['b'])])
    losses = [np.logaddexp(0, scores[p['a']]-scores[p['b']])-p['target']*(scores[p['a']]-scores[p['b']]) for p in eligible]
    return float(regret), float(np.mean(losses))


def tune(x, records, pairs, groups, *, artifact=False):
    import numpy as np

    groups = sorted(groups)
    candidates = []
    for config in CONFIGS:
        metrics = []
        for test in groups:
            model = fit(x, records, pairs, [g for g in groups if g != test], config, artifact=artifact)
            scores = predict(model, x)
            if artifact:
                indices = [i for i, r in enumerate(records) if r['group'] == test]
                target = np.array([np.mean(list(records[i]['ratings'].values()))/100 for i in indices])
                loss = float(np.mean(np.logaddexp(0, scores[indices])-target*scores[indices]))
                regret = loss
            else:
                regret, loss = selection_regret(scores, pairs, test)
            metrics.append({'test': test, 'train': [g for g in groups if g != test], 'regret': regret, 'log_loss': loss})
        candidates.append({'config': config, 'mean_regret': float(np.mean([v['regret'] for v in metrics])),
                           'mean_log_loss': float(np.mean([v['log_loss'] for v in metrics])), 'folds': metrics})
    winner = min(candidates, key=lambda c: (round(c['mean_regret'], 8), c['mean_log_loss']))
    return winner['config'], candidates


def nested(x, records, *, artifact=False, progress=print):
    import numpy as np

    pairs = pairs_for(records)
    groups = sorted({r['group'] for r in records})
    scores = np.full(len(records), np.nan)
    folds = []
    for test in groups:
        resource_checks()
        training = [g for g in groups if g != test]
        config, search = tune(x, records, pairs, training, artifact=artifact)
        model = fit(x, records, pairs, training, config, artifact=artifact)
        indices = [i for i, r in enumerate(records) if r['group'] == test]
        scores[indices] = predict(model, x[indices])
        folds.append({'test': test, 'test_ids': [records[i]['id'] for i in indices], 'model': model, 'inner_search': search})
        progress(f"{'Artifact' if artifact else 'MED'} entire content held out: {test}")
    config, search = tune(x, records, pairs, groups, artifact=artifact)
    model = fit(x, records, pairs, groups, config, artifact=artifact)
    ensemble = [fit(x, records, pairs, [s for s in groups if s != g], config, artifact=artifact) for g in groups]
    if not np.isfinite(scores).all():
        raise RuntimeError('Incomplete held-out predictions')
    return scores, folds, model | {'final_inner_search': search, 'experimental': True, 'auto_apply_allowed': False}, ensemble


def evaluate(records, scores, *, minimum=8, artifact=False):
    import numpy as np
    from scipy.special import expit

    pairs = pairs_for(records, minimum)
    trials = defaultdict(list)
    for i, r in enumerate(records):
        trials[r['trial']].append(i)
    results, pair_rows = [], []
    for trial, indices in sorted(trials.items()):
        chosen = max(indices, key=lambda i: scores[i])
        evaluated, regrets = [], []
        for p in (p for p in pairs if p['trial'] == trial):
            ci = bootstrap(p['differences'])
            decisive = ci[0] > 0 or ci[1] < 0
            delta = float(scores[p['a']]-scores[p['b']])
            credit = .5 if abs(delta) <= 1e-6 else float((delta > 0) == (p['human'] > 0))
            processed = all(records[p[k]]['name'] not in ('reference', 'LP35', 'LP70') for k in ('a', 'b'))
            against = chosen in (p['a'], p['b'])
            loss = (-p['human'] if chosen == p['a'] else p['human']) if against else 0.
            item = {k: v for k, v in p.items() if k != 'differences'} | {'a_name': records[p['a']]['name'],
                'b_name': records[p['b']]['name'], 'raters': len(p['differences']), 'CI': ci,
                'decisive': bool(decisive), 'credit': credit, 'processed': processed,
                'model_difference': delta, 'selected_loss': bool(against and decisive and loss > 0)}
            pair_rows.append(item)
            evaluated.append(item)
            regrets.append(loss)
        clear = [p for p in evaluated if p['decisive']]
        processed = [p for p in clear if p['processed']]
        results.append({'trial': trial, 'group': records[indices[0]]['group'], 'selected': records[chosen]['name'],
            'accuracy': float(np.mean([p['credit'] for p in clear])) if clear else None,
            'processed_accuracy': float(np.mean([p['credit'] for p in processed])) if processed else None,
            'decisive_pairs': len(clear), 'processed_pairs': len(processed),
            'has_decisively_better_alternative': any(p['selected_loss'] for p in evaluated),
            'maximum_paired_regret': max([0.] + regrets)})
    groups = defaultdict(list)
    for row in results:
        groups[row['group']].append(row)
    content = []
    for group, rows in sorted(groups.items()):
        clear = [r['accuracy'] for r in rows if r['accuracy'] is not None]
        processed = [r['processed_accuracy'] for r in rows if r['processed_accuracy'] is not None]
        content.append({'group': group, 'accuracy': float(np.mean(clear)) if clear else None,
                        'processed_accuracy': float(np.mean(processed)) if processed else None})
    accuracies = [g['accuracy'] for g in content if g['accuracy'] is not None]
    processed = [g['processed_accuracy'] for g in content if g['processed_accuracy'] is not None]
    result = {'trials': results, 'contents': content, 'pairs': pair_rows,
        'macro_accuracy': float(np.mean(accuracies)) if accuracies else None, 'CI': bootstrap(accuracies),
        'processed_accuracy': float(np.mean(processed)) if processed else None, 'processed_CI': bootstrap(processed),
        'decisive_pairs': sum(r['decisive_pairs'] for r in results), 'processed_pairs': sum(r['processed_pairs'] for r in results),
        'selection_failure_trials': sum(r['has_decisively_better_alternative'] for r in results),
        'maximum_paired_regret_mean': float(np.mean([r['maximum_paired_regret'] for r in results])),
        'independent_contents': len(content)}
    if artifact:
        quality = expit(scores)
        truth = np.array([np.mean(list(r['ratings'].values()))/100 for r in records])
        bad, clean = truth <= .6, truth >= .85
        result['quality_calibration'] = {
            'score_mean_absolute_error_0_100': float(np.mean(np.abs(quality-truth))*100),
            'poor_audio_recall_at_60': float(np.mean(quality[bad] < .6)) if np.any(bad) else None,
            'clean_audio_false_alarm_at_60': float(np.mean(quality[clean] < .6)) if np.any(clean) else None,
            'poor_conditions': int(bad.sum()), 'clean_conditions': int(clean.sum()),
            'threshold': .6, 'scope': 'ODAQ human quality <=60 versus >=85; not an artifact-type classifier'}
    return result


def explain_v1(previous):
    """Exact linear score decompositions, not invented descriptions of audible faults."""
    import numpy as np

    result = read_json(previous/'run-01/results.json')['datasets']['MED']
    folds = read_json(previous/'run-01/fold-models.json')['MED']
    features = {r['id']: r['features'] for r in read_json(previous/'run-01/features.json')['rows']}
    sources = {(r['group'], r['name']): r for r in read_json(previous/'protocol-01/sources.json')['records'] if r['dataset'] == 'MED'}
    failures = []
    for group in result['groups']:
        model = next(f['model'] for f in folds if f['test_group'] == group['group'])
        chosen = group['selected']
        for pair in result['pairs']:
            if pair['group'] != group['group'] or not pair['decisive'] or chosen not in (pair['a_name'], pair['b_name']):
                continue
            human_difference = pair['human_mean_difference'] * (1 if chosen == pair['a_name'] else -1)
            if human_difference >= 0:
                continue
            better = pair['b_name'] if chosen == pair['a_name'] else pair['a_name']
            a, b = sources[(group['group'], chosen)], sources[(group['group'], better)]
            names = list(features[a['id']])
            xa = np.array([features[a['id']][n] for n in names])
            xb = np.array([features[b['id']][n] for n in names])
            za = np.clip((xa-model['mean'])/model['scale'], -8, 8)
            zb = np.clip((xb-model['mean'])/model['scale'], -8, 8)
            contributions = (za-zb)*np.array(model['coefficient'])
            failures.append({'song': group['group'], 'selected': chosen, 'human_preferred_alternative': better,
                'paired_human_difference_selected_minus_alternative': human_difference,
                'selected_majority_fraction': pair['target'] if chosen == pair['a_name'] else 1-pair['target'],
                'score_advantage': float(contributions.sum()),
                'contributions': sorted([{'feature': n, 'score_contribution': float(c),
                    'selected_training_z': float(a), 'alternative_training_z': float(b)}
                    for n, c, a, b in zip(names, contributions, za, zb)], key=lambda r: -r['score_contribution'])})
    return {'failures': failures, 'interpretation': [
        'Linear spectral/dynamic proxies can outweigh clear listener disagreement; coefficients do not encode an optimal instrument balance.',
        'These selected losses also disagree with listener majorities: not merely a difference between mean-rating and majority targets.',
        'V1 tuned average pair log loss, not worst alternative beating its selected candidate.',
        'No artifact guard received training on no-reference artifact labels. Full-reference success did not transfer.',
        'A positive coefficient is a score attribution, not proof of an audible defect or an instruction to EQ that band.']}


def fresh_decisions(records, x, model, ensemble, artifact_model):
    import numpy as np
    from scipy.special import expit

    score = predict(model, x)
    quality = expit(predict(artifact_model, x))
    ensemble_scores = np.array([predict(m, x) for m in ensemble])
    decisions = []
    width = len(model['mean'])
    for group in sorted({r['group'] for r in records}):
        indices = [i for i, r in enumerate(records) if r['group'] == group]
        chosen = max(indices, key=lambda i: score[i])
        agreement = all(np.all(ensemble_scores[:, chosen]-ensemble_scores[:, other] > 1e-6)
                        for other in indices if other != chosen)
        maximum_z = float(np.max(np.abs((x[chosen, :width]-model['mean'])/model['scale'])))
        reasons = []
        if not agreement:
            reasons.append('Leave-training-song-out models disagree about this winner')
        if maximum_z > 4:
            reasons.append('Candidate features exceed the training scaling range')
        if quality[chosen] < .6:
            reasons.append('No-reference artifact model predicts ODAQ quality below 60/100')
        alerts = no_reference_alerts(dict(zip(ALL_NAMES, x[chosen])))
        reasons.extend(alerts)
        decisions.append({'song': group, 'forced_selected': records[chosen]['name'],
                          'recommendation': records[chosen]['name'] if not reasons else None,
                          'abstained': bool(reasons), 'reasons': reasons,
                          'artifact_predicted_quality_0_100': float(quality[chosen]*100),
                          'maximum_training_z': maximum_z, 'ensemble_unanimity': agreement})
    return score, quality, decisions


def no_reference_alerts(features):
    alerts = []
    thresholds = POLICY['signal_alerts']
    if max(features['mains50_concentration'], features['mains60_concentration']) > thresholds['mains_concentration_log10_above']:
        alerts.append('Strong mains-harmonic concentration; tonal instruments can imitate hum')
    if features['relative_flat_top_fraction'] > thresholds['relative_flat_top_fraction_above']:
        alerts.append('Sustained flat peaks suggest clipping or strong limiting')
    return alerts


def controls(records, model, artifact_model, *, progress=print):
    import numpy as np
    import soundfile as sf
    from scipy.signal import butter, sosfilt
    from scipy.special import expit

    from .preference_v2_features import extract_native

    rows = []
    for group in sorted({r['group'] for r in records}):
        source = min([r for r in records if r['group'] == group],
                     key=lambda r: hashlib.sha256(('v2-control/'+r['id']).encode()).hexdigest())
        audio, rate = sf.read(source['source_file'], dtype='float32', always_2d=True)
        rms = np.sqrt(np.mean(audio.astype('float64')**2))
        t = np.arange(len(audio))/rate
        hum = sum(np.sin(2*np.pi*f*t) for f in (60, 120, 180))[:, None]
        hum = hum/np.sqrt(np.mean(hum**2))*rms*10**(-12/20)
        rng = np.random.default_rng(POLICY['seed'])
        noise = rng.normal(size=audio.shape)
        noise = noise/np.sqrt(np.mean(noise**2))*rms*10**(-12/20)
        damaged = {'identity': audio.copy(), 'gain_minus12dB': audio*10**(-12/20),
                   'clipping_drive12dB': np.clip(audio*10**(12/20), -.95, .95),
                   'mains_hum': audio+hum, 'white_noise': audio+noise,
                   'lowpass_3500': sosfilt(butter(6, 3500, fs=rate, output='sos'), audio, axis=0)}
        baseline = None
        for name, variant in damaged.items():
            if name not in ('identity', 'gain_minus12dB'):
                variant = variant*rms/np.sqrt(np.mean(variant.astype('float64')**2))
            features, _ = extract_native(variant, rate)
            x = np.array([[features[n] for n in ALL_NAMES]])
            preference = float(predict(model, x)[0])
            quality = float(expit(predict(artifact_model, x)[0])*100)
            if baseline is None:
                baseline = (preference, quality)
            rows.append({'song': group, 'source_id': source['id'], 'condition': name,
                'preference_score': preference, 'quality_0_100': quality,
                'preference_delta': preference-baseline[0], 'quality_delta': quality-baseline[1],
                'mains60_concentration': features['mains60_concentration'],
                'relative_flat_top_fraction': features['relative_flat_top_fraction'],
                'signal_alerts': no_reference_alerts(features),
                'combined_artifact_flag': quality < 60 or bool(no_reference_alerts(features)),
                'expected': 'same' if name in ('identity', 'gain_minus12dB') else 'worse_synthetic_stress_not_a_human_preference_label'})
            progress('Fresh-song synthetic stress: '+group+'/'+name)
    return rows


def run(directory, previous, output, *, progress=print):
    from .master_benchmark_cpu import _deadline, _memory, configure_cpu, offline_connections

    configure_cpu()
    import numpy as np
    import soundfile as sf

    from .mix_preference import predict as predict_v1
    from .preference_v2_features import extract_native

    directory, previous, output = [Path(p).resolve() for p in (directory, previous, output)]
    policy = read_json(directory/'protocol.json')
    if policy['code_sha256'] != code_hashes() or any(policy[k] != v for k, v in POLICY.items()):
        raise ValueError('Changed v2 code or protocol')
    for name, digest in policy['inputs_sha256'].items():
        if file_hash(directory/name) != digest:
            raise ValueError('Changed source/key file')
    if file_hash(Path(policy['v1_weights'])) != policy['v1_weights_sha256']:
        raise ValueError('Changed v1 baseline')
    if output.exists():
        raise ValueError('Choose a new immutable run directory')
    output.mkdir(parents=True)
    began, cpu_began = time.monotonic(), time.process_time()
    timer = _deadline(POLICY['budget']['deadline_seconds'], output, 'v2')
    try:
        with offline_connections() as attempts:
            initial_resources = resource_checks()
            development = read_json(directory/'development.json')['records']
            holdout = read_json(directory/'holdout-blind.json')['records']
            write_json(output/'v1-failure-analysis.json', explain_v1(previous))
            extracted, repeats = {}, []
            for i, record in enumerate(development+holdout):
                resource_checks()
                path = Path(record['source_file'])
                if file_hash(path) != record['source_sha256']:
                    raise ValueError('Changed source audio')
                audio, rate = sf.read(path, dtype='float32', always_2d=True)
                features, health = extract_native(audio, rate)
                extracted[record['id']] = {'features': features, 'health': health}
                if i in (0, len(development)):
                    repeated, _ = extract_native(audio.copy(), rate)
                    repeats.append({'id': record['id'], 'exact_repeat': features == repeated})
                progress(f'Native features {i+1}/{len(development)+len(holdout)}: {record["dataset"]} {record["trial"]}/{record["name"]}')
                del audio
                time.sleep(.05)
            write_json(output/'features.json', {'rows': extracted, 'repeat_checks': repeats})
            evaluated, trained, fold_models, ensembles = {}, {}, {}, {}
            for dataset in ('MED', 'ODAQ'):
                rows = [r for r in development if r['dataset'] == dataset]
                x = np.array([[extracted[r['id']]['features'][n] for n in ALL_NAMES] for r in rows])
                scores, folds, model, ensemble = nested(x, rows, artifact=dataset == 'ODAQ', progress=progress)
                evaluated[dataset] = evaluate(rows, scores, artifact=dataset == 'ODAQ')
                trained[dataset], fold_models[dataset], ensembles[dataset] = model, folds, ensemble
                if dataset == 'MED':
                    old_folds = read_json(previous/'run-01/fold-models.json')['MED']
                    old_scores = np.array([predict_v1(next(f['model'] for f in old_folds if f['test_group'] == r['group']),
                                                       x[i:i+1, :32])[0] for i, r in enumerate(rows)])
                    evaluated['MED_v1_LOSO_baseline'] = evaluate(rows, old_scores)
                else:
                    from scipy.special import expit

                    poor = np.array([np.mean(list(r['ratings'].values())) <= 60 for r in rows])
                    clean = np.array([np.mean(list(r['ratings'].values())) >= 85 for r in rows])
                    flags = np.array([bool(no_reference_alerts(extracted[r['id']]['features'])) for r in rows])
                    combined = (expit(scores) < .6) | flags
                    evaluated[dataset]['signal_alert_screen'] = {
                        'flags_on_clean_conditions': int(flags[clean].sum()), 'clean_conditions': int(clean.sum()),
                        'combined_poor_audio_recall': float(np.mean(combined[poor])),
                        'combined_clean_false_alarm_fraction': float(np.mean(combined[clean])),
                        'note': 'Suspicious-signal flags plus quality below60; alerts are not proof of an audible defect'}
            write_json(output/'models.json', {'models': trained, 'ensembles': ensembles, 'feature_names': list(ALL_NAMES),
                       'experimental': True, 'auto_apply_allowed': False})
            write_json(output/'fold-models.json', fold_models)
            x = np.array([[extracted[r['id']]['features'][n] for n in ALL_NAMES] for r in holdout])
            score, quality, decisions = fresh_decisions(holdout, x, trained['MED'], ensembles['MED'], trained['ODAQ'])
            baseline = read_json(Path(policy['v1_weights']))['MED']
            baseline_scores = predict_v1(baseline, x[:, :32])
            # Irreversible evidence point: predictions/weights exist before fresh labels are read.
            write_json(output/'fresh-predictions-before-grading.json', {'holdout_key_opened': False,
                'fixed_models_sha256': file_hash(output/'models.json'),
                'predictions': [{'id': r['id'], 'v2_score': float(s), 'v1_score': float(b), 'artifact_quality': float(q)}
                                for r, s, b, q in zip(holdout, score, baseline_scores, quality)], 'decisions': decisions})
            key = read_json(directory/'holdout-answer-key.json')['records']
            if [r['id'] for r in key] != [r['id'] for r in holdout]:
                raise ValueError('Fresh answer key order mismatch')
            evaluated['fresh_MED_v2'] = evaluate(key, score, minimum=3)
            evaluated['fresh_MED_v1'] = evaluate(key, baseline_scores, minimum=3)
            artifact_rows = [r for r in development if r['dataset'] == 'ODAQ']
            artifact_x = np.array([[extracted[r['id']]['features'][n] for n in ALL_NAMES] for r in artifact_rows])
            evaluated['ODAQ_v1_preference_baseline'] = evaluate(artifact_rows, predict_v1(baseline, artifact_x[:, :32]))
            control_rows = controls(holdout, trained['MED'], trained['ODAQ'], progress=progress)
            gain_ok = all(abs(r['preference_delta']) < 1e-4 and abs(r['quality_delta']) < 1e-3
                          for r in control_rows if r['expected'] == 'same')
            groups_disjoint = all(not set(f['test_ids']) & set(f['model']['training_ids'])
                and f['test'] not in f['model']['training_groups']
                and all(f['test'] != v['test'] and f['test'] not in v['train']
                        for c in f['inner_search'] for v in c['folds'])
                for folds in fold_models.values() for f in folds)
            fresh_excluded = not {r['id'] for r in holdout} & {
                identifier for model in trained.values() for identifier in model['training_ids']}
            if not groups_disjoint or not fresh_excluded or not gain_ok or not all(r['exact_repeat'] for r in repeats):
                raise RuntimeError('V2 integrity/control check failed')
            gate = POLICY['engineering_gate']
            med, odaq = evaluated['MED'], evaluated['ODAQ']
            result = {'kind': 'wcfi_v2_preference_results', 'protocol_sha256': file_hash(directory/'protocol.json'),
                'evaluation': evaluated, 'fresh_decisions': decisions, 'synthetic_controls': control_rows,
                'engineering_screen_passed': bool(med['macro_accuracy'] >= gate['development_MED_accuracy_minimum']
                    and med['selection_failure_trials'] <= gate['development_selection_loss_songs_at_most']
                    and odaq['macro_accuracy'] >= gate['no_reference_ODAQ_accuracy_minimum']
                    and odaq['processed_accuracy'] >= gate['ODAQ_processed_accuracy_minimum']
                    and odaq['processed_CI'][0] > gate['ODAQ_processed_lower_ci_above']),
                'runtime': {'wall_seconds': time.monotonic()-began, 'cpu_seconds': time.process_time()-cpu_began,
                            'initial_resources': initial_resources, 'final_resources': resource_checks(), **_memory()},
                'checks': {'outer_and_inner_content_disjoint': groups_disjoint, 'fresh_songs_excluded': fresh_excluded,
                    'predictions_saved_before_fresh_key_read': True, 'identity_and_gain_controls': gain_ok,
                    'no_reference_artifact_inputs': True, 'network_attempts': len(attempts), 'GPU': False,
                    'DAW_operations': 0, 'desktop_operations': 0, 'uploads': 0},
                'professional_church_mixing_validated': False, 'auto_apply_allowed': False,
                'limitations': [POLICY['claims'], POLICY['raters']['fresh_note'],
                    'ODAQ quality regression is not artifact-type identification or guaranteed detection of every defect.',
                    'Synthetic damaged examples are sanity checks, not replacement expert listening labels.',
                    'Development song results are exploratory after studying v1 errors; intervals condition on these predictions.']}
            write_json(output/'results.json', result)
            (output/'report.html').write_text(render(result), encoding='utf-8')
            write_json(output/'evidence.json', {'protocol_sha256': result['protocol_sha256'],
                'files_sha256': {n: file_hash(output/n) for n in ('models.json', 'features.json', 'fold-models.json',
                    'fresh-predictions-before-grading.json', 'results.json', 'v1-failure-analysis.json', 'report.html')}})
            return {'report': str(output/'report.html'), 'engineering_screen_passed': result['engineering_screen_passed'],
                'evaluations': {k: {n: v for n, v in e.items() if n not in ('trials', 'contents', 'pairs')}
                                for k, e in evaluated.items()}, 'runtime': result['runtime'], 'fresh_decisions': decisions,
                'checks': result['checks']}
    finally:
        timer.cancel()


def render(result):
    def pct(value):
        return 'n/a' if value is None else f'{value:.1%}'

    rows = ''
    for name, d in result['evaluation'].items():
        ci = 'n/a' if d['CI'] is None else '–'.join(pct(v) for v in d['CI'])
        rows += f"<tr><td>{html.escape(name)}</td><td>{d['independent_contents']}</td><td>{d['decisive_pairs']}</td><td>{pct(d['macro_accuracy'])}</td><td>{ci}</td><td>{d['selection_failure_trials']}</td><td>{d['maximum_paired_regret_mean']:.3f}</td></tr>"
    selection = ''.join(f"<li>{html.escape(d['song'])}: forced choice {html.escape(d['forced_selected'])}; {'abstain: '+html.escape('; '.join(d['reasons'])) if d['abstained'] else 'all recommendation conditions met'}.</li>" for d in result['fresh_decisions'])
    controls_rows = ''.join(f"<tr><td>{html.escape(r['song'])}</td><td>{html.escape(r['condition'])}</td><td>{r['preference_delta']:+.3f}</td><td>{r['quality_delta']:+.2f}</td><td>{html.escape('; '.join(r['signal_alerts'])) or '—'}</td></tr>" for r in result['synthetic_controls'])
    limits = ''.join('<li>'+html.escape(v)+'</li>' for v in result['limitations'])
    odaq = result['evaluation']['ODAQ']
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>WCFI · Selection-aware evaluator v2</title><style>body{{background:#131925;color:#e8eaf0;font:16px/1.6 system-ui;max-width:1200px;margin:40px auto;padding:0 24px}}h1,h2{{color:#efb28a}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;border-bottom:1px solid #465063;padding:10px}}a{{color:#a9d9ed}}.card{{background:#263043;padding:18px;border-left:4px solid #efb28a}}</style>
<h1>Selection-aware evaluator · CPU v2</h1><p class="card">Experimental. Automatic application remains disabled. Original frozen engineering screen passed: {result['engineering_screen_passed']}. Scores and selection failures below include forced choices even when the recommendation rule abstains.</p>
<h2>Why v1 selected worse mixes</h2><p>The eight losing comparisons across six songs were also opposed by listener majorities. Exact score decompositions show brightness, low-band power, crest factor and related proxies outweighing that preference. V1 used a linear score and tuned average pair log loss, not the quality of its chosen winner. These are model explanations, not claims of audible defects. <a href="v1-failure-analysis.json">All score contributions</a>.</p>
<h2>Changes</h2><p>Training-content-only selection tuning, agreement/top-pair weights, and a small grid including bounded concave nonlinear features. Native-bandwidth flatness, spectral gaps/isolated peaks, transient shape, relative clipping and narrow mains concentration complement the original stereo descriptors. The separate ODAQ model receives only the candidate waveform: no clean reference, source identity or degradation label.</p>
<h2>Held-out results</h2><table><tr><th>Evaluation</th><th>Contents</th><th>Clear pairs</th><th>Accuracy</th><th>95% content interval</th><th>Selections with better alternative</th><th>Mean maximum paired regret</th></tr>{rows}</table>
<p>ODAQ processed conditions without reference or anchors: {pct(odaq['processed_accuracy'])}; 95% interval {'–'.join(pct(v) for v in odaq['processed_CI'])}. Repeated source audio, films and remixes remain together across methods. Pairs are compared only within their original rated trial.</p>
<p>Fresh MED songs have only five original raters; after author exclusion some pairs have three. Their scores are exploratory and less certain than the development results. The fresh answer key was opened only after <a href="fresh-predictions-before-grading.json">fixed models and predictions were saved</a>.</p>
<h2>Fresh-song recommendations</h2><ul>{selection}</ul><p>Ensemble agreement is a caution rule, not a calibrated confidence interval. The ODAQ quality estimate is not proof of professional church quality.</p>
<h2>Fresh-song synthetic controls</h2><p>One metadata-hash-selected source per fresh song; deliberate defects matched in RMS, with identity and gain-only controls. Negative deltas indicate rejection of damage. Fixed mains/flat-peak alerts are suspicious-signal checks, not confirmed diagnoses. Their clean-condition false alarms are in the full results. These tests are not human preference labels and do not replace them.</p><table><tr><th>Song</th><th>Condition</th><th>Preference change</th><th>Artifact quality change /100</th><th>Candidate-only alerts</th></tr>{controls_rows}</table>
<h2>Limits and runtime</h2><ul>{limits}</ul><p>{result['runtime']['wall_seconds']:.1f} wall seconds; {result['runtime']['cpu_seconds']:.1f} CPU seconds; peak {result['runtime'].get('peak_working_set_mib','unknown')} MiB. One CPU, idle priority, numerical threads 1. No GPU, playback, DAW/desktop control, uploads or network attempts. No user listening requested.</p>
<p><a href="results.json">Full results</a> · <a href="models.json">Experimental weights</a> · <a href="fold-models.json">Nested models and tuning</a> · <a href="features.json">Waveform features</a> · <a href="evidence.json">Hashes</a></p></html>'''
