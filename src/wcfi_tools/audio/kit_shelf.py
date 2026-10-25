"""Stereo drum-bus shelf, presence and combined experiments; no host writes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import sosfilt, tf2sos

from .kit_diagnostic import analyze_signal, audio_array
from .kit_study import low_shelf, write_auditions

VARIANTS = ('current', 'shelf1', 'shelf2')
AUDIO_KEYS = {f'study_{scope}_{variant}{suffix}' for scope in ('kit', 'mix')
              for variant in VARIANTS for suffix in ('', '_matched')}
DESCRIPTIONS = [
    {'id': 'current', 'label': 'Current chain', 'gain_db': 0, 'origin': 'native_capture'},
    {'id': 'shelf1', 'label': '120 Hz shelf −1 dB', 'gain_db': -1, 'origin': 'local_eq_preview'},
    {'id': 'shelf2', 'label': '120 Hz shelf −2 dB', 'gain_db': -2, 'origin': 'local_eq_preview'},
]
PRESENCE_VARIANTS = ('current', 'presence1', 'presence2')
PRESENCE_DESCRIPTIONS = [
    {'id': 'current', 'label': 'Current chain', 'gain_db': 0, 'origin': 'native_capture'},
    {'id': 'presence1', 'label': '3 kHz bell +1 dB', 'gain_db': 1, 'origin': 'local_eq_preview'},
    {'id': 'presence2', 'label': '3 kHz bell +2 dB', 'gain_db': 2, 'origin': 'local_eq_preview'},
]
COMBINATIONS = ((1, 1), (1, 2), (2, 1), (2, 2))
COMBINED_DESCRIPTIONS = [
    {'id': 'current', 'label': 'Current chain', 'shelf_db': 0, 'presence_db': 0, 'origin': 'native_capture'},
    *[{'id': f's{s}p{p}', 'label': f'Shelf −{s} / Bell +{p} dB', 'shelf_db': -s,
       'presence_db': p, 'origin': 'local_eq_preview'} for s, p in COMBINATIONS],
]
STUDIES = {
    'shelf': {'package': 'wcfi_kit_shelf_package', 'report': 'wcfi_kit_shelf_study',
        'title': 'October 11 · KIT 120 Hz shelf test', 'variants': VARIANTS, 'descriptions': DESCRIPTIONS,
        'filter': {'type': 'low_shelf', 'hz': 120, 'slope': 1, 'placement': 'after the captured current compressor',
            'implementation': 'RBJ stereo minimum-phase shelf, periodic warmup; not a native Ableton EQ render'}},
    'presence': {'package': 'wcfi_kit_presence_package', 'report': 'wcfi_kit_presence_study',
        'title': 'October 11 · KIT 3 kHz presence test', 'variants': PRESENCE_VARIANTS, 'descriptions': PRESENCE_DESCRIPTIONS,
        'filter': {'type': 'bell', 'hz': 3000, 'q': .7, 'placement': 'after the captured current compressor',
            'implementation': 'RBJ stereo minimum-phase bell, periodic warmup; not a native Ableton EQ render'}},
    'combined': {'package': 'wcfi_kit_combined_package', 'report': 'wcfi_kit_combined_study',
        'title': 'October 11 · KIT combined EQ test', 'variants': tuple(d['id'] for d in COMBINED_DESCRIPTIONS),
        'descriptions': COMBINED_DESCRIPTIONS,
        'filter': {'type': 'shelf_plus_bell', 'shelf': {'hz': 120, 'slope': 1}, 'bell': {'hz': 3000, 'q': .7},
            'order': ['low_shelf', 'bell'], 'placement': 'after the captured current compressor',
            'implementation': 'RBJ stereo minimum-phase cascade, periodic warmup; not a native Ableton EQ render'}},
}


def audio_keys(report_kind):
    spec = next((s for s in STUDIES.values() if s['report'] == report_kind), None)
    if spec is None:
        raise ValueError('Unknown KIT EQ study kind')
    return {f'study_{scope}_{v}{suffix}' for scope in ('kit', 'mix')
            for v in spec['variants'] for suffix in ('', '_matched')}


def presence_bell(audio, rate, *, hz=3000, q=.7, gain_db=1):
    """Bounded requested presence lift; periodic warmup retains the loop seam."""
    if rate != 48000 or hz != 3000 or q != .7 or not math.isfinite(gain_db) or not 0 <= gain_db <= 2:
        raise ValueError('Presence preview supports a 3 kHz, Q 0.7 bell with 0–2 dB gain at 48 kHz')
    audio = audio_array(audio)
    a = 10**(gain_db/40);omega = 2*math.pi*hz/rate
    alpha = math.sin(omega)/(2*q);cosine = math.cos(omega)
    sos = tf2sos([1+alpha*a, -2*cosine, 1-alpha*a], [1+alpha/a, -2*cosine, 1-alpha/a])
    out = sosfilt(sos, np.tile(audio, (2, 1)), axis=0)[len(audio):]
    return out.astype(np.float32)


def validate_path(snapshot):
    """Recognize the existing passive KIT-to-Master path; reject unknown processing."""
    tracks = {t['name'].upper(): t for t in snapshot['tracks']}
    kit, group, master = (tracks[k] for k in ('KIT', 'INSTRUMENTS', 'MASTER'))
    for track in (kit, group, master):
        if (track['mute'] or track['solo'] or track['pan']['value'] != 0
                or track['volume']['automation_state'] != 0 or any(s['value'] != 0 for s in track['sends'])):
            raise ValueError('Shelf substitution requires an active centered path without sends or fader automation')
    if (kit['group_id'] != group['id'] or kit['routing']['output_routing_type'] != group['name']
            or group['routing']['output_routing_type'].lower() != 'master' or group['devices']):
        raise ValueError('Shelf substitution requires a direct KIT path through a passive group')
    names = [d['name'] for d in master['devices']]
    if names[:2] != ['WCFI Analysis Node']*2 or any(n not in ('WCFI Analysis Node', 'MiniMeters - Audio-Server') for n in names):
        raise ValueError('Master has an unknown or nonlinear processor; local mix substitution is unsafe')
    if not kit['devices'] or kit['devices'][-1]['name'] != 'WCFI Analysis Node':
        raise ValueError('The KIT output node must follow its processing')
    try:
        db = sum(float(t['volume']['display'].removesuffix(' dB')) for t in (kit, group, master))
    except ValueError:
        raise ValueError('The KIT-to-Master faders must have finite displayed gains') from None
    gain = 10**(db/20)
    if not math.isfinite(gain) or not 0 < gain <= 64:
        raise ValueError('The measured KIT path gain is out of bounds')
    return {'kit_to_mix_gain': gain, 'kit_to_mix_gain_db': db,
        'master_fader_included': True, 'scope': 'Final Master gain included; not the ATEM/encoded stream',
        'faders': {t['name']: t['volume']['display'] for t in (kit, group, master)}}


def shelf_versions(kit, mix, *, kit_to_mix_gain, sum_relative_error, recipe='shelf'):
    """Replace only KIT in a verified linear capture, preserving its recorded phase."""
    kit, mix = audio_array(kit), audio_array(mix)
    if kit.shape != mix.shape or not math.isfinite(sum_relative_error) or not 0 <= sum_relative_error <= .001:
        raise ValueError('The native mix must reconstruct from its aligned source contributions')
    if not math.isfinite(kit_to_mix_gain) or not 0 < kit_to_mix_gain <= 64:
        raise ValueError('A verified positive KIT-to-Master gain is required')
    if recipe == 'shelf':
        kits = {'current': kit.astype(np.float32),
                'shelf1': low_shelf(kit, 48000, hz=120, gain_db=-1),
                'shelf2': low_shelf(kit, 48000, hz=120, gain_db=-2)}
    elif recipe == 'presence':
        # Always begin from the unchanged chain, never a previous shelf candidate.
        kits = {'current': kit.astype(np.float32),
                'presence1': presence_bell(kit, 48000, gain_db=1),
                'presence2': presence_bell(kit, 48000, gain_db=2)}
    elif recipe == 'combined':
        kits = {'current': kit.astype(np.float32)}
        for shelf, presence in COMBINATIONS:
            kits[f's{shelf}p{presence}'] = presence_bell(
                low_shelf(kit, 48000, hz=120, gain_db=-shelf), 48000, gain_db=presence)
    else:
        raise ValueError('Choose the shelf, presence or combined experiment')
    mixes = {v: mix+(a-kit)*kit_to_mix_gain for v, a in kits.items()}
    return kits, mixes


def build_package(kit, mix, output, *, ffmpeg, context, recipe='shelf'):
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new EQ-study directory')
    if recipe not in STUDIES:
        raise ValueError('Choose the shelf, presence or combined experiment')
    spec = STUDIES[recipe]
    path = context['path']
    kits, mixes = shelf_versions(kit, mix, kit_to_mix_gain=path['kit_to_mix_gain'],
        sum_relative_error=context['validation']['master_sum_relative_error'], recipe=recipe)
    output.mkdir(parents=True)
    audio, stats, matching = write_auditions(kits, mixes, output, 48000, ffmpeg,
        match_kit_in_mix=True, kit_to_mix_gain=path['kit_to_mix_gain'])
    observed = {v: analyze_signal(a) for v, a in kits.items()}
    changes = {v: {'weight_over_attack_change_db': observed[v]['spectrum']['weight_over_attack_db']-
                      observed['current']['spectrum']['weight_over_attack_db'],
                  'crest_change_db': observed[v]['crest_db']-observed['current']['crest_db']}
               for v in spec['variants']}
    for change in changes.values():
        change['attack_over_weight_change_db'] = -change['weight_over_attack_change_db']
    report = {'kind': spec['report'], 'title': spec['title'],
        'context': context, 'duration_seconds': len(kit)/48000, 'variants': copy.deepcopy(spec['descriptions']),
        'audio': audio, 'measurements': stats, 'matching': matching, 'observed': observed, 'changes': changes,
        'filter': copy.deepcopy(spec['filter']),
        'processing_applied': False, 'preference_established': False, 'audio_models_run': 0,
        'notes': ['Current chain is the actual node capture. EQ variants are local DSP previews; Ableton effects and faders are unchanged.',
            'The mix is the current RED/KIT/BASS foundation, not the completed band mix. Its Master fader gain is included.',
            'Matched listening equalizes KIT integrated loudness in solo and in the mix. RED and BASS contributions stay fixed across the mix versions after the common audition gain. Total mix LUFS may differ slightly.',
            'Original-level mode retains the EQ level change. Matched mode is the intended tonal comparison.',
            {'shelf': '120 Hz is the shelf transition frequency. The low-frequency asymptote is −1 or −2 dB; the gain at 120 Hz is approximately half that.',
             'presence': 'The presence bell is centered at 3 kHz, Q 0.7. Its +1/+2 dB gains are at the center. These versions contain no 120 Hz shelf cut.',
             'combined': 'Each candidate starts from the same unchanged compressor output, followed by a 120 Hz shelf (slope 1) and a 3 kHz bell (Q 0.7). All four −1/−2 dB shelf and +1/+2 dB bell combinations are represented.'}[recipe],
            'Band and transient changes are measured descriptors; no version is established as the better musical choice.',
            'Complete known-loop cyclic alignment validates the saved captures; it is not a host latency measurement.']}
    package = {'kind': spec['package'], 'schema_version': 1, 'reports': {'KIT': report},
        'files': {filename: hashlib.sha256((output/filename).read_bytes()).hexdigest() for filename in audio.values()}}
    (output/'study.json').write_text(json.dumps(package, allow_nan=False), encoding='utf-8')
    return package


def validate_package(folder, package, raw):
    """Finite local assets and explicit preview/matching provenance before publication."""
    report = package['reports']['KIT'];folder = Path(folder).resolve()
    spec = next((s for s in STUDIES.values() if s['package'] == package.get('kind')), None)
    if spec is None:
        raise ValueError('Unknown KIT EQ package kind')
    keys = audio_keys(spec['report'])
    if (type(package.get('schema_version')) is not int or package['schema_version'] != 1
            or set(package['reports']) != {'KIT'}
            or report.get('kind') != spec['report'] or report.get('variants') != spec['descriptions']
            or report.get('processing_applied') is not False or report.get('preference_established') is not False
            or report.get('audio_models_run') != 0 or set(report['audio']) != keys
            or not isinstance(report.get('title'), str) or not 1 <= len(report['title']) <= 100):
        raise ValueError('Invalid KIT shelf preview scope, variants or provenance')
    expected_files = {f'KIT-{v}.wav' for v in keys};duration = report['duration_seconds']
    if set(package['files']) != expected_files or type(duration) not in (int, float) or not .5 <= duration <= 20:
        raise ValueError('Shelf-study assets or duration are invalid')
    if report['filter'] != spec['filter']:
        raise ValueError('EQ prescription differs from this experiment')
    context = report['context']
    if (type(context['start_seconds']) not in (int, float) or not 0 <= context['start_seconds'] <= 86400
            or context['duration_seconds'] != duration or context['validation']['master_sum_relative_error'] > .001
            or context['validation']['master_sum_relative_error'] < 0):
        raise ValueError('Shelf-study reconstruction or passage is unverified')
    for scope in ('kit', 'mix'):
        m = report['matching'][scope]
        if set(m['measured_matched_loudness']) != set(spec['variants']):
            raise ValueError('Shelf loudness observations are incomplete')
        if scope == 'mix' and m.get('kit_contribution_matched') is not True:
            raise ValueError('Mix matching must preserve other source levels')
        for v in spec['variants']:
            observed = m['measured_matched_loudness'][v]
            if observed['true_peak_dbtp'] > -2.85 or (scope == 'kit' and abs(observed['integrated_lufs']-m['target_lufs']) > .15):
                raise ValueError('Shelf listening levels are unverified or unsafe')
    for key, filename in report['audio'].items():
        asset = (folder/filename).resolve()
        if filename != f'KIT-{key}.wav' or asset.parent != folder or asset.stat().st_size > 16*1024*1024:
            raise ValueError('Invalid shelf-study asset path')
        info = sf.info(asset)
        if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
            raise ValueError('Shelf-study format differs from its manifest')
        samples, _ = sf.read(asset, dtype='float32', always_2d=True)
        if not np.isfinite(samples).all() or hashlib.sha256(asset.read_bytes()).hexdigest() != package['files'][filename]:
            raise ValueError('Shelf-study samples or checksum are invalid')
    return package, hashlib.sha256(raw).hexdigest()
