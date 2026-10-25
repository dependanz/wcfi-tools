"""Bounded KIT/BASS body experiments on aligned native captures; no host writes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, tf2sos

from .kit_compression import FIXED_EQ
from .kit_diagnostic import audio_array
from .kit_study import measurements, scan_loudness

VARIANTS = ('current', 'bass_space', 'kit_body', 'both')
DESCRIPTIONS = [
    {'id': 'current', 'label': 'Gentle compression · unchanged tone', 'kit_cut_db': 0, 'bass_cut_db': 0},
    {'id': 'bass_space', 'label': 'BASS body cut', 'kit_cut_db': 0, 'bass_cut_db': -2},
    {'id': 'kit_body', 'label': 'KIT body cut', 'kit_cut_db': -2, 'bass_cut_db': 0},
    {'id': 'both', 'label': 'Both body cuts', 'kit_cut_db': -2, 'bass_cut_db': -2},
]
FILTERS = {'bass': {'hz': 200, 'q': .8}, 'kit': {'hz': 180, 'q': 1}}
AUDIO_KEYS = {f'overlap_mix_{v}{suffix}' for v in VARIANTS for suffix in ('', '_matched')}
BANDS = ((40, 90), (90, 140), (140, 250), (250, 500), (2000, 5000))


def body_bell(audio, *, hz, q, gain_db):
    """Fixed stereo minimum-phase preview, warmed with one complete saved loop."""
    a = audio_array(audio)
    if (hz, q) not in ((180, 1), (200, .8)) or gain_db not in (-2, 0):
        raise ValueError('Use the prescribed KIT or BASS body bell, 0 or −2 dB')
    if gain_db == 0:
        return a.copy()
    omega = 2*math.pi*hz/48000;alpha = math.sin(omega)/(2*q);g = 10**(gain_db/40)
    sos = tf2sos([1+alpha*g, -2*math.cos(omega), 1-alpha*g],
                [1+alpha/g, -2*math.cos(omega), 1-alpha/g])
    return sosfilt(sos, np.tile(a, (2, 1)), axis=0)[len(a):]


def band_frames(audio, low, high):
    a = audio_array(audio)
    sos = butter(4, (low, high), fs=48000, btype='bandpass', output='sos')
    filtered = sosfilt(sos, np.tile(a, (2, 1)), axis=0)[len(a):]
    count = len(a)//480
    return np.mean(filtered[:count*480].reshape(count, 480, 2)**2, axis=(1, 2))


def db_ratio(a, b):
    return float(10*np.log10(max(float(a), 1e-24)/max(float(b), 1e-24)))


def overlap_metrics(kit, bass, red, *, selection_kit):
    """Channel-power coactivity, not auditory masking or isolated kick detection."""
    arrays = [audio_array(a) for a in (kit, bass, red, selection_kit)]
    if any(a.shape != arrays[0].shape for a in arrays):
        raise ValueError('Overlap measurements require aligned equal-length source contributions')
    bands = {};powers = {}
    for lo, hi in BANDS:
        key = f'{lo}-{hi}'
        k, b, r, s = [band_frames(a, lo, hi) for a in arrays]
        active = s > max(1e-18, float(np.max(s))*10**(-35/10))
        if not np.any(active) or np.mean(b) < 1e-18:
            raise ValueError('Non-silent KIT and BASS are required for overlap comparison')
        selected = active & (s >= np.percentile(s[active], 90))
        powers[key] = {'kit': float(np.mean(k)), 'bass': float(np.mean(b)), 'red': float(np.mean(r))}
        bands[key] = {'selected_windows': int(np.count_nonzero(selected)),
            'bass_over_kit_db': db_ratio(np.mean(b), np.mean(k)),
            'bass_over_kit_strong_p50_db': float(np.median(10*np.log10(np.maximum(b[selected], 1e-24)/np.maximum(k[selected], 1e-24)))),
            'bass_within_6db_of_kit_fraction': float(np.mean(b[selected] >= k[selected]*10**(-6/10))),
            'kit_power_dbfs': db_ratio(np.mean(k), 1), 'bass_power_dbfs': db_ratio(np.mean(b), 1)}
    return {'bands': bands, 'kit_body_over_attack_db': db_ratio(powers['140-250']['kit'], powers['2000-5000']['kit']),
        'kit_weight_over_attack_db': db_ratio(powers['40-90']['kit'], powers['2000-5000']['kit']),
        'body_source_power_percent': {name: powers['140-250'][name]/sum(powers['140-250'].values())*100 for name in ('kit', 'bass', 'red')},
        'lowmid_source_power_percent': {name: powers['250-500'][name]/sum(powers['250-500'].values())*100 for name in ('kit', 'bass', 'red')}}


def versions(kit, bass, mix):
    kit, bass, mix = (audio_array(a) for a in (kit, bass, mix))
    if kit.shape != bass.shape or kit.shape != mix.shape:
        raise ValueError('Use same-length aligned mix contributions')
    remainder = mix-kit-bass
    sources = {v['id']: {'kit': body_bell(kit, **FILTERS['kit'], gain_db=v['kit_cut_db']),
                        'bass': body_bell(bass, **FILTERS['bass'], gain_db=v['bass_cut_db'])} for v in DESCRIPTIONS}
    mixes = {v: remainder+a['kit']+a['bass'] for v, a in sources.items()}
    return sources, mixes


def write_mix_auditions(mixes, output, *, ffmpeg, track='KIT', prefix='overlap'):
    """Verified whole-mix scalar matching; never normalize sources independently."""
    if (track, prefix) not in (('KIT', 'overlap'), ('RED', 'redlowmid')):
        raise ValueError('Unknown mix-audition namespace')
    output = Path(output)
    stats = {};audio = {}
    for v, a in mixes.items():
        key = f'{prefix}_mix_{v}';filename = f'{track}-{key}.wav';audio[key] = filename
        sf.write(output/filename, a, 48000, subtype='FLOAT')
        stats[v] = measurements(a, 48000) | {'loudness': scan_loudness(output/filename, ffmpeg)}
    target = min(-24., *(-3-m['loudness']['true_peak_dbtp']+m['loudness']['integrated_lufs'] for m in stats.values()))
    original_gain = min(0., -3-max(m['loudness']['true_peak_dbtp'] for m in stats.values()))
    matching = {'target_lufs': target, 'original_shared_gain_db': original_gain,
        'method': 'Whole-mix integrated loudness; one scalar per version, no individual source matching',
        'variant_gain_db': {}, 'measured': {}}
    for v, a in mixes.items():
        sf.write(output/audio[f'{prefix}_mix_{v}'], a*10**(original_gain/20), 48000, subtype='FLOAT')
        key = f'{prefix}_mix_{v}_matched';audio[key] = f'{track}-{key}.wav'
        gain = target-stats[v]['loudness']['integrated_lufs']
        for _ in range(4):
            sf.write(output/audio[key], a*10**(gain/20), 48000, subtype='FLOAT')
            m = scan_loudness(output/audio[key], ffmpeg)
            if abs(m['integrated_lufs']-target) <= .11:
                break
            gain += target-m['integrated_lufs']
        if abs(m['integrated_lufs']-target) > .15 or m['true_peak_dbtp'] > -2.85:
            raise ValueError('Whole-mix matching failed verification')
        matching['variant_gain_db'][v] = gain;matching['measured'][v] = m
    return audio, stats, matching


def build_package(kit, bass, red, mix, output, *, context, ffmpeg):
    """Entire-mix matching preserves each candidate's intended source balance."""
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new overlap-study directory')
    kit, bass, red, mix = (audio_array(a) for a in (kit, bass, red, mix))
    error = float(np.sqrt(np.mean((mix-kit-bass-red)**2))/max(1e-12, np.sqrt(np.mean(mix**2))))
    if error > .001 or context['validation']['master_sum_relative_error'] > .001:
        raise ValueError('Verify the passive three-source mix before substitution')
    sources, mixes = versions(kit, bass, mix)
    observed = {v: overlap_metrics(a['kit'], a['bass'], red, selection_kit=kit) for v, a in sources.items()}
    output.mkdir(parents=True)
    audio, stats, matching = write_mix_auditions(mixes, output, ffmpeg=ffmpeg)
    r = {'kind': 'wcfi_kit_bass_study', 'title': 'October 11 · KIT/BASS body and overlap test',
        'context': context, 'duration_seconds': len(kit)/48000, 'variants': copy.deepcopy(DESCRIPTIONS),
        'fixed_eq': copy.deepcopy(FIXED_EQ), 'filters': copy.deepcopy(FILTERS),
        'measurements': stats, 'matching': matching, 'overlap': observed, 'audio': audio,
        'processing_applied': False, 'preference_established': False, 'audio_models_run': 0,
        'origin': 'saved_native_gentle_capture_plus_local_eq_previews',
        'notes': [
            'All versions start with the same saved native KIT gentle-compression capture: threshold −16 dB, ratio 2:1, attack 15 ms, release 100 ms. The fixed −1 dB shelf at 120 Hz and +2 dB bell at 3 kHz follow it as local DSP. No host settings changed for this study.',
            'Candidates add a −2 dB bell to BASS at 200 Hz, Q 0.8; to KIT at 180 Hz, Q 1; or both. Periodic warmup retains the recorded loop alignment. These are local minimum-phase EQ previews, not native Ableton renders.',
            'Matched mode normalizes each whole mix with one scalar to the same integrated LUFS. It does not undo cuts by normalizing KIT or BASS separately. RED is unchanged before matching, but scales with the whole mix; Original levels retains identical RED contribution across versions.',
            'Overlap uses channel-power bandpass analysis in 10 ms windows. Strong windows are selected once from the baseline KIT separately in each band and reused for every version. The within-6 dB fraction is a descriptive coactivity threshold, not validated auditory masking, a quality score or an isolated kick detector.',
            'Source-power percentages sum separate track powers, excluding their phase-dependent cross terms. They describe where energy originates, not each track’s perceptual contribution to the summed mix.',
            'BASS is already well below KIT during its strongest 40–90 Hz windows. No deep-bass ducking is included. The body cuts test sustained 140–250 Hz energy versus attack; lower overlap or a higher attack/body ratio is not automatically a better sound.',
            'This is a RED/KIT/BASS foundation only, with its Master fader included, not the finished full-band or encoded stream. No Play/Record, desktop control, GPU or audio model is used.',
        ]}
    p = {'kind': 'wcfi_kit_bass_package', 'schema_version': 1, 'reports': {'KIT': r},
        'files': {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in audio.values()}}
    (output/'study.json').write_text(json.dumps(p, allow_nan=False), encoding='utf-8')
    return p


def validate_package(folder, package, raw):
    folder = Path(folder).resolve();r = package['reports']['KIT'];c = r['context']
    if (package.get('kind') != 'wcfi_kit_bass_package' or type(package.get('schema_version')) is not int
            or package['schema_version'] != 1 or set(package['reports']) != {'KIT'}
            or r.get('kind') != 'wcfi_kit_bass_study' or r.get('variants') != DESCRIPTIONS
            or r.get('fixed_eq') != FIXED_EQ or r.get('filters') != FILTERS
            or r.get('origin') != 'saved_native_gentle_capture_plus_local_eq_previews'
            or r.get('processing_applied') is not False or r.get('preference_established') is not False
            or r.get('audio_models_run') != 0 or not isinstance(r.get('title'), str) or not 1 <= len(r['title']) <= 100):
        raise ValueError('Invalid overlap study scope or processing provenance')
    duration = r['duration_seconds'];validation = c['validation']
    if (type(duration) not in (int, float) or not .5 <= duration <= 20 or c['duration_seconds'] != duration
            or not 0 <= c['start_seconds'] <= 86400 or not 0 <= validation['master_sum_relative_error'] <= .001
            or validation['gentle_capture_verified'] is not True or validation['other_sources_preserved'] is not True
            or c['compressor'] != {'threshold_db': -16, 'ratio': 2, 'attack_ms': 15, 'release_ms': 100}
            or set(r['overlap']) != set(VARIANTS) or set(r['measurements']) != set(VARIANTS)):
        raise ValueError('Unverified fixed compression, passage or source reconstruction')
    m = r['matching']
    if (m['method'] != 'Whole-mix integrated loudness; one scalar per version, no individual source matching'
            or not -60 <= m['target_lufs'] <= -24 or set(m['measured']) != set(VARIANTS)
            or set(m['variant_gain_db']) != set(VARIANTS) or not -60 <= m['original_shared_gain_db'] <= 0):
        raise ValueError('Invalid whole-mix matching')
    for obs in m['measured'].values():
        if abs(obs['integrated_lufs']-m['target_lufs']) > .15 or obs['true_peak_dbtp'] > -2.85:
            raise ValueError('Unverified listening loudness or peak ceiling')
    for obs in r['overlap'].values():
        if set(obs['bands']) != {f'{lo}-{hi}' for lo, hi in BANDS}:
            raise ValueError('Incomplete band observations')
        for band in obs['bands'].values():
            if not 0 < band['selected_windows'] <= duration*100 or not 0 <= band['bass_within_6db_of_kit_fraction'] <= 1:
                raise ValueError('Invalid fixed-window overlap observation')
    if set(r['audio']) != AUDIO_KEYS or set(package['files']) != {f'KIT-{k}.wav' for k in AUDIO_KEYS}:
        raise ValueError('Incomplete overlap audio assets')
    for key, filename in r['audio'].items():
        asset = (folder/filename).resolve()
        if filename != f'KIT-{key}.wav' or asset.parent != folder or asset.stat().st_size > 16*1024*1024:
            raise ValueError('Invalid overlap audio path')
        info = sf.info(asset)
        if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
            raise ValueError('Overlap audio format differs from its manifest')
        a, _ = sf.read(asset, dtype='float32', always_2d=True)
        if not np.isfinite(a).all() or hashlib.sha256(asset.read_bytes()).hexdigest() != package['files'][filename]:
            raise ValueError('Overlap audio samples or checksum invalid')
    return package, hashlib.sha256(raw).hexdigest()
