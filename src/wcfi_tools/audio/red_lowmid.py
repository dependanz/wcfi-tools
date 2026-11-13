"""RED low-mid tone trial with a vocal-level control and fixed KIT/BASS bed."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import find_peaks, sosfilt, tf2sos, welch

from .kit_bass_study import band_frames, db_ratio, write_mix_auditions
from .kit_compression import FIXED_EQ
from .kit_diagnostic import audio_array
from .kit_study import scan_loudness

DESCRIPTIONS = [
    {'id': 'current', 'label': 'Current RED'},
    {'id': 'eq', 'label': 'RED 400 Hz cut'},
    {'id': 'level', 'label': 'RED level-only control'},
]
VARIANTS = tuple(v['id'] for v in DESCRIPTIONS)
AUDIO_KEYS = {f'redlowmid_mix_{v}{suffix}' for v in VARIANTS for suffix in ('', '_matched')}
FILTER = {'hz': 400, 'gain_db': -2, 'q': 1, 'type': 'bell', 'implementation': 'local stereo minimum-phase RBJ, periodic warmup'}
KIT_CHOICE = {'compressor_threshold_db': -16, 'ratio': 2, 'attack_ms': 15, 'release_ms': 100,
              'body_bell': {'hz': 180, 'gain_db': -2, 'q': 1}, 'fixed_eq': FIXED_EQ, 'bass_unchanged': True}


def lowmid_cut(audio):
    a = audio_array(audio);omega = 2*math.pi*400/48000;alpha = math.sin(omega)/2;g = 10**(-2/40)
    sos = tf2sos([1+alpha*g, -2*math.cos(omega), 1-alpha*g],
                [1+alpha/g, -2*math.cos(omega), 1-alpha/g])
    return sosfilt(sos, np.tile(a, (2, 1)), axis=0)[len(a):]


def tone_metrics(audio):
    a = audio_array(audio)
    low = float(np.mean(band_frames(a, 250, 500)));presence = float(np.mean(band_frames(a, 1000, 4000)))
    return {'lowmid_power_dbfs': db_ratio(low, 1), 'presence_power_dbfs': db_ratio(presence, 1),
            'lowmid_over_presence_db': db_ratio(low, presence)}


def inspect_red(audio):
    a = audio_array(audio)
    def shape(x):
        f, p = welch(x, fs=48000, nperseg=8192, noverlap=4096, axis=0);p = p.mean(axis=1)
        total = max(float(p[f < 20000].sum()), 1e-24)
        peaks, _ = find_peaks(10*np.log10(np.maximum(p, 1e-24)), prominence=6)
        peaks = sorted((i for i in peaks if 100 <= f[i] <= 1000), key=lambda i: p[i], reverse=True)[:4]
        return {'band_power_percent': {f'{lo}-{hi}': float(p[(f >= lo) & (f < hi)].sum()/total*100)
                    for lo, hi in ((30, 100), (100, 250), (250, 500), (500, 1000), (1000, 4000), (4000, 8000))},
                'dominant_peaks_hz': [float(f[i]) for i in peaks]}
    return {'whole': shape(a), 'two_second_windows': [{'start_seconds': i/48000, **shape(a[i:i+96000])}
            for i in range(0, len(a)-96000+1, 96000)],
        'interpretation': 'Moving peaks are consistent with musical fundamentals/harmonics; no pitch tracking, voice isolation or fixed room resonance has been established.'}


def red_versions(red, mix, *, level_gain_db):
    red, mix = audio_array(red), audio_array(mix)
    if red.shape != mix.shape or not math.isfinite(level_gain_db) or not -3 <= level_gain_db <= 0:
        raise ValueError('Use aligned contributions and a bounded measured RED control gain')
    reds = {'current': red, 'eq': lowmid_cut(red), 'level': red*10**(level_gain_db/20)}
    return reds, {v: mix-red+a for v, a in reds.items()}


def build_package(red, mix, output, *, context, ffmpeg):
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new RED comparison directory')
    red, mix = audio_array(red), audio_array(mix);output.mkdir(parents=True)
    levels = {};cut = lowmid_cut(red)
    def scan(name, a):
        p = output/f'.red-{name}.wav'
        try:
            sf.write(p, a, 48000, subtype='FLOAT')
            return scan_loudness(p, ffmpeg)
        finally:
            p.unlink(missing_ok=True)
    levels['current'] = scan('current', red);levels['eq'] = scan('eq', cut)
    gain = levels['eq']['integrated_lufs']-levels['current']['integrated_lufs']
    for _ in range(4):
        levels['level'] = scan('level', red*10**(gain/20))
        error = levels['eq']['integrated_lufs']-levels['level']['integrated_lufs']
        if abs(error) <= .11:
            break
        gain += error
    if abs(error) > .15:
        raise ValueError('RED level control does not match the EQ version')
    reds, mixes = red_versions(red, mix, level_gain_db=gain)
    audio, stats, matching = write_mix_auditions(mixes, output, ffmpeg=ffmpeg, track='RED', prefix='redlowmid')
    observed = {'red': {v: tone_metrics(a) for v, a in reds.items()},
                'mix': {v: tone_metrics(a) for v, a in mixes.items()}}
    r = {'kind': 'wcfi_red_lowmid_study', 'title': 'October 11 · RED low-mid tone test',
        'context': context, 'duration_seconds': len(red)/48000, 'variants': copy.deepcopy(DESCRIPTIONS),
        'filter': copy.deepcopy(FILTER), 'kit_choice': copy.deepcopy(KIT_CHOICE),
        'inspection': inspect_red(red), 'observed': observed, 'red_loudness': levels, 'level_control_gain_db': gain,
        'measurements': stats, 'matching': matching, 'audio': audio,
        'processing_applied': False, 'preference_established': False, 'audio_models_run': 0,
        'origin': 'saved_native_capture_plus_local_RED_EQ_and_KIT_body_preview',
        'notes': [
            'Working KIT choice: native gentle compressor capture followed by the fixed −1/+2 EQ and −2 dB body bell at 180 Hz, Q 1. BASS is unchanged. This is a provisional engineering choice, not a validated listening preference. No host settings are applied.',
            'RED EQ candidate: one −2 dB bell at 400 Hz, Q 1, using local minimum-phase DSP and periodic warmup. It acts on the entire mic signal including bleed; it is not a native Pro-Q render.',
            'A level-only control attenuates unfiltered RED until its integrated loudness matches the EQ-treated RED within 0.15 LU before mixing. Compare EQ against this control to separate a tonal change from lowering the vocal. KIT and BASS are identical before whole-mix matching.',
            'Each saved complete mix is normalized by one scalar to the same integrated LUFS. No source is separately restored to its old loudness. Matched-mode KIT/BASS scale with the whole mix; Original levels keeps them identical.',
            'RED dominates separate-track 250–500 Hz power here, but vocal fundamentals/harmonics and bleed all contribute. Band energy alone does not establish excessive proximity effect, a room resonance, poor intelligibility or a universal vocal target.',
            'Two-second spectra show moving peaks rather than one identified fixed unwanted resonance. A broad, small tonal trial is used instead of an automatic deep notch or high-pass cut through the singer’s body.',
            'Input sample health is measured on the original saved pre-effects node, not on the attenuated mix contribution. Absence of full-scale indicators does not prove that upstream distortion is absent.',
            'The reference context is the RED/KIT/BASS foundation only, not the complete band or encoded stream. No Play/Record, desktop control, audio model, GPU or upload is used.',
        ], 'sources': [{'title': 'iZotope · Vocal EQ and preserving body', 'url': 'https://www.izotope.com/community/blog/how-to-eq-vocals'}]}
    p = {'kind': 'wcfi_red_lowmid_package', 'schema_version': 1, 'reports': {'RED': r},
        'files': {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in audio.values()}}
    (output/'study.json').write_text(json.dumps(p, allow_nan=False), encoding='utf-8')
    return p


def validate_package(folder, p, raw):
    folder = Path(folder).resolve();r = p['reports']['RED'];c = r['context'];duration = r['duration_seconds']
    if (p.get('kind') != 'wcfi_red_lowmid_package' or type(p.get('schema_version')) is not int or p['schema_version'] != 1
            or set(p['reports']) != {'RED'} or r.get('kind') != 'wcfi_red_lowmid_study' or r.get('variants') != DESCRIPTIONS
            or r.get('filter') != FILTER or r.get('kit_choice') != KIT_CHOICE
            or r.get('processing_applied') is not False or r.get('preference_established') is not False
            or r.get('audio_models_run') != 0 or r.get('origin') != 'saved_native_capture_plus_local_RED_EQ_and_KIT_body_preview'
            or not isinstance(r.get('title'), str) or not 1 <= len(r['title']) <= 100):
        raise ValueError('Invalid RED trial scope, fixed KIT bed or provenance')
    if (type(duration) not in (int, float) or not .5 <= duration <= 20 or c['duration_seconds'] != duration
            or not 0 <= c['start_seconds'] <= 86400 or not 0 <= c['validation']['master_sum_relative_error'] <= .001
            or c['validation']['fixed_bed_verified'] is not True or c['validation']['saved_capture_verified'] is not True):
        raise ValueError('Unverified RED passage or mix substitution')
    if (not -3 <= r['level_control_gain_db'] <= 0 or set(r['red_loudness']) != set(VARIANTS)
            or abs(r['red_loudness']['eq']['integrated_lufs']-r['red_loudness']['level']['integrated_lufs']) > .15
            or abs(r['observed']['red']['level']['lowmid_over_presence_db']-r['observed']['red']['current']['lowmid_over_presence_db']) > .001
            or r['observed']['red']['eq']['lowmid_over_presence_db'] >= r['observed']['red']['current']['lowmid_over_presence_db']):
        raise ValueError('Invalid RED EQ or loudness control')
    m = r['matching']
    if (m['method'] != 'Whole-mix integrated loudness; one scalar per version, no individual source matching'
            or not -60 <= m['target_lufs'] <= -24 or set(m['measured']) != set(VARIANTS)
            or set(m['variant_gain_db']) != set(VARIANTS) or not -60 <= m['original_shared_gain_db'] <= 0):
        raise ValueError('Invalid RED whole-mix matching')
    for obs in m['measured'].values():
        if abs(obs['integrated_lufs']-m['target_lufs']) > .15 or obs['true_peak_dbtp'] > -2.85:
            raise ValueError('Unverified RED listening-copy loudness')
    if set(r['audio']) != AUDIO_KEYS or set(p['files']) != {f'RED-{k}.wav' for k in AUDIO_KEYS}:
        raise ValueError('Incomplete RED listening assets')
    for key, filename in r['audio'].items():
        asset = (folder/filename).resolve()
        if filename != f'RED-{key}.wav' or asset.parent != folder or asset.stat().st_size > 16*1024*1024:
            raise ValueError('Invalid RED audio path')
        info = sf.info(asset)
        if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
            raise ValueError('RED audio differs from its declared format')
        a, _ = sf.read(asset, dtype='float32', always_2d=True)
        if not np.isfinite(a).all() or hashlib.sha256(asset.read_bytes()).hexdigest() != p['files'][filename]:
            raise ValueError('RED samples or checksum invalid')
    return p, hashlib.sha256(raw).hexdigest()
