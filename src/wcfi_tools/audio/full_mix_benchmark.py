"""Saved-audio full-mix search and blinded challenges; no device or DAW access.

The deterministic selector is an experimental reference/style heuristic, never a
validated perceptual-quality model. Preparation freezes policy and opaque assets;
inference cannot read candidate labels or the challenge key. Unblinding reports
failures without adjusting the frozen policy to make these examples pass.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import random
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.optimize import nnls
from scipy.signal import butter, fftconvolve, resample_poly, sosfilt, welch

from .kit_study import low_shelf, scan_loudness
from .reference_study import describe_mix
from .role_processing import _bell, compress

RATE = 48000
SESSIONS = ('oct04', 'oct11', 'oct18')
ROLES = ('RED', 'KIT', 'BASS', 'EGUITAR', 'AC GUITAR', 'KEYS', 'YELLOW', 'GREEN')
POLICY = {
    'version': 1, 'kind': 'experimental_deterministic_reference_selector',
    'feature_scales': {'low_mid_over_presence_db': 4., 'sub_over_presence_db': 5.,
                       'high_over_presence_db': 5., 'crest_db': 4., 'side_energy_percent': 12.},
    'feature_weights': {'low_mid_over_presence_db': .3, 'sub_over_presence_db': .15,
                        'high_over_presence_db': .15, 'crest_db': .2, 'side_energy_percent': .2},
    'reference_range_slack': .25, 'tie_tolerance': .005, 'improvement_margin': .05,
    'balance_weight': .25, 'residual_weight': .15,
    'lead_over_band_range_db': [-10., 6.], 'kit_over_band_range_db': [-15., 3.],
    'max_mono_loss_db': 3., 'max_residual_fraction': .9,
    'max_plateau_fraction': .005, 'max_dropout_fraction': .08,
    'max_hum_prominence_db': 22.,
    'gate': 'All null, repeat, order, degradation and held-out-reference checks must pass in every session.',
    'perceptual_quality_validated': False, 'automatic_host_apply': False,
}
CANDIDATES = ('current', 'raw_faders', 'balanced_dry', 'shaped_dry', 'room', 'hall', 'hall_more', 'bus')
LABELS = {'current': 'Saved current Master', 'raw_faders': 'Raw full band · equal faders',
          'balanced_dry': 'Role balance · dry', 'shaped_dry': 'Role balance + EQ/dynamics · dry',
          'room': 'Shaped band + short room', 'hall': 'Shaped band + restrained hall',
          'hall_more': 'Shaped band + more hall', 'bus': 'Restrained hall + gentle bus compression',
          'identical': 'Identical room control', 'gain_only': 'Gain-only room control',
          'buried_lead': 'Lead reduced 18 dB', 'buried_kit': 'KIT reduced 18 dB',
          'dominant_acoustic': 'Acoustic raised 18 dB', 'lowpass': 'Full mix low-passed at 2 kHz',
          'hum': 'Added 60 Hz hum/harmonics', 'clipped': 'Severe hard clipping',
          'overwet': 'Excessive hall return', 'phase_width': 'Artificial width / phase fault'}
DEGRADATIONS = ('buried_lead', 'buried_kit', 'dominant_acoustic', 'lowpass', 'hum', 'clipped', 'overwet', 'phase_width')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_audio(path, *, frames=None):
    a, rate = sf.read(path, dtype='float32', always_2d=True, frames=frames or -1)
    if rate != RATE or a.shape[1] not in (1, 2) or not np.isfinite(a).all():
        raise ValueError('Use finite mono/stereo 48 kHz audio')
    if a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    return a


def power(a):
    return float(np.mean(np.asarray(a, dtype=float)**2))


def db_ratio(a, b):
    return float(10*np.log10(max(a, 1e-24)/max(b, 1e-24)))


def filter_periodic(a, sos):
    return sosfilt(sos, np.tile(a, (2, 1)), axis=0)[len(a):].astype(np.float32)


def place(a, pan):
    if not -.7 <= pan <= .7:
        raise ValueError('Bounded source placement required')
    # Preserve native stereo; only pan a dual-mono source.
    if np.max(np.abs(a[:, 0]-a[:, 1])) > 1e-7:
        return a.copy()
    angle = (pan+1)*math.pi/4
    return a*np.array([math.sqrt(2)*math.cos(angle), math.sqrt(2)*math.sin(angle)])


def room_ir(seconds, seed):
    """Deterministic stereo diffuse IR, filtered, with distinct early reflections.

    This is a local convolution ambience candidate, not a captured sanctuary or
    an emulation of Ableton Reverb. Each channel is energy-normalized together.
    """
    n = round(seconds*RATE)
    t = np.arange(n)/RATE
    rng = np.random.default_rng(seed)
    ir = rng.normal(size=(n, 2))*np.exp(-math.log(1000)*t[:, None]/seconds)
    ir[:round(.025*RATE)] = 0  # predelay; keep the direct signal separate
    for ch in (0, 1):
        for delay, gain in ((.031, .9), (.047, -.7), (.073, .5)):
            ir[round((delay+ch*.003)*RATE), ch] += gain*6
    ir = sosfilt(butter(2, [220, 6500], fs=RATE, btype='bandpass', output='sos'), ir, axis=0)
    return ir/math.sqrt(float(np.sum(ir**2)/2))


def ambience(stems, seconds, wet_db):
    # Musical sends, excluding BASS and uncertain backing microphones.
    send = .7*stems['RED']+.22*stems['KIT']+.3*stems['EGUITAR']+.3*stems['KEYS']+.2*stems['AC GUITAR']
    ir = room_ir(seconds, 714 if seconds < 1 else 815)
    # Periodic warmup is explicit: recordings are the existing bounded loop, not
    # evidence of a real room decay or of unrecorded audience ambience.
    repeated = np.tile(send, (2, 1))
    wet = np.column_stack([fftconvolve(repeated[:, c], ir[:, c])[:len(repeated)] for c in (0, 1)])[len(send):]
    direct = sum(stems.values())
    wet *= math.sqrt(power(direct)/max(power(wet), 1e-24))*10**(wet_db/20)
    return wet.astype(np.float32)


def source_versions(raw, levels):
    """One fixed role recipe shared across dates; never tuned on a test result."""
    targets = {'RED': -24, 'KIT': -27, 'BASS': -30, 'EGUITAR': -33, 'AC GUITAR': -36,
               'KEYS': -34, 'YELLOW': -34, 'GREEN': -34}
    pans = {'EGUITAR': -.5, 'AC GUITAR': .5, 'YELLOW': -.3, 'GREEN': .3}
    balanced, shaped, plan = {}, {}, {}
    for name in ROLES:
        a = raw[name]
        level = levels[name]['integrated_lufs']
        # Intent is authoritative; silence and uncertain backing bleed never
        # receive an unlimited normalization boost. Vocal activity is unproven.
        active = level is not None and level > -55
        if not active:
            balanced[name] = shaped[name] = np.zeros_like(a)
            plan[name] = {'gain_db': None, 'excluded': 'Below signal gate', 'pan': 0}
            continue
        gain = min(6 if name in ('YELLOW', 'GREEN') else 18, max(-36, targets[name]-level))
        balanced[name] = place(a, pans.get(name, 0))*10**(gain/20)
        b = a.copy()
        bands = []
        if name in ('RED', 'YELLOW', 'GREEN', 'AC GUITAR', 'EGUITAR', 'KEYS'):
            b = filter_periodic(b, butter(2, 65 if name != 'KEYS' else 40, fs=RATE, btype='highpass', output='sos'))
        if name == 'RED':
            b = filter_periodic(b, _bell(400, -2, 1));bands = [{'hz': 400, 'gain_db': -2, 'q': 1}]
        elif name == 'KIT':
            b = low_shelf(b, RATE, hz=120, gain_db=-1)
            b = filter_periodic(b, _bell(3000, 2, .7))
            b = filter_periodic(b, _bell(180, -2, 1))
            bands = [{'hz': 120, 'shelf_db': -1}, {'hz': 3000, 'gain_db': 2, 'q': .7}, {'hz': 180, 'gain_db': -2, 'q': 1}]
        elif name in ('AC GUITAR', 'KEYS'):
            b = filter_periodic(b, _bell(300, -2, .8));bands = [{'hz': 300, 'gain_db': -2, 'q': .8}]
        compression = {'enabled': False}
        if name in ('RED', 'KIT'):
            # Level-relative threshold, linked RMS detector; bounded 2 dB GR.
            threshold = 10*math.log10(max(power(b), 1e-24))+3
            b, compression = compress(b, {'enabled': True, 'ratio': 1.5 if name == 'KIT' else 2,
                'threshold_dbfs': min(-1, max(-90, threshold)), 'knee_db': 3, 'attack_ms': 15,
                'release_ms': 100, 'maximum_reduction_db': 2, 'makeup_gain_db': 0})
        shaped[name] = place(b, pans.get(name, 0))*10**(gain/20)
        plan[name] = {'gain_db': gain, 'pan': pans.get(name, 0), 'eq': bands, 'compression': compression,
                      'raw_full_scale_fraction': float(np.mean(np.abs(a) >= .99999))}
    return balanced, shaped, plan


def variants(raw, current, levels):
    balanced, shaped, plan = source_versions(raw, levels)
    dry = sum(shaped.values())
    short = ambience(shaped, .7, -16)
    hall = ambience(shaped, 1.4, -13)
    anchor = dry+short
    bus, bus_stats = compress(dry+hall, {'enabled': True, 'ratio': 1.5,
        'threshold_dbfs': min(-1, 10*math.log10(max(power(dry+hall), 1e-24))+2),
        'knee_db': 3, 'attack_ms': 20, 'release_ms': 150,
        'maximum_reduction_db': 1.5, 'makeup_gain_db': 0})
    t = np.arange(len(dry))/RATE
    hum = sum(np.sin(2*math.pi*f*t) for f in (60, 120, 180))[:, None]
    hum *= math.sqrt(power(anchor)/max(power(hum), 1e-24))*.65
    threshold = math.sqrt(power(anchor))*.55
    return {
        'current': current, 'raw_faders': sum(raw.values())*10**(-6/20),
        'balanced_dry': sum(balanced.values()), 'shaped_dry': dry,
        'room': anchor, 'hall': dry+hall, 'hall_more': dry+hall*10**(4/20), 'bus': bus,
        'identical': anchor.copy(), 'gain_only': anchor*.5,
        'buried_lead': anchor-shaped['RED']*(1-10**(-18/20)),
        'buried_kit': anchor-shaped['KIT']*(1-10**(-18/20)),
        'dominant_acoustic': anchor+shaped['AC GUITAR']*(10**(18/20)-1),
        'lowpass': filter_periodic(anchor, butter(6, 2000, fs=RATE, output='sos')),
        'hum': anchor+hum, 'clipped': np.clip(anchor, -threshold, threshold),
        'overwet': dry+hall*10**(19/20),
        'phase_width': np.column_stack((anchor[:, 0], -anchor[:, 0]*.95)),
    }, plan | {'bus': bus_stats}


def match_audio(a, output, ffmpeg):
    # First put every variant on a common RMS scale. Exact 0.5 gain controls then
    # converge to identical float samples before perceptual loudness matching.
    a = np.asarray(a, dtype=np.float64)
    a = a/math.sqrt(max(power(a), 1e-24))*.05
    sf.write(output, a, RATE, subtype='FLOAT')
    observed = scan_loudness(output, ffmpeg)
    target = -24.
    gain = target-observed['integrated_lufs']
    if observed['true_peak_dbtp']+gain > -3:
        raise ValueError('Fixed benchmark loudness exceeds the -3 dBTP ceiling')
    for _ in range(3):
        sf.write(output, a*10**(gain/20), RATE, subtype='FLOAT')
        checked = scan_loudness(output, ffmpeg)
        if abs(checked['integrated_lufs']-target) <= .11:
            break
        gain += target-checked['integrated_lufs']
    if abs(checked['integrated_lufs']-target) > .15 or checked['true_peak_dbtp'] > -2.85:
        raise ValueError('Benchmark loudness verification failed')
    return {k: checked[k] for k in ('integrated_lufs', 'true_peak_dbtp')}


def prepare(profile, output, *, ffmpeg, seed=20261022, progress=None):
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new immutable preparation directory')
    profile_path = Path(profile).resolve();profile = load(profile_path)
    if tuple(s['id'] for s in profile['sessions']) != SESSIONS or set(profile['references']) != {'bethel', 'maverick'}:
        raise ValueError('Use all three sessions and both references')
    output.mkdir(parents=True);assets = output/'assets';assets.mkdir()
    save(output/'policy.json', POLICY)  # freeze before rendering or scoring
    public = {'kind': 'wcfi_full_mix_blind_inputs', 'schema_version': 1, 'items': [],
              'sessions': [], 'references': {}, 'policy_sha256': digest(output/'policy.json'),
              'profile_sha256': digest(profile_path)}
    key = {'kind': 'wcfi_full_mix_private_key', 'items': {}, 'plans': {}, 'sources': profile}
    rng = random.Random(seed)
    for sid in SESSIONS:
        s = next(s for s in profile['sessions'] if s['id'] == sid)
        root = Path(s['manifest']).resolve().parent;manifest = load(root/'manifest.json')
        if digest(root/'manifest.json') != s['manifest_sha256']:
            raise ValueError('Source manifest changed')
        raw, levels, source_entries = {}, {}, {}
        n = 12*RATE
        for role in ROLES:
            entry = next(e for e in manifest['sources'] if e['track_name'] == role)
            path = root/entry['file']
            if path.resolve().parent != root.resolve() or digest(path) != entry['sha256']:
                raise ValueError('Use checksum-verified bounded source excerpts inside their take')
            a = read_audio(path, frames=n)
            if a.shape != (n, 2):
                raise ValueError('Every source must share the twelve-second passage')
            raw[role] = a
            # Loudness scanner rejects silence; gate its signal level first.
            filename = f'{sid}-source-{role.replace(" ", "_")}.wav'
            sf.write(assets/filename, a, RATE, subtype='FLOAT')
            levels[role] = scan_loudness(assets/filename, ffmpeg) if power(a) > 1e-10 else {'integrated_lufs': None}
            source_entries[role] = {'file': filename, 'sha256': digest(assets/filename)}
        path = Path(s['current']['path'])
        if digest(path) != s['current']['sha256']:
            raise ValueError('Saved Master capture changed')
        current = read_audio(path, frames=n)*10**(s['current']['gain_db']/20)
        rendered, plan = variants(raw, current, levels)
        key['plans'][sid] = plan
        public['sessions'].append({'id': sid, 'sources': source_entries})
        for name, a in rendered.items():
            identifier = f'{rng.getrandbits(96):024x}'
            filename = f'MIX-fmb_{identifier}.wav'
            measured = match_audio(a, assets/filename, ffmpeg)
            public['items'].append({'id': identifier, 'session': sid, 'file': filename,
                                    'sha256': digest(assets/filename), 'matching': measured})
            key['items'][identifier] = {'variant': name, 'label': LABELS[name], 'candidate': name in CANDIDATES}
        if progress:
            progress(f'Prepared {sid}: {len(rendered)} full-mix versions')
    for name, source in profile['references'].items():
        path = Path(source['path'])
        if digest(path) != source['sha256']:
            raise ValueError('Saved reference changed')
        filename = f'MIX-fmb_{name}.wav'
        measured = match_audio(read_audio(path, frames=12*RATE), assets/filename, ffmpeg)
        public['references'][name] = {'file': filename, 'sha256': digest(assets/filename), 'matching': measured}
    rng.shuffle(public['items'])
    save(output/'blind-inputs.json', public);save(output/'private-key.json', key)
    freeze = {'policy_sha256': digest(output/'policy.json'), 'inputs_sha256': digest(output/'blind-inputs.json'),
              'key_sha256': digest(output/'private-key.json'), 'candidate_labels_hidden_during_run': True,
              'professional_quality_judgment_validated': False}
    save(output/'freeze.json', freeze)
    return {'versions': len(public['items']), 'references': 2, **freeze}


def checked_asset(root, entry):
    path = (Path(root)/'assets'/entry['file']).resolve()
    if path.parent != (Path(root)/'assets').resolve() or digest(path) != entry['sha256']:
        raise ValueError('Benchmark asset escaped its root or changed checksum')
    return read_audio(path)


class SourceProjection:
    """Nonnegative source fit, a balance/residual proxy under bleed and effects.

    This is not source separation, vocal intelligibility, or a reverb estimate.
    A residual can include EQ, dynamics, distortion, timing mismatch and ambience.
    """
    def __init__(self, raw):
        self.sos = butter(2, [150, 3500], fs=8000, btype='bandpass', output='sos')
        self.names = []
        columns = []
        for name, a in raw.items():
            a = self.convert(a)
            rms = math.sqrt(power(a))
            if rms > 1e-6:
                self.names.append(name);columns.append(a.ravel()/rms)
        self.x = np.column_stack(columns)

    def convert(self, a):
        return sosfilt(self.sos, resample_poly(a, 1, 6, axis=0), axis=0)

    def evaluate(self, a):
        y = self.convert(a).ravel()
        coefficient, residual = nnls(self.x, y)
        energies = dict(zip(self.names, (coefficient**2).tolist(), strict=True))
        def relative(name):
            floor = max(sum(energies.values())*1e-12, 1e-30)
            return float(10*np.log10(max(energies.get(name, 0), floor)/max(sum(v for k, v in energies.items() if k != name), floor)))
        return {'lead_over_band_db': relative('RED'), 'kit_over_band_db': relative('KIT'),
                'residual_fraction': float(residual**2/max(float(y@y), 1e-24))}


def observations(a):
    m = describe_mix(a, RATE)
    # Flat plateaus survive whole-mix gain matching. Reject severe hard clipping
    # without relying on full-scale amplitude in these quiet audition copies.
    scale = np.max(np.abs(a))
    plateau = (np.abs(np.diff(a, axis=0)) < max(scale*1e-7, 1e-12)) & (np.abs(a[:-1]) > scale*.6)
    blocks = a[:len(a)//480*480].reshape(-1, 480, 2)
    energy = np.mean(blocks.astype(float)**2, axis=(1, 2))
    dropout = float(np.mean(energy < max(float(np.median(energy))*1e-5, 1e-24)))
    f, p = welch(a, fs=RATE, nperseg=48000, noverlap=24000, axis=0);p = p.mean(axis=1)
    prominences = []
    for base in (50, 60):
        scores = []
        for hz in (base, base*2, base*3):
            line = p[(f >= hz-1) & (f <= hz+1)].sum()/3
            local = p[(f >= hz-12) & (f <= hz+12) & (np.abs(f-hz) > 3)]
            scores.append(db_ratio(float(line), float(np.median(local))))
        prominences.append(float(np.median(scores)))
    return m | {'plateau_fraction': float(plateau.mean()), 'dropout_fraction': dropout,
                'hum_prominence_db': max(prominences)}


def outside(value, low, high):
    return max(0., low-value, value-high)


def score(m, projection, references, policy):
    distances = {}
    for name, reference in references.items():
        distances[name] = sum(policy['feature_weights'][k]*abs(m[k]-reference[k])/scale
                              for k, scale in policy['feature_scales'].items())
    # Match a range, allowing differences shared by neither reference. This still
    # measures style proximity only; it cannot certify good musical production.
    distance = 0.
    for k, scale in policy['feature_scales'].items():
        values = [r[k] for r in references.values()]
        slack = policy['reference_range_slack']*scale
        distance += policy['feature_weights'][k]*outside(m[k], min(values)-slack, max(values)+slack)/scale
    balance = (outside(projection['lead_over_band_db'], *policy['lead_over_band_range_db'])
               + outside(projection['kit_over_band_db'], *policy['kit_over_band_range_db']))/12
    failures = []
    for value, maximum, label in ((-m['mono_energy_change_db'], policy['max_mono_loss_db'], 'mono cancellation'),
            (m['plateau_fraction'], policy['max_plateau_fraction'], 'hard-clipping plateaus'),
            (m['dropout_fraction'], policy['max_dropout_fraction'], 'signal dropouts'),
            (m['hum_prominence_db'], policy['max_hum_prominence_db'], 'tonal hum'),
            (projection['residual_fraction'], policy['max_residual_fraction'], 'unexplained source residual')):
        if value > maximum:
            failures.append(label)
    return {'cost': float(distance+policy['balance_weight']*balance+policy['residual_weight']*projection['residual_fraction']),
            'style_distance': float(distance), 'reference_distances': distances,
            'balance_penalty': float(balance), 'guardrail_failures': failures,
            'eligible': not failures, 'projection': projection}


def rank(rows):
    return sorted(rows, key=lambda r: (not r['evaluation']['eligible'], r['evaluation']['cost'], r['id']))


def run(directory, output, *, progress=None):
    """Read only opaque inputs and frozen policy. Never read the answer key."""
    start = time.monotonic();directory = Path(directory);output = Path(output)
    if output.exists():
        raise ValueError('Use a new inference run directory')
    public = load(directory/'blind-inputs.json');policy = load(directory/'policy.json')
    if policy != POLICY or digest(directory/'policy.json') != public['policy_sha256']:
        raise ValueError('Frozen selector policy changed; create a new benchmark version')
    output.mkdir(parents=True)
    references = {k: observations(checked_asset(directory, e)) for k, e in public['references'].items()}
    rows, repeats = [], []
    for session in public['sessions']:
        sid = session['id']
        projection = SourceProjection({k: checked_asset(directory, e) for k, e in session['sources'].items()})
        items = [i for i in public['items'] if i['session'] == sid]
        for index, entry in enumerate(items):
            a = checked_asset(directory, entry);m = observations(a);p = projection.evaluate(a)
            rows.append({'id': entry['id'], 'session': sid, 'sha256': entry['sha256'], 'metrics': m,
                         'evaluation': score(m, p, references, policy)})
            if index == 0:
                repeat = score(observations(a), projection.evaluate(a), references, policy)
                repeats.append({'session': sid, 'id': entry['id'], 'exact_repeat': repeat == rows[-1]['evaluation']})
        if progress:
            progress(f'Scored {sid}: {len(items)} opaque inputs on CPU')
        save(output/'progress.json', {'completed_sessions': len(repeats), 'items': len(rows)})
    result = {'kind': 'wcfi_full_mix_blind_run', 'policy_sha256': digest(directory/'policy.json'),
              'inputs_sha256': digest(directory/'blind-inputs.json'), 'rows': rows, 'references': references,
              'repeats': repeats, 'order_invariance': [r['id'] for r in rank(rows)] == [r['id'] for r in rank(rows[::-1])],
              'runtime': {'wall_seconds': time.monotonic()-start, 'gpu_used': False, 'audio_models_run': 0,
                          'audio_uploads': 0, 'device_or_daw_connections': 0, 'cpu_worker_limit': 1},
              'private_key_read': False}
    save(output/'blind-results.json', result)
    return {k: v for k, v in result.items() if k not in ('rows', 'references')}


def summarize(directory, run_directory):
    directory = Path(directory);run_directory = Path(run_directory)
    freeze = load(directory/'freeze.json');result = load(run_directory/'blind-results.json')
    for key, filename in (('policy_sha256', 'policy.json'), ('inputs_sha256', 'blind-inputs.json'), ('key_sha256', 'private-key.json')):
        if freeze[key] != digest(directory/filename) or key in result and result[key] != freeze[key]:
            raise ValueError('Prepared benchmark or completed results changed')
    policy = load(directory/'policy.json');key = load(directory/'private-key.json')
    public = load(directory/'blind-inputs.json');checks, sessions, audio = [], [], {}
    entries = {i['id']: i for i in public['items']}
    for row in result['rows']:
        checked_asset(directory, entries[row['id']])
        if row['sha256'] != entries[row['id']]['sha256']:
            raise ValueError('Run evaluated different audio')
    for sid in SESSIONS:
        rows = [r | key['items'][r['id']] for r in result['rows'] if r['session'] == sid]
        by_name = {r['variant']: r for r in rows};anchor = by_name['room']
        for name in ('identical', 'gain_only'):
            r = by_name[name]
            delta = abs(r['evaluation']['cost']-anchor['evaluation']['cost'])
            checks.append({'session': sid, 'test': name, 'passed': delta <= policy['tie_tolerance']
                           and r['evaluation']['guardrail_failures'] == anchor['evaluation']['guardrail_failures'], 'delta': delta})
        for name in DEGRADATIONS:
            r = by_name[name]
            margin = r['evaluation']['cost']-anchor['evaluation']['cost']
            passed = (anchor['evaluation']['eligible'] and (not r['evaluation']['eligible'] or margin >= policy['improvement_margin']))
            checks.append({'session': sid, 'test': name, 'passed': passed, 'delta': margin})
        candidates = [r for r in rows if r['candidate']]
        ordered = rank(candidates);winner = ordered[0];current = by_name['current']
        # Single-reference choices test sensitivity; neither song is a same-stem
        # ground-truth mix, and neither is used to tune policy weights.
        single_winners = {}
        for ref in ('bethel', 'maverick'):
            rescored = [r | {'evaluation': score(r['metrics'], r['evaluation']['projection'],
                         {ref: result['references'][ref]}, policy)} for r in candidates]
            single_winners[ref] = rank(rescored)[0]['variant']
        stable = len(set(single_winners.values())) == 1 and next(iter(single_winners.values())) == winner['variant']
        checks.append({'session': sid, 'test': 'held_out_reference_agreement', 'passed': stable})
        improvement = current['evaluation']['cost']-winner['evaluation']['cost']
        for r in rows:
            audio[f'fmb_{r["id"]}'] = entries[r['id']]['file']
        sessions.append({'id': sid, 'source': next(s for s in key['sources']['sessions'] if s['id'] == sid),
            'ranked_candidates': ordered, 'controls': [r for r in rows if not r['candidate']],
            'best_candidate': winner['variant'], 'improvement_over_current': improvement,
            'single_reference_winners': single_winners, 'plan': key['plans'][sid]})
    checks.extend({'session': r['session'], 'test': 'repeat', 'passed': r['exact_repeat']} for r in result['repeats'])
    checks.append({'session': 'all', 'test': 'order_invariance', 'passed': result['order_invariance']})
    passed = all(c['passed'] for c in checks)
    for session in sessions:
        session['automatic_recommendation'] = ('experimental_style_candidate' if passed
            and session['improvement_over_current'] >= policy['improvement_margin'] else 'withheld')
    for ref, e in public['references'].items():
        audio[f'fmb_{ref}'] = e['file']
    report = {'kind': 'wcfi_full_mix_benchmark', 'title': 'Full-mix benchmark · three sessions + ambience',
        'context': {'start_seconds': 0}, 'duration_seconds': 12, 'sessions': sessions, 'checks': checks,
        'control_gate_passed': passed, 'professional_quality_judgment_validated': False,
        'processing_applied': False, 'audio_models_run': 0, 'runtime': result['runtime'],
        'policy': policy, 'freeze': freeze, 'audio': audio,
        'references': key['sources']['references'], 'reference_metrics': result['references'],
        'limits': ['A lower cost is closer to the chosen feature ranges, not proof of better production.',
            'Saved current Master is an earlier captured chain, not the historical broadcast or the latest October 11 seven-source local preview. All new candidates are local stem renders, not native Ableton renders.',
            'All recipes, weights, controls and failure criteria were frozen before scoring; no tuning after unblinding.',
            'The run reads opaque filenames, raw source context and reference audio; it cannot read candidate labels or the private key.',
            'Balance and source residual estimates are nonnegative projection proxies, unreliable under correlated bleed or unmodeled effects.',
            'Ambience is filtered synthetic stereo convolution with periodic warmup, not an audience/room recording or native Ableton render.',
            'Known-defect controls test direction and invariance. They do not establish preference for subtle changes.',
            'All three dates are development material already examined previously, not an independent held-out church dataset.',
            'The reference sensitivity check holds out a reference, not a recording session. There is no fitted model or session-training split.',
            'No professional same-stem target or independent expert preference labels are available. Professional-quality validation remains false even if controls pass.',
            'No host changes, transport commands, playback, recording, GPU, new downloads, or cloud uploads.']}
    save(run_directory/'results.json', report)
    write_html(report, run_directory/'report.html')
    return report


def write_html(report, path):
    esc = lambda v: html.escape(str(v))
    title = esc(report.get('title', 'Full-mix benchmark'))
    gate = 'Source-balance gate' if report['kind'] == 'wcfi_source_balance_benchmark' else 'Control gate'
    rows = ''.join(f'<tr><td>{esc(c["session"])}</td><td>{esc(c["test"])}</td><td>{"PASS" if c["passed"] else "FAIL"}</td><td>{esc(round(c["delta"], 5)) if "delta" in c else ""}</td></tr>' for c in report['checks'])
    sessions = ''.join(f'<h2>{esc(s["id"])}</h2><p>Best feature candidate: {esc(s["best_candidate"])}. Recommendation: {esc(s["automatic_recommendation"])}</p><pre>{esc(json.dumps([{ "variant": r["variant"], **r["evaluation"]} for r in s["ranked_candidates"]], indent=2))}</pre>' for s in report['sessions'])
    Path(path).write_text('<!doctype html><meta charset="utf-8"><title>WCFI full-mix benchmark</title><style>body{font:16px system-ui;background:#121919;color:#eee;max-width:1100px;margin:40px auto;padding:20px}td,th{padding:8px;border-bottom:1px solid #456}pre{white-space:pre-wrap}table{width:100%}</style>'
        f'<h1>{title}</h1><p>{gate}: {"PASS" if report["control_gate_passed"] else "FAIL"}. Professional mixing judgment: unvalidated. No processing applied.</p><table><tr><th>Session</th><th>Challenge</th><th>Result</th><th>Cost difference</th></tr>{rows}</table>{sessions}<h2>Limits</h2>'
        +''.join(f'<p>{esc(v)}</p>' for v in report['limits']), encoding='utf-8')


def export_package(directory, run_directory, output):
    import shutil

    directory = Path(directory);output = Path(output)
    report = summarize(directory, run_directory)
    if output.exists():
        raise ValueError('Use a new package directory')
    output.mkdir(parents=True)
    files = {}
    for filename in report['audio'].values():
        shutil.copyfile(directory/'assets'/filename, output/filename)
        files[filename] = digest(output/filename)
    package = {'kind': 'wcfi_full_mix_benchmark_package', 'schema_version': 1, 'reports': {'MIX': report}, 'files': files}
    save(output/'study.json', package)
    return package


def validate_package(folder, package, raw):
    folder = Path(folder).resolve();r = package['reports']['MIX']
    if (package.get('kind') != 'wcfi_full_mix_benchmark_package' or package.get('schema_version') != 1
            or set(package['reports']) != {'MIX'} or r.get('kind') != 'wcfi_full_mix_benchmark'
            or r.get('professional_quality_judgment_validated') is not False or r.get('processing_applied') is not False
            or r.get('audio_models_run') != 0 or r.get('policy') != POLICY or r.get('duration_seconds') != 12
            or r.get('runtime', {}).get('gpu_used') is not False
            or r.get('runtime', {}).get('audio_uploads') != 0
            or r.get('context') != {'start_seconds': 0}
            or not isinstance(r.get('title'), str) or not 1 <= len(r['title']) <= 100
            or [s['id'] for s in r['sessions']] != list(SESSIONS)
            or r.get('control_gate_passed') is not all(c['passed'] for c in r['checks'])
            or len(r['checks']) != 37 or len(r['audio']) != 56
            or set(package['files']) != set(r['audio'].values())):
        raise ValueError('Invalid benchmark scope, frozen policy, gates or assets')
    for k, filename in r['audio'].items():
        if not re.fullmatch(r'fmb_([0-9a-f]{24}|bethel|maverick)', k) or filename != f'MIX-{k}.wav':
            raise ValueError('Invalid benchmark audio key')
        entry = {'file': filename, 'sha256': package['files'][filename]}
        path = (folder/filename).resolve()
        if path.parent != folder or digest(path) != entry['sha256']:
            raise ValueError('Benchmark package asset changed')
        a = read_audio(path)
        if a.shape != (12*RATE, 2) or np.max(np.abs(a)) >= .999:
            raise ValueError('Invalid benchmark audio format or headroom')
    reference_metrics = {name: observations(read_audio(folder/f'MIX-fmb_{name}.wav')) for name in ('bethel', 'maverick')}
    if reference_metrics != r.get('reference_metrics'):
        raise ValueError('Published reference observations changed')
    for s in r['sessions']:
        if (len(s['ranked_candidates']) != 8 or len(s['controls']) != 10
                or {v['variant'] for v in s['ranked_candidates']} != set(CANDIDATES)
                or {v['variant'] for v in s['controls']} != set(DEGRADATIONS) | {'identical', 'gain_only'}
                or s['best_candidate'] != s['ranked_candidates'][0]['variant']
                or rank(s['ranked_candidates']) != s['ranked_candidates']
                or s['automatic_recommendation'] not in ('withheld', 'experimental_style_candidate')
                or not r['control_gate_passed'] and s['automatic_recommendation'] != 'withheld'):
            raise ValueError('Invalid candidate ranking or recommendation')
        for v in s['ranked_candidates']+s['controls']:
            path = folder/f'MIX-fmb_{v["id"]}.wav'
            if digest(path) != v['sha256'] or not math.isfinite(v['evaluation']['cost']):
                raise ValueError('Run is not bound to published audio')
            observed = observations(read_audio(path))
            if observed != v['metrics'] or score(observed, v['evaluation']['projection'], reference_metrics, POLICY) != v['evaluation']:
                raise ValueError('Ranking is not bound to saved audio observations and frozen policy')
    return package, hashlib.sha256(raw).hexdigest()
