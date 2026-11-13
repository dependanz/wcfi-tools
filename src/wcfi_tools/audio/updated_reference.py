"""Loudness-matched saved foundation/reference check, without a quality score."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import soundfile as sf

from .kit_diagnostic import audio_array
from .kit_study import scan_loudness
from .reference_study import describe_mix

IDS = ('before', 'updated', 'bethel', 'maverick')
DESCRIPTIONS = [
    {'id': 'before', 'label': 'WCFI before RED EQ', 'scope': 'three_source_foundation'},
    {'id': 'updated', 'label': 'WCFI updated', 'scope': 'three_source_foundation'},
    {'id': 'bethel', 'label': 'Bethel', 'scope': 'complete_reference_mix'},
    {'id': 'maverick', 'label': 'Maverick City', 'scope': 'complete_reference_mix'},
]
AUDIO = {f'updatedref_{v}_matched': f'MIX-updatedref_{v}_matched.wav' for v in IDS}
CHOICES = {'red': {'hz': 400, 'gain_db': -2, 'q': 1},
           'kit': {'compressor_threshold_db': -16, 'ratio': 2, 'attack_ms': 15, 'release_ms': 100,
                   'shelf': {'hz': 120, 'gain_db': -1}, 'presence': {'hz': 3000, 'gain_db': 2, 'q': .7},
                   'body': {'hz': 180, 'gain_db': -2, 'q': 1}}, 'bass': 'unchanged'}


def findings(metrics):
    a, b = metrics['before'], metrics['updated'];refs = [metrics[v] for v in ('bethel', 'maverick')]
    space_gap = all(ref['side_energy_percent']-b['side_energy_percent'] >= 8 for ref in refs)
    tone_gap = all(b['low_mid_over_presence_db']-ref['low_mid_over_presence_db'] >= 3 for ref in refs)
    return [
        {'id': 'space', 'screened_gap': space_gap,
         'title': 'Largest structural gap: stereo presentation and the incomplete band' if space_gap else 'Stereo comparison: no large difference shared by both references',
         'evidence': f"Updated WCFI side energy is {b['side_energy_percent']:.2f}%, versus {refs[0]['side_energy_percent']:.1f}% Bethel and {refs[1]['side_energy_percent']:.1f}% Maverick. WCFI L/R correlation is {b['lr_correlation']:.3f}.",
         'interpretation': ('The measured foundation is nearly mono. Other WCFI band and return faders are down, while both references contain complete arrangements. Width does not measure reverb quality or prove a musical defect.' if space_gap else 'No large side-energy deficit is shared by both references under this descriptive screening rule. Side energy is not a reverb-quality or professional-quality score.'),
         'next_test': 'Reintroduce and balance recorded stereo keys/guitars and other intended sources, then test restrained placement and a filtered ambience return. Keep lead, kick and bass anchored and verify mono; do not widen Master to copy a percentage.'},
        {'id': 'tone', 'screened_gap': tone_gap,
         'title': 'Largest remaining tonal difference: low mids versus presence' if tone_gap else 'Low-mid versus presence comparison',
         'evidence': f"200–500 Hz / 1–3 kHz: {a['low_mid_over_presence_db']:.2f} → {b['low_mid_over_presence_db']:.2f} dB WCFI; {refs[0]['low_mid_over_presence_db']:.2f} Bethel; {refs[1]['low_mid_over_presence_db']:.2f} Maverick.",
         'interpretation': ('The RED cut changes the intended balance, but WCFI remains more low-mid weighted in this excerpt. ' if tone_gap else 'No large low-mid excess is shared by both references under this descriptive screening rule. ')+ 'Different singers, notes, arrangements and processing remain confounds; these ratios are not an EQ target or a vocal-intelligibility score.',
         'next_test': 'Judge RED placement with the complete band restored before another cut. Check contributing sources separately; do not apply a large Master EQ correction simply to match these ratios.'},
        {'id': 'dynamics', 'screened_gap': all(b['crest_db']-ref['crest_db'] >= 3 for ref in refs), 'title': 'Peak-to-average contrast',
         'evidence': f"Crest factor: WCFI {b['crest_db']:.2f} dB; Bethel {refs[0]['crest_db']:.2f} dB; Maverick {refs[1]['crest_db']:.2f} dB.",
         'interpretation': 'Different musical events, arrangements and unknown mastering affect peak/RMS contrast. Lower crest is not automatically better; twelve seconds cannot establish a broadcast loudness target or compressor setting.',
         'next_test': 'After balancing the complete band, test occasional peaks and short-term level consistency across multiple sections. Do not add heavy Master compression just to match these numbers.'},
    ]


def matched_reference_audio(arrays, output, *, ffmpeg):
    """Render the four named equal-duration references with verified scalar gain."""
    if set(arrays) != set(IDS):
        raise ValueError('Use four named comparison sources')
    arrays = {k: audio_array(a) for k, a in arrays.items()}
    if any(len(a) != 576000 for a in arrays.values()):
        raise ValueError('Use equal twelve-second comparison excerpts')
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new reference comparison directory')
    output.mkdir(parents=True);metrics = {}
    for v, a in arrays.items():
        sf.write(output/AUDIO[f'updatedref_{v}_matched'], a, 48000, subtype='FLOAT')
        metrics[v] = describe_mix(a, 48000) | {'loudness': scan_loudness(output/AUDIO[f'updatedref_{v}_matched'], ffmpeg)}
    target = min(-24., *(-3-m['loudness']['true_peak_dbtp']+m['loudness']['integrated_lufs'] for m in metrics.values()))
    matching = {'target_lufs': target, 'measured': {}, 'gain_db': {}, 'method': 'Verified whole-excerpt scalar matching only; no EQ, limiting or widening added by comparison'}
    for v, a in arrays.items():
        gain = target-metrics[v]['loudness']['integrated_lufs'];path = output/AUDIO[f'updatedref_{v}_matched']
        for _ in range(4):
            sf.write(path, a*10**(gain/20), 48000, subtype='FLOAT')
            checked = scan_loudness(path, ffmpeg)
            error = target-checked['integrated_lufs']
            if abs(error) <= .11:
                break
            gain += error
        if abs(error) > .15 or checked['true_peak_dbtp'] > -2.85:
            raise ValueError('Reference comparison loudness/peak verification failed')
        matching['measured'][v] = checked;matching['gain_db'][v] = gain
        # Use the actual saved float32 samples. Tiny quantization noise can
        # change ratios in otherwise empty bands of procedural test signals.
        input_loudness = metrics[v]['loudness']
        saved, _ = sf.read(path, dtype='float32', always_2d=True)
        metrics[v] = describe_mix(saved, 48000) | {'loudness': checked, 'input_loudness': input_loudness}
    return metrics, matching


def build_package(signals, output, *, context, sources, ffmpeg):
    if set(signals) != set(IDS) or set(sources) != set(IDS):
        raise ValueError('Use both WCFI versions and both named saved references')
    arrays = {k: audio_array(a) for k, a in signals.items()}
    if any(len(a) != 576000 for a in arrays.values()):
        raise ValueError('Use equal twelve-second excerpts for this comparison')
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new reference comparison directory')
    metrics, matching = matched_reference_audio(arrays, output, ffmpeg=ffmpeg)
    report = {'kind': 'wcfi_updated_reference_study', 'title': 'Updated WCFI ↔ Bethel / Maverick',
        'duration_seconds': 12, 'variants': copy.deepcopy(DESCRIPTIONS), 'audio': AUDIO.copy(),
        'context': context, 'sources': sources, 'choices': copy.deepcopy(CHOICES), 'metrics': metrics,
        'matching': matching, 'findings': findings(metrics),
        'processing_applied': False, 'preference_established': False, 'audio_models_run': 0,
        'scope': 'WCFI_RED_KIT_BASS_foundation_vs_complete_reference_mixes',
        'notes': [
            'All excerpts are twelve seconds. WCFI uses 40–52 s from the existing October 11 analysis passage, sliced after processing the full 20 s loop. Reference audio is the exact saved, checksummed excerpt used in earlier studies; no new download or model listening occurred.',
            'Updated WCFI carries the working RED 400 Hz cut, KIT gentle native compression capture with fixed −1/+2 EQ and the KIT-only 180 Hz body cut. BASS stays unchanged. Before RED EQ has the same KIT/BASS bed and only omits RED’s cut.',
            'All four complete excerpts are matched to one integrated loudness target with scalar gain and verified true peaks. No additional EQ, limiter, stereo widener, time stretch, source separation or mastering is applied by this comparison.',
            'Our WCFI excerpt is a three-source foundation with the saved Master fader included. Other intended band channels and returns remain down. This is not a finished full-band mix or a capture of the encoded livestream.',
            'References are different songs and arrangements with unknown production/mastering. Corresponding seconds are not synchronized performances. The saved 12 s passages cannot support whole-service loudness targets, LRA conclusions or general quality rankings.',
            'Side energy and mono fold-down are measured spatial descriptors, not reverb-quality or professional-quality scores. Spectral ratios do not measure auditory masking or intelligibility. A distance to either reference is not an optimization objective.',
            'Descriptive screening uses a side-energy deficit of at least 8 percentage points or a low-mid/presence excess of at least 3 dB relative to both references. These thresholds organize hypotheses, not validated perceptual acceptance gates.',
            'The largest structural difference is the narrow incomplete foundation. Restoring intended sources and testing space is a proposed next experiment, not evidence that reverb alone will fix the gap.',
            'All processing choices remain local listening previews; the current Ableton chain is unchanged. No Play/Record, desktop control, GPU, cloud audio upload or model inference is used.',
        ]}
    p = {'kind': 'wcfi_updated_reference_package', 'schema_version': 1, 'reports': {'MIX': report},
        'files': {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in AUDIO.values()}}
    (output/'study.json').write_text(json.dumps(p, allow_nan=False), encoding='utf-8')
    return p


def validate_package(folder, p, raw):
    folder = Path(folder).resolve();r = p['reports']['MIX']
    if (p.get('kind') != 'wcfi_updated_reference_package' or type(p.get('schema_version')) is not int or p['schema_version'] != 1
            or set(p['reports']) != {'MIX'} or r.get('kind') != 'wcfi_updated_reference_study'
            or r.get('variants') != DESCRIPTIONS or r.get('choices') != CHOICES or r.get('audio') != AUDIO
            or r.get('duration_seconds') != 12 or r.get('processing_applied') is not False
            or r.get('preference_established') is not False or r.get('audio_models_run') != 0
            or r.get('scope') != 'WCFI_RED_KIT_BASS_foundation_vs_complete_reference_mixes'
            or not isinstance(r.get('title'), str) or not 1 <= len(r['title']) <= 100):
        raise ValueError('Invalid updated reference scope, choices or assets')
    c = r['context'];m = r['matching']
    if (c['duration_seconds'] != 12 or not 0 <= c['start_seconds'] <= 86400
            or c['validation']['source_packages_verified'] is not True
            or c['validation']['only_RED_differs_between_WCFI_versions'] is not True
            or set(r['sources']) != set(IDS) or set(r['metrics']) != set(IDS)
            or r['findings'] != findings(r['metrics'])
            or not -60 <= m['target_lufs'] <= -24 or set(m['measured']) != set(IDS) or set(m['gain_db']) != set(IDS)):
        raise ValueError('Unverified comparison provenance, findings or loudness')
    validate_reference_assets(folder, p, r)
    return p, hashlib.sha256(raw).hexdigest()


def validate_reference_assets(folder, p, r):
    """Shared bounded audio, provenance and saved-sample descriptor checks."""
    folder = Path(folder).resolve();m = r['matching']
    for v in IDS:
        source = r['sources'][v];checked = m['measured'][v]
        if (source['duration_seconds'] != 12 or len(source['sha256']) != 64
                or abs(checked['integrated_lufs']-m['target_lufs']) > .15 or checked['true_peak_dbtp'] > -2.85
                or not -60 <= m['gain_db'][v] <= 36):
            raise ValueError('Invalid reference source or matched observation')
        if v in ('bethel', 'maverick'):
            parsed = urlparse(source['metadata']['url'])
            if parsed.scheme != 'https' or parsed.hostname not in ('www.youtube.com', 'youtube.com', 'youtu.be'):
                raise ValueError('Use the saved public YouTube reference provenance')
    if set(p['files']) != set(AUDIO.values()):
        raise ValueError('Incomplete updated reference assets')
    for v in IDS:
        filename = AUDIO[f'updatedref_{v}_matched']
        asset = (folder/filename).resolve()
        if asset.parent != folder or asset.stat().st_size > 16*1024*1024:
            raise ValueError('Invalid reference asset path')
        info = sf.info(asset)
        if info.samplerate != 48000 or info.channels != 2 or info.frames != 576000:
            raise ValueError('Reference format differs from its manifest')
        a, _ = sf.read(asset, dtype='float32', always_2d=True)
        if not np.isfinite(a).all() or hashlib.sha256(asset.read_bytes()).hexdigest() != p['files'][filename]:
            raise ValueError('Reference samples or checksum invalid')
        observed = describe_mix(a, 48000)
        for key in ('side_energy_percent', 'lr_correlation', 'mono_energy_change_db', 'crest_db',
                    'low_mid_over_presence_db', 'sub_over_presence_db', 'high_over_presence_db'):
            if observed[key] is None or abs(observed[key]-r['metrics'][v][key]) > .001:
                raise ValueError('Reference comparison metrics do not match saved audio')
