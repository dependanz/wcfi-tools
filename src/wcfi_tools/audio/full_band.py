"""Saved-stem full-band balance with fixed RED/KIT treatment; no DAW writes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .kit_diagnostic import audio_array
from .updated_reference import (
    AUDIO,
    CHOICES,
    IDS,
    matched_reference_audio,
    validate_reference_assets,
)

PLAN = [
    {'track': 'EGUITAR', 'role': 'Electric guitar', 'fader_db': -6, 'pan': -.35},
    {'track': 'AC GUITAR', 'role': 'Acoustic guitar', 'fader_db': -24, 'pan': .35},
    {'track': 'KEYS', 'role': 'Stereo keys', 'fader_db': 0, 'pan': 0},
    {'track': 'GREEN', 'role': 'Quiet backing mic; performance activity unconfirmed', 'fader_db': 6, 'pan': .2},
]
EXCLUDED = {'YELLOW': 'Effectively silent in this passage', 'LAPTOP': 'Near-silent; no music contribution established',
            'LAPEL': 'Preacher channel, effectively silent here', 'MDMIC Talkback': 'Talkback is not part of the broadcast music mix',
            'AMBIENT': 'No recorded ambient stem exists in this take'}
DESCRIPTIONS = [
    {'id': 'before', 'label': 'Three-source foundation'},
    {'id': 'updated', 'label': 'Full-band working balance'},
    {'id': 'bethel', 'label': 'Bethel'},
    {'id': 'maverick', 'label': 'Maverick City'},
]


def place_mono(audio, pan):
    """Local center-normalized equal-power law, not a native Live pan render."""
    a = audio_array(audio)
    if not math.isfinite(pan) or not -.5 <= pan <= .5:
        raise ValueError('Use bounded moderate local placement')
    if pan == 0:
        return a.copy()
    if np.max(np.abs(a[:, 0]-a[:, 1])) > 1e-7:
        raise ValueError('Preserve recorded stereo; only place dual-mono additions')
    angle = (pan+1)*math.pi/4
    return a*np.array([math.sqrt(2)*math.cos(angle), math.sqrt(2)*math.sin(angle)])


def full_band_mix(foundation, additions):
    foundation = audio_array(foundation)
    if set(additions) != {p['track'] for p in PLAN}:
        raise ValueError('Use the four identified band additions; exclude talkback and unused sources')
    contributions = {}
    for p in PLAN:
        a = audio_array(additions[p['track']])
        if a.shape != foundation.shape:
            raise ValueError('All band additions must use the same synchronized passage')
        # Master -6 dB is already included in the fixed foundation.
        contributions[p['track']] = place_mono(a, p['pan'])*10**((p['fader_db']-6)/20)
    return foundation+sum(contributions.values()), contributions


def findings(metrics, source_stats):
    a, b = metrics['before'], metrics['updated'];refs = [metrics[v] for v in ('bethel', 'maverick')]
    ac = source_stats['AC GUITAR']
    return [
        {'id': 'capture', 'title': 'Acoustic input has recorded full-scale indicators' if ac['full_scale_samples'] else 'Acoustic input sample health',
         'evidence': f"AC GUITAR has {ac['full_scale_samples']} full-scale samples ({ac['full_scale_percent']:.2f}% of the measured channel samples) in the raw 20-second excerpt. Its raw peak is {ac['peak_dbfs']:.2f} dBFS.",
         'next_test': ('The acoustic fader equivalent is kept at −24 dB in this preview. At the next rehearsal check its instrument output and physical analogue mixer trim. Lower playback gain cannot restore clipped recording peaks.' if ac['full_scale_samples'] else 'No full-scale indicators are observed in this raw excerpt. Establish its musical balance before considering input changes; this alone does not rule out upstream distortion.')},
        {'id': 'space', 'title': 'Full-band placement and remaining stereo context',
         'evidence': f"Side energy: {a['side_energy_percent']:.2f}% foundation → {b['side_energy_percent']:.2f}% full band; Bethel {refs[0]['side_energy_percent']:.1f}%, Maverick {refs[1]['side_energy_percent']:.1f}%. Full-band mono fold-down energy change is {b['mono_energy_change_db']:.2f} dB.",
         'next_test': 'Compare guitar/keys placement and mono compatibility. The keys recording itself is fairly narrow, and no ambience return is included yet. Test a restrained filtered room return after confirming this dry balance; reference percentages are not targets.'},
        {'id': 'tone', 'title': 'Low-mid versus presence after adding the band',
         'evidence': f"200–500 Hz / 1–3 kHz: {a['low_mid_over_presence_db']:.2f} → {b['low_mid_over_presence_db']:.2f} dB; Bethel {refs[0]['low_mid_over_presence_db']:.2f}, Maverick {refs[1]['low_mid_over_presence_db']:.2f} dB.",
         'next_test': 'Judge whether RED remains easy to follow above guitar/keys and the rhythm section. These are different performances; a lower spectral ratio alone does not establish clearer lyrics or better production.'},
    ]


def build_package(foundation, additions, references, output, *, sources, context, source_stats, ffmpeg):
    if set(references) != {'bethel', 'maverick'}:
        raise ValueError('Use both saved references')
    full, contributions = full_band_mix(foundation, additions)
    arrays = {'before': foundation, 'updated': full, **references}
    metrics, matching = matched_reference_audio(arrays, output, ffmpeg=ffmpeg)
    # Contribution powers describe the sources, not perceived source loudness.
    stats = {name: {'rms_dbfs': float(10*np.log10(max(float(np.mean(a*a)), 1e-24))),
                    'sample_peak_dbfs': float(20*np.log10(max(float(np.max(np.abs(a))), 1e-12)))}
             for name, a in contributions.items()}
    r = {'kind': 'wcfi_full_band_study', 'title': 'October 11 · Full-band balance ↔ Bethel / Maverick',
        'duration_seconds': 12, 'variants': copy.deepcopy(DESCRIPTIONS), 'audio': AUDIO.copy(),
        'sources': sources, 'context': context, 'fixed_choices': copy.deepcopy(CHOICES),
        'plan': copy.deepcopy(PLAN), 'excluded': EXCLUDED.copy(), 'source_stats': source_stats,
        'contributions': stats, 'metrics': metrics, 'matching': matching, 'findings': findings(metrics, source_stats),
        'processing_applied': False, 'preference_established': False, 'audio_models_run': 0,
        'scope': 'saved_seven_source_full_band_dry_preview_vs_complete_references',
        'notes': [
            'All WCFI channels come from the same sample-aligned October 11 take, 40–52 s. Raw stems receive the same emulator loop envelope before slicing. The fixed RED/KIT/BASS foundation is unchanged, including its existing Master gain.',
            'Keep RED’s −2 dB 400 Hz bell; KIT’s native gentle compressor capture, −1 dB 120 Hz shelf, +2 dB 3 kHz bell and −2 dB 180 Hz body bell; BASS unchanged. These are the previously selected local previews, not changes to the current Ableton devices.',
            'EGUITAR −6 dB, AC GUITAR −24 dB, KEYS 0 dB and GREEN +6 dB are preview fader equivalents with Master −6 dB. They are engineering starting levels based on source loudness, role and headroom, not model scores or a validated optimum.',
            'Guitars are placed 35% left/right and GREEN 20% right with a local center-normalized equal-power law. Recorded KEYS stereo is preserved; RED, KIT and BASS retain their existing placement. This law is not claimed to reproduce Ableton’s pan control exactly.',
            'GREEN is quiet and its own performance versus bleed has not been established. Its fader is capped at +6 dB, giving unity raw-to-mix gain after Master, rather than normalizing a potentially unused mic to the lead. YELLOW and LAPTOP are effectively silent here; lapel and talkback are excluded.',
            'AC GUITAR contains recorded full-scale indicators. Lower gain prevents it dominating the mix but does not repair possible input clipping. No declipper, new EQ or neural cleanup was applied to the added sources.',
            'This is a dry seven-source music mix. No ambient recording exists in this take, and existing return effects are not reconstructed or enabled. Wetness/depth and whole-service performance remain untested.',
            'Each entire twelve-second excerpt is scalar matched to one integrated LUFS target, with saved true-peak and descriptor verification. Matching preserves the intended balance; no source is separately normalized inside the final mixes.',
            'The exact saved Bethel and Maverick excerpts are reused. Different songs, voices, arrangements and unknown mastering remain confounds; reference width, spectral ratios and crest factor are descriptors, not professional-quality targets.',
            'No Play/Record, mouse/keyboard control, host fader/device write, GPU, model inference, new download or cloud audio upload is used. These are reversible local auditions and the current Ableton mix is left intact.',
        ]}
    output = Path(output)
    p = {'kind': 'wcfi_full_band_package', 'schema_version': 1, 'reports': {'MIX': r},
        'files': {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in AUDIO.values()}}
    (output/'study.json').write_text(json.dumps(p, allow_nan=False), encoding='utf-8')
    return p


def validate_package(folder, p, raw):
    folder = Path(folder).resolve();r = p['reports']['MIX'];c = r['context'];m = r['matching']
    if (p.get('kind') != 'wcfi_full_band_package' or type(p.get('schema_version')) is not int or p['schema_version'] != 1
            or set(p['reports']) != {'MIX'} or r.get('kind') != 'wcfi_full_band_study' or r.get('variants') != DESCRIPTIONS
            or r.get('plan') != PLAN or r.get('excluded') != EXCLUDED or r.get('fixed_choices') != CHOICES
            or r.get('audio') != AUDIO or r.get('duration_seconds') != 12 or r.get('processing_applied') is not False
            or r.get('preference_established') is not False or r.get('audio_models_run') != 0
            or r.get('scope') != 'saved_seven_source_full_band_dry_preview_vs_complete_references'
            or not isinstance(r.get('title'), str) or not 1 <= len(r['title']) <= 100):
        raise ValueError('Invalid full-band balance, fixed processing or scope')
    if (c['duration_seconds'] != 12 or not 0 <= c['start_seconds'] <= 86400
            or c['validation']['source_hashes_verified'] is not True or c['validation']['foundation_unchanged'] is not True
            or c['validation']['added_channels_have_no_active_FX'] is not True
            or set(r['sources']) != set(IDS) or set(r['metrics']) != set(IDS)
            or r['findings'] != findings(r['metrics'], r['source_stats'])
            or set(r['contributions']) != {a['track'] for a in PLAN}
            or not {a['track'] for a in PLAN} <= set(r['source_stats'])
            or not 0 <= r['source_stats']['AC GUITAR']['full_scale_percent'] <= 100
            or not -60 <= m['target_lufs'] <= -24 or set(m['measured']) != set(IDS) or set(m['gain_db']) != set(IDS)):
        raise ValueError('Unverified full-band sources, foundation or observations')
    for a in PLAN:
        stats = r['source_stats'][a['track']]
        if (not 0 <= stats['full_scale_percent'] <= 100 or not 0 <= stats['full_scale_samples'] <= 1920000
                or type(stats['peak_dbfs']) not in (int, float)
                or stats['loudness']['integrated_lufs'] is not None
                    and type(stats['loudness']['integrated_lufs']) not in (int, float)):
            raise ValueError('Invalid included-source observation')
    validate_reference_assets(folder, p, r)
    return p, hashlib.sha256(raw).hexdigest()
