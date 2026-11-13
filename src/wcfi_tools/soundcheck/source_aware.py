"""Auditable source-aware CPU experiments: human metadata and real-stem controls.

MED annotations are only input-LUFS plus fader estimates. Never substitute them
for measured source contributions or claim they describe rendered processing.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from .profiles import file_hash, write_json

ANNOTATION_ROLES = ('lead', 'backing', 'vocals_unknown', 'drums', 'bass', 'guitar', 'keys', 'other')
ANNOTATION_FEATURES = tuple(f'proxy_{role}_{kind}' for role in ANNOTATION_ROLES for kind in ('relative_db', 'present'))
CONFIGS = tuple({'representation': rep, 'regularization': value}
    for rep in ('extended_linear', 'extended_concave') for value in (1., .1))
POLICY = {
    'kind': 'wcfi_source_aware_benchmark_v1', 'seed': 20261022,
    'MED': 'Only exact song+mix matches between saved audio/ratings and complete MixParams rows; MgC prefix normalized to McG with join audit.',
    'proxy': 'log-sum power of input LUFS plus fader gain per role. Omits dynamic automation, plugin gain, bus gain and phase interaction. NOT measured output source balance.',
    'human_test': 'Whole-song nested LOSO; all variants stay together. Compare 32 global features, 16 source proxies, and their concatenation on the SAME records/folds. Training-only scaling/tuning, equal song weight.',
    'human_pairs': 'At least 3 shared listeners excluding both mix authors. Report >=8 separately; 3-listener songs exploratory. Prior study songs already examined: no pristine final MED test claim.',
    'inner_selection': 'Minimum mean maximum paired human selection regret, then pairwise soft-label log loss; 2 representations x 2 regularizations.',
    'features_excluded': ['song/artist/mix IDs', 'mixer institution', 'track filenames', 'ratings/comments', 'expert ranking'],
    'MUSDB': 'Official v0.4.0 7-second STEMS sample. All 50 official test songs; training songs with a test artist excluded. Actual archive has 94 train tracks, not the full 100.',
    'decoding': 'Four synchronized AAC source streams, ffmpeg software decoder, 16k stereo float32. Build mixture by summing sources; separately encoded stream 0 is not used.',
    'controls': 'For each test song/active role, +/-12dB, -60dB (near absent), +/-6dB source gain. Each candidate whole-mixture RMS matched to original; raw activity unchanged. Identity and -12dB global gain controls.',
    'calibration': 'Training original source relative energy distributions, 5th–95th percentiles +3dB; minimum 5 songs per feature. No test-song fitting or feedback.',
    'control_judgment': 'Original versus deliberately perturbed sources tests recovery of original balance; no human ratings of these perturbations. Instrument OTHER is not separately identified guitar/keys.',
    'control_gate': {'extreme_original_preference_fraction': .8, 'clean_song_alert_fraction_at_most': .2,
        'gain_and_identity_maximum_feature_error_db': .001},
    'human_gate': 'Combined must improve paired macro accuracy over global-only with 95% song-bootstrap improvement CI >0, and reduce selection failures. No auto apply even if passed: source proxies require measured-stem confirmation.',
    'budget': {'CPU_cores': 1, 'numerical_threads': 1, 'priority': 'idle', 'GPU': False,
        'maximum_working_set_MiB': 2048, 'deadline_seconds': 2400},
    'operations': 'Saved files only; no playback, audio devices, Ableton, desktop control or uploads during benchmark.',
    'auto_apply_allowed': False,
}


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def annotation_role(row):
    kind, sub = row['track_instrument_type'], row['track_instrument_subtype']
    if kind == 'VOCALS':
        return {'LEAD': 'lead', 'BACKGROUND': 'backing'}.get(sub, 'vocals_unknown')
    return {'DRUMS': 'drums', 'BASS': 'bass', 'GUITARS': 'guitar', 'KEYS': 'keys'}.get(kind, 'other')


def annotation_features(rows):
    """Approximate relative levels, explicitly NOT post-FX waveform measurements."""
    energy = dict.fromkeys(ANNOTATION_ROLES, 0.)
    for row in rows:
        lufs = row['track_audio_lufs']
        gain = json.loads(row['parameters'])['gain']
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lufs, gain)):
            raise ValueError('Nonfinite annotation level')
        energy[annotation_role(row)] += 10**((lufs+gain)/10)
    total = sum(energy.values())
    if total <= 0:
        raise ValueError('Silent annotation')
    features = []
    for role in ANNOTATION_ROLES:
        e = energy[role]
        ratio = max(-60., min(60., 10*math.log10(max(e, 1e-30)/max(total-e, 1e-30))))
        features.extend([ratio if e else 0., float(e > 0)])
    return features


def _song_id(name):
    return ''.join(c for c in name.lower() if c.isalnum())


def prepare(metadata, previous, assets, output):
    """Freeze joins, splits, code and source hashes before scoring anything."""
    from ..audio.source_evaluation import POLICY as source_policy

    metadata, previous, assets, output = [Path(p).resolve() for p in (metadata, previous, assets, output)]
    if output.exists():
        raise ValueError('Choose a new immutable source-aware protocol directory')
    records = [r for r in _read(previous/'protocol-01/development.json')['records'] if r['dataset'] == 'MED']
    records += _read(previous/'protocol-01/holdout-answer-key.json')['records']
    annotations, aliases = defaultdict(list), []
    for row in _read(metadata/'mixparams-rows.json')['rows']:
        name = row['mix_name'].replace('MgC-', 'McG-')
        key = (_song_id(row['song_name']), name)
        annotations[key].append(row)
        if name != row['mix_name']:
            aliases.append({'original': row['mix_name'], 'canonical': name, 'song': row['song_name']})
    joined, missing, ambiguous = [], [], []
    for record in records:
        key = (_song_id(record['group']), record['name'])
        rows = annotations.get(key)
        if not rows:
            missing.append({'song': record['group'], 'mix': record['name']})
            continue
        if file_hash(Path(record['source_file'])) != record['source_sha256']:
            raise ValueError('Saved MED source changed')
        if len({r['track_name'] for r in rows}) != len(rows):
            ambiguous.append({'song': record['group'], 'mix': record['name'],
                'reason': 'Repeated track labels/path with differing pan; cannot resolve duplicate versus doubled channel from annotations'})
            continue
        joined.append(record | {'proxy_features': annotation_features(rows),
            'annotation_tracks': len(rows), 'annotation_key': list(key)})
    # The complete official test split is fixed before decoding or inspecting levels.
    archive = metadata/'MUSDB18-7-STEMS.zip'
    with zipfile.ZipFile(archive) as z:
        tracks = [{'member': i.filename, 'split': i.filename.split('/')[0], 'bytes': i.file_size,
            'crc': i.CRC, 'artist': Path(i.filename).name.split(' - ', 1)[0]}
            for i in z.infolist() if i.filename.endswith('.stem.mp4')]
    test_artists = {r['artist'] for r in tracks if r['split'] == 'test'}
    excluded = [r for r in tracks if r['split'] == 'train' and r['artist'] in test_artists]
    tracks = [r for r in tracks if r not in excluded]
    if sum(r['split'] == 'test' for r in tracks) != 50:
        raise ValueError('Official test split incomplete')
    features = _read(previous/'run-01/features.json')['rows']
    output.mkdir(parents=True)
    write_json(output/'records.json', {'records': joined})
    write_json(output/'joins.json', {'missing_saved_mixes': missing, 'normalized_aliases': aliases,
        'ambiguous_excluded': ambiguous,
        'joined_mixes': len(joined), 'joined_songs': sorted({r['group'] for r in joined}),
        'annotation_schema': list(ANNOTATION_FEATURES),
        'note': 'Track sets and session processing can differ. Proxy uncertainty remains even with complete metadata rows.'})
    write_json(output/'tracks.json', {'tracks': tracks, 'excluded_artist_overlap': excluded})
    write_json(output/'protocol.json', POLICY | {'source_measurement_policy': source_policy,
        'metadata_root': str(metadata), 'previous_root': str(previous), 'assets_root': str(assets),
        'archive': str(archive), 'archive_sha256': file_hash(archive),
        'annotation_sha256': file_hash(metadata/'mixparams-rows.json'),
        'global_features_sha256': file_hash(previous/'run-01/features.json'),
        'global_feature_count': 32, 'cached_feature_rows': len(features),
        'records_sha256': file_hash(output/'records.json'), 'tracks_sha256': file_hash(output/'tracks.json'),
        'code_sha256': {str(p): file_hash(p) for p in [Path(__file__),
            Path(__file__).parents[1]/'audio/source_evaluation.py', Path(__file__).with_name('preference_v2.py'),
            Path(__file__).with_name('mix_preference.py')]}})
    return {'directory': str(output), 'MED_joined_mixes': len(joined),
        'MED_songs': len({r['group'] for r in joined}),
        'source_train_songs': sum(r['split'] == 'train' for r in tracks), 'source_test_songs': 50,
        'training_artist_overlap_excluded': len(excluded)}


def _human_study(records, cached, progress):
    import numpy as np

    from .mix_preference import bootstrap, resource_checks
    from .preference_features import FEATURE_NAMES
    from .preference_v2 import evaluate, fit, pairs_for, predict, selection_regret

    if not isinstance(cached, dict):
        raise TypeError('Unexpected feature cache')
    rows = cached['rows']
    # Existing cache is keyed by immutable recording IDs.
    global_x = np.asarray([[rows[r['id']]['features'][f] for f in FEATURE_NAMES] for r in records])
    proxy = np.asarray([r['proxy_features'] for r in records])
    pairs = pairs_for(records, minimum=3)
    groups = sorted({p['group'] for p in pairs})
    if set(groups) != {r['group'] for r in records} or len(groups) < 5:
        raise ValueError('Insufficient paired song coverage')
    all_results = {}
    for name, x in [('global', global_x), ('proxy_only', proxy), ('combined', np.column_stack((global_x, proxy)))]:
        scores = np.full(len(records), np.nan)
        folds = []
        for test in groups:
            training = [g for g in groups if g != test]
            choices = []
            for config in CONFIGS:
                metrics = []
                for inner in training:
                    model = fit(x, records, pairs, [g for g in training if g != inner], config)
                    regret, loss = selection_regret(predict(model, x), pairs, inner)
                    metrics.append({'test': inner, 'regret': regret, 'loss': loss})
                choices.append({'config': config, 'mean_regret': float(np.mean([m['regret'] for m in metrics])),
                    'mean_loss': float(np.mean([m['loss'] for m in metrics])), 'folds': metrics})
            choice = min(choices, key=lambda c: (round(c['mean_regret'], 8), c['mean_loss']))
            model = fit(x, records, pairs, training, choice['config'])
            test_indices = [i for i, r in enumerate(records) if r['group'] == test]
            scores[test_indices] = predict(model, x[test_indices])
            folds.append({'test': test, 'test_ids': [records[i]['id'] for i in test_indices],
                'model': model, 'inner_search': choices})
            resource_checks()
            progress(f'MED {name}, held out: {test}')
        all_results[name] = {'scores': scores.tolist(), 'folds': folds,
            'all_pairs': evaluate(records, scores, minimum=3), 'higher_rater_pairs': evaluate(records, scores, minimum=8)}
    base = {r['group']: r['accuracy'] for r in all_results['global']['all_pairs']['contents']}
    added = {r['group']: r['accuracy'] for r in all_results['combined']['all_pairs']['contents']}
    differences = [added[g]-base[g] for g in groups if added[g] is not None and base[g] is not None]
    interval = bootstrap(differences)
    all_results['ablation'] = {'paired_macro_accuracy_improvement': float(np.mean(differences)),
        'conditional_song_bootstrap_CI': interval, 'songs': len(differences),
        'human_gate_pass': bool(interval[0] > 0 and
            all_results['combined']['all_pairs']['selection_failure_trials'] <
            all_results['global']['all_pairs']['selection_failure_trials']),
        'scope': 'Previously examined songs, held out of ALL fitting/tuning in their outer fold. Source balance features are annotation estimates, not audio measurements.'}
    return all_results


def _decode(archive, track, root, ffmpeg):
    """Decode four synchronous streams with one software ffmpeg process, no output device."""
    import subprocess

    import soundfile as sf

    from .mix_preference import resource_checks

    token = hashlib.sha256(track['member'].encode()).hexdigest()[:16]
    folder = root/token
    folder.mkdir(parents=True, exist_ok=False)
    source = folder/'source.stem.mp4'
    source.write_bytes(archive.read(track['member']))  # ZipFile checks CRC.
    command = [str(ffmpeg), '-nostdin', '-v', 'error', '-threads', '1', '-i', str(source)]
    roles = ('drums', 'bass', 'other', 'vocals')
    for stream, role in enumerate(roles, 1):
        command += ['-map', f'0:a:{stream}', '-threads', '1', '-ar', '16000', '-ac', '2',
            '-c:a', 'pcm_f32le', str(folder/(role+'.wav'))]
    result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError('Software source decoding failed: '+result.stderr[:1000])
    raw, provenance = {}, {}
    for role in roles:
        path = folder/(role+'.wav')
        audio, rate = sf.read(path, dtype='float32', always_2d=True)
        if rate != 16000 or not 6.5 <= len(audio)/rate <= 7.5:
            raise ValueError('Unexpected official sample excerpt duration/rate')
        raw[role] = audio
        provenance[role] = {'file': str(path), 'sha256': file_hash(path), 'frames': len(audio)}
    if len({a.shape for a in raw.values()}) != 1:
        raise ValueError('Source streams not synchronized')
    resource_checks()
    return raw, provenance


def _controls(protocol, tracks, output, ffmpeg, progress):
    import numpy as np

    from ..audio.source_evaluation import SourceBank, assess_ranges, fit_ranges
    from .mix_preference import bootstrap

    train, test, evidence = [], [], []
    output.mkdir()
    with zipfile.ZipFile(protocol['archive']) as archive:
        for track in sorted(tracks, key=lambda r: r['member']):
            raw, provenance = _decode(archive, track, output, ffmpeg)
            bank = SourceBank(raw, {r: r for r in raw}, 16000)
            mix = sum(raw.values())
            report = bank.measure(raw, master_pre=mix)
            record = {'track': track, 'report': report}
            evidence.append({'member': track['member'], 'sources': provenance})
            if track['split'] == 'train':
                train.append(record)
            else:
                # Retain only small features, not the audio for the entire corpus.
                variants = []
                rms = float(np.sqrt(np.mean(mix.astype('float64')**2)))
                for role in raw:
                    if report['roles'][role]['full']['status'] != 'measured':
                        continue
                    for gain in (-60., -12., -6., 6., 12.):
                        changed = {r: a*(10**(gain/20) if r == role else 1.) for r, a in raw.items()}
                        new_mix = sum(changed.values())
                        match = rms/max(float(np.sqrt(np.mean(new_mix.astype('float64')**2))), 1e-15)
                        changed = {r: a*match for r, a in changed.items()}
                        candidate = bank.measure(changed, master_pre=new_mix*match)
                        variants.append({'role': role, 'gain_db': gain, 'report': candidate,
                            'loudness_matching': 'Exact mixture RMS; not psychoacoustic LUFS',
                            'rms_error_db': float(20*np.log10(max(float(np.sqrt(np.mean((new_mix*match).astype('float64')**2))), 1e-15)/max(rms,1e-15)))})
                gain_report = bank.measure({r: a*10**(-12/20) for r, a in raw.items()})
                identity = bank.measure(raw)
                record |= {'variants': variants,
                    'gain_feature_error_db': float(np.max(np.abs(np.array(report['features'])-gain_report['features']))),
                    'identity_feature_error_db': float(np.max(np.abs(np.array(report['features'])-identity['features'])))}
                test.append(record)
            progress(f"Source features {track['split']}: {Path(track['member']).stem}")
    model = fit_ranges([r['report'] for r in train], training_groups=[r['track']['member'] for r in train])
    write_json(output/'model.json', model)
    # Model is saved before any official test labels/control scores are evaluated.
    for record in test:
        record['original'] = assess_ranges(record['report'], model)
        for variant in record['variants']:
            variant['assessment'] = assess_ranges(variant['report'], model)
            delta = variant['assessment']['anomaly_cost']-record['original']['anomaly_cost']
            variant['original_preference_credit'] = .5 if abs(delta) < 1e-9 else float(delta > 0)
            variant['affected_role_alert'] = any(a['feature'].startswith(variant['role']+'_') and
                a['direction'] == ('dominant' if variant['gain_db'] > 0 else 'buried')
                for a in variant['assessment']['alerts'])
    variants = [v for r in test for v in r['variants']]
    buckets = {}
    for gain in (-60., -12., -6., 6., 12.):
        group_means = [float(np.mean([v['original_preference_credit'] for v in r['variants'] if v['gain_db']==gain]))
            for r in test if any(v['gain_db']==gain for v in r['variants'])]
        selected = [v for v in variants if v['gain_db']==gain]
        buckets[str(gain)] = {'original_preference_fraction': float(np.mean(group_means)),
            'CI': bootstrap(group_means), 'controls': len(selected), 'songs': len(group_means),
            'affected_role_alert_recall': float(np.mean([v['affected_role_alert'] for v in selected]))}
    clean_alert_fraction = float(np.mean([bool(r['original']['alerts']) for r in test]))
    max_gain_error = max(r['gain_feature_error_db'] for r in test)
    max_identity_error = max(r['identity_feature_error_db'] for r in test)
    extreme = [v for v in variants if abs(v['gain_db']) >= 12]
    extreme_macro = float(np.mean([np.mean([v['original_preference_credit'] for v in r['variants'] if abs(v['gain_db'])>=12]) for r in test]))
    summary = {'training_songs': len(train), 'test_songs': len(test), 'test_controls': len(variants),
        'by_gain_db': buckets, 'clean_song_alert_fraction': clean_alert_fraction,
        'extreme_controls': len(extreme), 'extreme_original_preference_fraction': extreme_macro,
        'maximum_gain_feature_error_db': max_gain_error, 'maximum_identity_feature_error_db': max_identity_error,
        'maximum_RMS_match_error_db': max(abs(v['rms_error_db']) for v in variants),
        'gate_pass': bool(extreme_macro >= .8 and clean_alert_fraction <= .2 and max_gain_error <= .001 and max_identity_error <= .001),
        'scope': 'Deliberately changed source balance versus original in official test songs, not human-rated preference or professional quality.'}
    write_json(output/'training-features.json', {'records': train})
    write_json(output/'test-predictions.json', {'records': test})
    write_json(output/'evidence.json', {'decoded': evidence})
    write_json(output/'results.json', summary)
    return summary


def run(directory, output, ffmpeg, *, progress=print):
    """One offline worker, one logical CPU; never touches Ableton or devices."""
    from .master_benchmark_cpu import _deadline, configure_cpu, offline_connections

    configure_cpu()
    from .mix_preference import resource_checks

    directory, output, ffmpeg = [Path(p).resolve() for p in (directory, output, ffmpeg)]
    if output.exists():
        raise ValueError('Choose a new immutable source-aware run directory')
    protocol = _read(directory/'protocol.json')
    for path, expected in protocol['code_sha256'].items():
        if file_hash(Path(path)) != expected:
            raise ValueError('Frozen source-aware benchmark code changed')
    for path, expected in [(directory/'records.json',protocol['records_sha256']),
        (directory/'tracks.json',protocol['tracks_sha256']),
        (Path(protocol['archive']),protocol['archive_sha256']),
        (Path(protocol['metadata_root'])/'mixparams-rows.json',protocol['annotation_sha256']),
        (Path(protocol['previous_root'])/'run-01/features.json',protocol['global_features_sha256'])]:
        if file_hash(path) != expected:
            raise ValueError('Frozen source-aware input changed')
    output.mkdir(parents=True)
    timer = _deadline(2400, output, 'source-aware')
    start, cpu = time.monotonic(), time.process_time()
    with offline_connections() as attempts:
        records = _read(directory/'records.json')['records']
        for row in records:
            if file_hash(Path(row['source_file'])) != row['source_sha256']:
                raise ValueError('MED input source changed')
        human = _human_study(records, _read(Path(protocol['previous_root'])/'run-01/features.json'), progress)
        write_json(output/'human-results.json', human)
        controls = _controls(protocol, _read(directory/'tracks.json')['tracks'], output/'source-controls', ffmpeg, progress)
        resources = resource_checks()
        audit = {'protocol_sha256': file_hash(directory/'protocol.json'),
            'wall_seconds': round(time.monotonic()-start,2), 'CPU_seconds': round(time.process_time()-cpu,2),
            'resources': resources, 'network_attempts': len(attempts), 'audio_device_calls': 0,
            'DAW_calls': 0, 'desktop_calls': 0, 'ffmpeg_sha256': file_hash(ffmpeg),
            'auto_apply_allowed': False}
        write_json(output/'audit.json', audit)
        summary = {'human_ablation': human['ablation'], 'source_controls': controls, 'audit': audit}
        write_json(output/'results.json', summary)
        (output/'report.html').write_text(render(summary, human), encoding='utf-8')
    timer.cancel()
    return {'report': str(output/'report.html'), **summary}


def render(summary, human):
    def percent(value):
        return 'n/a' if value is None else f'{100*value:.1f}%'
    rows = ''
    for name in ('global','proxy_only','combined'):
        r = human[name]['all_pairs']
        rows += f'<tr><td>{name}</td><td>{percent(r["macro_accuracy"])}</td><td>{r["selection_failure_trials"]}/{r["independent_contents"]}</td><td>{r["maximum_paired_regret_mean"]:.3f}</td></tr>'
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Source-aware evaluator benchmark</title>
<style>body{background:#10191c;color:#e8ece9;font:17px system-ui;max-width:1050px;margin:40px auto;padding:24px;line-height:1.6}h1,h2{color:#e0bb73}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #425055;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#e0bb73}</style>
<h1>Source-aware evaluator: held-out song tests</h1><p>CPU only • Saved files only • No playback, Ableton control, desktop control or audio uploads.</p>
<h2>Human preference ablation</h2><p>All variants of each test song excluded from fitting, scaling and inner model selection. MED songs were examined in earlier studies; this is development cross-validation, not a new pristine final test. Source proxies use input LUFS + fader annotations, omitting actual processing, automation, buses and phase. They are NOT measured source contributions.</p>
<table><tr><th>Features</th><th>Macro decisive pair accuracy</th><th>Selections with a decisively better alternative</th><th>Mean maximum rating regret</th></tr>'''+rows+'''</table>
<p>Three-listener minimum after excluding authors; lower-powered songs are exploratory. Results with at least eight listeners and all fold models are in human-results.json. Song-bootstrap intervals are conditional on fitted CV predictions.</p>
<h2>Actual source audio</h2><p>Official MUSDB short sample: artist-disjoint training, all 50 official test songs. Vocals, drums, bass and OTHER are real stems. OTHER includes several instruments; guitar and keys are not separated in this dataset. Candidates are RMS matched; no listening is requested.</p>
<p>A broad training distribution flags unusual source balance. Controls change one source by a known gain. Recovering original balance is an engineering check, not evidence of improved human preference or church production quality. Per-song originals, mutations, source hashes and clean false alarms are saved.</p>
<h2>Outcome and measured resources</h2><pre>'''+html.escape(json.dumps(summary,indent=2))+'''</pre>
<h2>Use in the product</h2><p>Measured post-mixer source contributions can supply source-aware context at Master pre-effects. Raw activity stays fixed so buried or nearly absent performers remain detectable. Missing routing, returns or latency cause reconstruction rejection. Post-FX nodes before faders must include mixer gain/pan; nonlinear Master processing cannot be attributed exactly back to individual sources. High source correlation warns of ambiguous attribution; bleed remains a limitation.</p>
<p>Automatic application remains disabled. None of these results alone establish professional quality, and an annotation proxy model is not deployed against measured audio descriptors.</p></html>'''
