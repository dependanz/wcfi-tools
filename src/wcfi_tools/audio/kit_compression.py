"""Native KIT compression comparisons with fixed post-compressor EQ.

Gain reduction is estimated from aligned pre/post samples, not read from a
plug-in meter. This module never writes to Live or starts its transport.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from ..ableton.client import choose_track
from .kit_diagnostic import audio_array
from .kit_shelf import presence_bell
from .kit_study import low_shelf, write_auditions

VARIANTS = ('current', 'gentle', 'light')
AUDIO_KEYS = {f'study_{scope}_{v}{suffix}' for scope in ('kit', 'mix')
              for v in VARIANTS for suffix in ('', '_matched')}
DESCRIPTIONS = [
    {'id': 'current', 'label': 'Current compression + fixed EQ', 'threshold_db': -6},
    {'id': 'gentle', 'label': 'Gentle compression + fixed EQ', 'threshold_db': -16},
    {'id': 'light', 'label': 'Light compression + fixed EQ', 'threshold_db': -18},
]
FIXED_EQ = {'shelf': {'hz': 120, 'gain_db': -1, 'slope': 1},
            'bell': {'hz': 3000, 'gain_db': 2, 'q': .7}, 'order': ['low_shelf', 'bell'],
            'placement': 'identical local DSP after each native compressor capture'}


def threshold_control(snapshot):
    devices = [d for d in choose_track(snapshot, 'KIT')['devices'] if d['class_name'] == 'Compressor2']
    if len(devices) != 1:
        raise ValueError('Choose a KIT with one identifiable native Compressor')
    return next(p for p in devices[0]['parameters'] if p['name'] == 'Threshold')


def only_threshold_changed(original, candidate):
    before, after = copy.deepcopy(original), copy.deepcopy(candidate)
    before.pop('inspected_at', None);after.pop('inspected_at', None)
    old, new = threshold_control(before), threshold_control(after)
    change = {'parameter': 'Threshold', 'before': old['display'], 'after': new['display'],
              'before_value': old['value'], 'after_value': new['value']}
    # The bridge keeps identity, range and automation metadata unchanged.
    old['value'], old['display'] = new['value'], new['display']
    if before != after:
        raise ValueError('Native trial changed more than KIT compressor Threshold')
    return change


def gain_reduction(raw, processed, *, output_gain_db):
    """Estimate linked scalar attenuation with positive-polarity waveform proof.

    Strong windows are the loudest 10% of non-silent raw 10 ms windows.
    Energy-weighted scalar fits avoid divisions at waveform zero crossings.
    1 ms fits bound within-window attenuation and check the amplifier model.
    """
    x, y = audio_array(raw), audio_array(processed)
    if x.shape != y.shape or not math.isfinite(output_gain_db) or not -36 <= output_gain_db <= 36:
        raise ValueError('Use aligned same-length pairs and a known compressor output gain')
    if np.mean(x*x) < 1e-16:
        raise ValueError('Silent input cannot establish gain reduction')
    def windows(size):
        count = len(x)//size
        a, b = x[:count*size].reshape(count, size, 2), y[:count*size].reshape(count, size, 2)
        power = np.mean(a*a, axis=(1, 2))
        active = power > max(1e-16, float(np.max(power))*10**(-35/10))
        fitted = np.sum(a*b, axis=(1, 2))/np.maximum(np.sum(a*a, axis=(1, 2)), 1e-30)
        if np.any(fitted[active] <= 0):
            raise ValueError('The paired signals do not support a positive gain-only compressor model')
        reduction = output_gain_db-20*np.log10(np.maximum(fitted, 1e-24))
        return a, b, power, active, fitted, reduction
    a, b, _, active, fitted, reduction = windows(48)
    residual = b-fitted[:, None, None]*a
    relative_error = float(np.sqrt(np.mean(residual[active]**2))/max(1e-12, np.sqrt(np.mean(b[active]**2))))
    if relative_error > .02:
        raise ValueError('Paired waveforms do not validate a time-varying gain estimate')
    _, _, ten_power, ten_active, _, ten_reduction = windows(480)
    strong = ten_active & (ten_power >= np.percentile(ten_power[ten_active], 90))
    return {'method': 'Aligned waveform scalar fits; known Output Gain removed. Estimated attenuation, not native gain-reduction meter telemetry.',
        'strong_window_definition': 'Loudest 10% of raw 10 ms windows above a relative -35 dB energy gate; not individual drum-hit identification',
        'strong_windows': int(np.count_nonzero(strong)),
        'strong_p50_p90_db': [float(v) for v in np.percentile(ten_reduction[strong], [50, 90])],
        'active_1ms_p99_db': float(np.percentile(reduction[active], 99)),
        'active_1ms_max_db': float(np.max(reduction[active])),
        'gain_model_relative_rms_error': relative_error,
        'output_gain_db': output_gain_db,
        'timing_method': 'Complete known-loop cyclic alignment, not a host latency measurement'}


def compression_versions(raw, processed, mix, *, kit_to_mix_gain, output_gain_db, sum_relative_error):
    """Same raw loop, native compressor captures, one fixed EQ, one mix remainder."""
    raw, mix = audio_array(raw), audio_array(mix)
    if (set(processed) != set(VARIANTS) or raw.shape != mix.shape
            or not math.isfinite(kit_to_mix_gain) or not 0 < kit_to_mix_gain <= 64
            or not math.isfinite(sum_relative_error) or not 0 <= sum_relative_error <= .001):
        raise ValueError('Use all three aligned native captures and a verified passive mix path')
    processed = {v: audio_array(a) for v, a in processed.items()}
    if any(a.shape != raw.shape for a in processed.values()):
        raise ValueError('Every version must contain the same complete recorded loop')
    reductions = {v: gain_reduction(raw, a, output_gain_db=output_gain_db) for v, a in processed.items()}
    kits = {v: presence_bell(low_shelf(a, 48000, hz=120, gain_db=-1), 48000, gain_db=2)
            for v, a in processed.items()}
    remainder = mix-processed['current']*kit_to_mix_gain
    mixes = {v: remainder+a*kit_to_mix_gain for v, a in kits.items()}
    return kits, mixes, reductions


def build_package(raw, processed, mix, output, *, ffmpeg, context):
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new compression-study directory')
    kits, mixes, reductions = compression_versions(raw, processed, mix,
        kit_to_mix_gain=context['path']['kit_to_mix_gain'],
        output_gain_db=context['compressor']['output_gain_db'],
        sum_relative_error=max(context['validation']['master_sum_relative_errors'].values()))
    output.mkdir(parents=True)
    audio, stats, matching = write_auditions(kits, mixes, output, 48000, ffmpeg,
        match_kit_in_mix=True, kit_to_mix_gain=context['path']['kit_to_mix_gain'])
    report = {'kind': 'wcfi_kit_compression_study', 'title': 'October 11 · KIT light compression test',
        'context': context, 'duration_seconds': len(raw)/48000, 'variants': copy.deepcopy(DESCRIPTIONS),
        'audio': audio, 'measurements': stats, 'matching': matching, 'gain_reduction': reductions,
        'fixed_eq': copy.deepcopy(FIXED_EQ), 'processing_applied': False, 'audio_models_run': 0,
        'preference_established': False, 'native_settings_restored': True,
        'origin': 'native_compressor_captures_with_identical_local_post_eq',
        'notes': [
            'The native Ableton compressor Threshold was changed for each trial and restored afterwards. Ratio, attack, release, output gain, faders and routing stayed fixed; Play and Record were never used.',
            'All versions include the same local post-compressor EQ: −1 dB shelf at 120 Hz, slope 1, then +2 dB bell at 3 kHz, Q 0.7. This EQ has not been installed in Ableton.',
            'Gain reduction is estimated before EQ from aligned input/output waveforms after removing the known Output Gain. It is not native meter telemetry. Strong windows are the loudest 10% of active raw 10 ms windows, not identified individual drum hits.',
            'The mix contains RED, KIT and BASS only, including the Master fader. Other band and return faders remain down. A verified passive path permits replacing only KIT with the fixed-EQ preview.',
            'Matched listening equalizes KIT integrated loudness using scalar gain only. RED and BASS remain fixed across mix versions after one common audition gain. Total mix LUFS may vary slightly.',
            'Compare punch and hit consistency, then check for softened attacks, cymbal swells or pumping. Lower peaks and a successful reduction target do not establish better musical quality.',
            'Alignment uses a complete known repeated loop; these offsets do not measure host latency.',
        ]}
    package = {'kind': 'wcfi_kit_compression_package', 'schema_version': 1, 'reports': {'KIT': report},
        'files': {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in audio.values()}}
    (output/'study.json').write_text(json.dumps(package, allow_nan=False), encoding='utf-8')
    return package


def validate_package(folder, package, raw):
    """Bounded checksummed audio and honest native/local processing provenance."""
    folder = Path(folder).resolve();r = package['reports']['KIT']
    if (package.get('kind') != 'wcfi_kit_compression_package'
            or type(package.get('schema_version')) is not int or package['schema_version'] != 1
            or set(package['reports']) != {'KIT'} or r.get('kind') != 'wcfi_kit_compression_study'
            or r.get('variants') != DESCRIPTIONS or r.get('fixed_eq') != FIXED_EQ
            or r.get('processing_applied') is not False or r.get('native_settings_restored') is not True
            or r.get('preference_established') is not False or r.get('audio_models_run') != 0
            or r.get('origin') != 'native_compressor_captures_with_identical_local_post_eq'
            or not isinstance(r.get('title'), str) or not 1 <= len(r['title']) <= 100
            or set(r['audio']) != AUDIO_KEYS):
        raise ValueError('Invalid compression study scope, fixed EQ or restoration provenance')
    duration = r['duration_seconds'];context = r['context']
    if (type(duration) not in (int, float) or not .5 <= duration <= 20
            or context['duration_seconds'] != duration
            or not 0 <= context['start_seconds'] <= 86400
            or set(context['validation']['master_sum_relative_errors']) != set(VARIANTS)
            or any(not 0 <= v <= .001 for v in context['validation']['master_sum_relative_errors'].values())
            or context['validation']['raw_and_other_sources_unchanged'] is not True
            or not 0 < context['path']['kit_to_mix_gain'] <= 64):
        raise ValueError('Unverified shared input or mix reconstruction')
    settings = context['compressor']
    if (settings['ratio'] != 2 or settings['attack_ms'] != 15 or settings['release_ms'] != 100
            or settings['wet'] != 1 or settings['lookahead_ms'] != 0
            or not -36 <= settings['output_gain_db'] <= 36):
        raise ValueError('Unexpected compressor settings')
    if set(context['native_trials']) != {'gentle', 'light'}:
        raise ValueError('Both native trial journals are required')
    for v in ('gentle', 'light'):
        trial = context['native_trials'][v]
        if (trial['restored'] is not True or trial['only_threshold_changed'] is not True
                or trial['new_underruns'] != 0 or trial['threshold_db'] != DESCRIPTIONS[VARIANTS.index(v)]['threshold_db']):
            raise ValueError('Native threshold trial was not verified and restored')
    if set(r['gain_reduction']) != set(VARIANTS):
        raise ValueError('Gain reduction estimates are incomplete')
    for v, m in r['gain_reduction'].items():
        if (m['output_gain_db'] != settings['output_gain_db'] or not 0 <= m['gain_model_relative_rms_error'] <= .02
                or not 0 < m['strong_windows'] <= round(duration*100)
                or len(m['strong_p50_p90_db']) != 2
                or not -.1 <= m['strong_p50_p90_db'][0] <= m['strong_p50_p90_db'][1] <= m['active_1ms_max_db']+.05
                or not -.1 <= m['active_1ms_p99_db'] <= m['active_1ms_max_db'] <= 3.5):
            raise ValueError('Gain reduction estimates do not validate light compression')
    if not 1 <= r['gain_reduction']['light']['strong_p50_p90_db'][1] <= 3:
        raise ValueError('Light trial must reach 1–3 dB on stronger windows')
    if not (r['gain_reduction']['current']['strong_p50_p90_db'][1]
            < r['gain_reduction']['gentle']['strong_p50_p90_db'][1]
            < r['gain_reduction']['light']['strong_p50_p90_db'][1]):
        raise ValueError('Measured compression strengths are not ordered')
    for scope in ('kit', 'mix'):
        m = r['matching'][scope]
        if (set(m['measured_matched_loudness']) != set(VARIANTS) or not -60 <= m['target_lufs'] <= -24
                or scope == 'mix' and m.get('kit_contribution_matched') is not True):
            raise ValueError('Incomplete or unsafe loudness matching')
        for obs in m['measured_matched_loudness'].values():
            if obs['true_peak_dbtp'] > -2.85 or scope == 'kit' and abs(obs['integrated_lufs']-m['target_lufs']) > .15:
                raise ValueError('Unverified listening-copy loudness')
    if set(package['files']) != {f'KIT-{k}.wav' for k in AUDIO_KEYS}:
        raise ValueError('Incomplete compression listening assets')
    for key, filename in r['audio'].items():
        asset = (folder/filename).resolve()
        if filename != f'KIT-{key}.wav' or asset.parent != folder or asset.stat().st_size > 16*1024*1024:
            raise ValueError('Invalid compression asset path')
        info = sf.info(asset)
        if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
            raise ValueError('Compression audio format differs from its manifest')
        a, _ = sf.read(asset, dtype='float32', always_2d=True)
        if not np.isfinite(a).all() or hashlib.sha256(asset.read_bytes()).hexdigest() != package['files'][filename]:
            raise ValueError('Compression audio checksum or samples are invalid')
    return package, hashlib.sha256(raw).hexdigest()
