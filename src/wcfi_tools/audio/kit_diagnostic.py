"""Bounded CPU drum-bus diagnostics, paired measurements and local report assets.

Energy events are deliberately not labelled kick/snare. Full-mix references
cannot supply isolated drum EQ targets. This module never controls Live.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, find_peaks, sosfiltfilt, welch

from .kit_study import scan_loudness
from .reference_study import describe_mix

RATE = 48000
IDS = ("raw", "processed", "foundation", "bethel", "maverick")
AUDIO = {f"diagnostic_{name}_matched": f"KIT-diagnostic_{name}_matched.wav" for name in IDS}
BANDS = (("subsonic", 0, 30), ("weight", 30, 100), ("body", 100, 250),
         ("low_mid", 250, 500), ("mid", 500, 1500), ("upper_mid", 1500, 2000),
         ("attack", 2000, 5000), ("cymbal", 5000, 12000), ("air", 12000, 20000))
SOURCES = [
    {"title": "iZotope · How to EQ Drums", "url": "https://www.izotope.com/community/blog/how-to-eq-drums"},
    {"title": "iZotope · Mixing Live Drums", "url": "https://www.izotope.com/community/blog/2-approaches-to-mixing-live-drums"},
    {"title": "Fastl / Zwicker · Psychoacoustics", "url": "https://link.springer.com/book/10.1007/978-3-540-68888-4"},
    {"title": "Wichern et al. · Masking and loudness loss", "url": "https://aes.org/publications/elibrary/elibrary-page/?id=18450"},
]


def audio_array(audio):
    a = np.asarray(audio, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 2 or not RATE//2 <= len(a) <= RATE*20 or not np.isfinite(a).all():
        raise ValueError("Diagnostics require finite 0.5–20 second stereo 48 kHz audio")
    return a


def power_db(power):
    return float(10*np.log10(max(float(power), 1e-24)))


def frame_power(a, samples=480):
    count = len(a)//samples
    return np.mean(a[:count*samples].reshape(count, samples, 2)**2, axis=(1, 2))


def spectral(a):
    # Average channel powers: an antiphase stereo signal must not become silence.
    f, p = welch(a, fs=RATE, nperseg=8192, noverlap=4096, axis=0)
    p = p.mean(axis=1);total = max(float(np.sum(p[f < 20000])), 1e-24)
    bands = {name: float(np.sum(p[(f >= low) & (f < high)])) for name, low, high in BANDS}
    centers = 1000*2**(np.arange(-16, 13)/3)
    shape = []
    for center in centers:
        low, high = center/2**(1/6), center*2**(1/6)
        band = float(np.sum(p[(f >= low) & (f < high)]))
        shape.append({"hz": float(center), "relative_db": power_db(band/total)})
    db = 10*np.log10(np.maximum(p, 1e-24))
    peaks, props = find_peaks(db, prominence=6, distance=3)
    selected = [i for i, k in enumerate(peaks) if 40 <= f[k] <= 6000]
    selected = sorted(selected, key=lambda i: props['prominences'][i], reverse=True)[:6]
    return {"band_energy_percent": {k: v/total*100 for k, v in bands.items()},
        "shape": shape, "local_psd_peaks": [{"hz": float(f[peaks[i]]),
            "prominence_db": float(props['prominences'][i])} for i in selected],
        "low_mid_over_attack_db": power_db(bands['low_mid']/max(bands['attack'], 1e-24)),
        "weight_over_attack_db": power_db(bands['weight']/max(bands['attack'], 1e-24)),
        "peak_interpretation": "Local averaged spectral peaks, not confirmed ringing or automatic notch targets"}


def analyze_signal(audio):
    a = audio_array(audio);peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a*a)));levels = 10*np.log10(np.maximum(frame_power(a), 1e-24))
    active = levels[levels > max(-90, float(np.max(levels))-40)]
    equal = np.abs(np.diff(a, axis=0)) <= 1e-8
    near = np.abs(a[1:]) >= max(.001, .995*peak)
    stereo = describe_mix(a, RATE)
    return {"duration_seconds": len(a)/RATE, "sample_peak_dbfs": power_db(peak*peak),
        "rms_dbfs": power_db(rms*rms), "crest_db": power_db(peak*peak)-power_db(rms*rms),
        "full_scale_samples": int(np.count_nonzero(np.abs(np.abs(a)-1) <= 2**-23)),
        "above_full_scale_samples": int(np.count_nonzero(np.abs(a) > 1+2**-23)),
        "possible_flat_top_samples": int(np.count_nonzero((equal & near)[1:] & (equal & near)[:-1])),
        "level_spread_p90_p10_db": float(np.ptp(np.percentile(active, [10, 90]))) if len(active) else None,
        "lr_correlation": stereo['lr_correlation'], "side_energy_percent": stereo['side_energy_percent'],
        "mono_energy_change_db": stereo['mono_energy_change_db'], "spectrum": spectral(a)}


def band_signal(a, low, high):
    return sosfiltfilt(butter(4, (low, high), fs=RATE, btype='bandpass', output='sos'), a, axis=0)


def event_windows(raw):
    """Fixed pre-effects event selection; zero-phase filters are offline analysis only."""
    result = {}
    for name, low, high in (("low_band", 40, 150), ("mid_attack", 150, 5000)):
        envelope = frame_power(band_signal(raw, low, high), 240)
        levels = 10*np.log10(np.maximum(envelope, 1e-24))
        rise = np.r_[np.zeros(4), levels[4:]-levels[:-4]]
        peaks, _ = find_peaks(rise, height=3, prominence=2, distance=30)
        # Keep isolated windows with 20 ms lead-in and 200 ms of subsequent audio.
        peaks = [int(p) for p in peaks if 4 <= p < len(levels)-42 and levels[p] > np.max(levels)-35]
        result[name] = {"band_hz": [low, high], "events": peaks[:120], "hop_seconds": .005,
            "interpretation": "Energy-rise candidates; not a validated kick/snare classifier"}
    return result


def event_metrics(audio, events):
    result = {}
    for name, group in events.items():
        band = band_signal(audio, *group['band_hz']);ratios = []
        band_ratios = {key: [] for key, _, _ in BANDS}
        kept = []
        for index in group['events']:
            pos = index*240
            # Reject candidates with another selected event inside the measured tail.
            if any(index < other < index+40 for other in group['events']):
                continue
            early = band[pos-480:pos+2400];tail = band[pos+3840:pos+9600]
            ratios.append(power_db(np.mean(early**2)/max(np.mean(tail**2), 1e-24)))
            kept.append(index*.005)
            f, p = welch(audio[pos-480:pos+2400], fs=RATE, nperseg=2048, axis=0)
            p = p.mean(axis=1);total = max(float(p.sum()), 1e-24)
            for key, low, high in BANDS:
                band_ratios[key].append(float(p[(f >= low) & (f < high)].sum()/total*100))
        result[name] = {"selected_candidates": len(group['events']), "isolated_windows": len(kept),
            "times_seconds": kept, "early_over_tail_median_db": float(np.median(ratios)) if ratios else None,
            "early_over_tail_p10_p90_db": [float(v) for v in np.percentile(ratios, [10, 90])] if ratios else None,
            "early_band_energy_median_percent": {k: float(np.median(v)) if v else None for k, v in band_ratios.items()}}
    return result


def compare_pair(raw, processed):
    x, y = audio_array(raw), audio_array(processed)
    if x.shape != y.shape or np.mean(x*x) < 1e-18 or np.mean(y*y) < 1e-18:
        raise ValueError("Paired diagnostics need equal-length non-silent aligned audio")
    scalar = float(np.sum(x*y)/np.sum(x*x))
    if scalar <= 0:
        raise ValueError("Paired diagnostic needs a verified positive-polarity cyclic alignment")
    residual = float(np.sqrt(np.mean((y-x*scalar)**2))/np.sqrt(np.mean(y*y)))
    events = event_windows(x)
    before, after = analyze_signal(x), analyze_signal(y)
    px, py = frame_power(x), frame_power(y)
    active = px > np.max(px)*10**(-35/10)
    gain = 10*np.log10(np.maximum(py[active], 1e-24)/px[active])
    return {"best_fit_scalar_gain_db": float(20*np.log10(scalar)),
        "scalar_residual_relative_rms": residual,
        "correlation": float(np.sum(x*y)/np.sqrt(np.sum(x*x)*np.sum(y*y))),
        "frame_gain_p10_p50_p90_db": [float(v) for v in np.percentile(gain, [10, 50, 90])],
        "crest_change_db": after['crest_db']-before['crest_db'],
        "band_shape_change_db": {k: power_db(after['spectrum']['band_energy_percent'][k]/
            max(before['spectrum']['band_energy_percent'][k], 1e-24)) for k, _, _ in BANDS},
        "events": events, "raw_events": event_metrics(x, events), "processed_events": event_metrics(y, events),
        "event_method": "Raw-selected 20 ms rises >=3 dB; >=150 ms candidate spacing. Paired early -10–50 ms / tail 80–200 ms energy, excluding overlapping candidates. Mixed-kit windows can contain several instruments; not decay time or RT60.",
        "timing_method": "Complete known-loop cyclic alignment; not a host latency measurement"}


def diagnostic_findings(report):
    """Transparent engineering observations and bounded tests, never quality grades."""
    raw = report['measurements']['raw']
    pair = report['pair'];bands = raw['spectrum']['band_energy_percent']
    findings = [
        {'id': 'input', 'title': 'Raw input health', 'evidence':
            f"Raw peak {raw['sample_peak_dbfs']:.2f} dBFS; {raw['full_scale_samples']} full-scale samples, "
            f"{raw['above_full_scale_samples']} above full scale, {raw['possible_flat_top_samples']} possible flat-top samples. "
            'These are sample indicators in this window, not a guarantee that upstream distortion is absent.',
            'scope': 'paired_kit_measurement'},
        {'id': 'tone', 'title': 'Weight and body dominate the KIT spectrum', 'evidence':
            f"30–100 Hz: {bands['weight']:.1f}%; 100–250 Hz: {bands['body']:.1f}%; "
            f"250–500 Hz: {bands['low_mid']:.1f}%; 2–5 kHz: {bands['attack']:.1f}%; 5–12 kHz: {bands['cymbal']:.2f}% of power below 20 kHz. "
            'This describes the source; no universal drum-band percentage is assumed.', 'scope': 'paired_kit_measurement'},
    ]
    nearly_scalar = (pair['scalar_residual_relative_rms'] < .005 and abs(pair['crest_change_db']) < .2
        and max(abs(v) for v in pair['band_shape_change_db'].values()) < .25)
    findings.append({'id': 'processing', 'title': 'Current processing is almost entirely gain in this passage' if nearly_scalar
        else 'Current processing changes more than gain', 'evidence':
        f"Best-fit gain {pair['best_fit_scalar_gain_db']:.3f} dB; residual after that fit "
        f"{pair['scalar_residual_relative_rms']*100:.4f}% RMS; crest-factor change {pair['crest_change_db']:+.3f} dB. "
        'The gain-normalized spectral changes and raw-selected event windows below show how much processing remains.',
        'scope': 'paired_kit_measurement'})
    for key, label in (('low_band', '40–150 Hz events'), ('mid_attack', '150 Hz–5 kHz events')):
        a, b = pair['raw_events'][key], pair['processed_events'][key]
        if a['isolated_windows']:
            findings.append({'id': key, 'title': label, 'evidence':
                f"{a['isolated_windows']} isolated candidate windows. Median early/tail ratio "
                f"{a['early_over_tail_median_db']:.2f} → {b['early_over_tail_median_db']:.2f} dB. "
                'These are mixed-kit energy events, not identified kick/snare hits or reverberation times.',
                'scope': 'paired_kit_measurement'})
    findings.append({'id': 'references', 'title': 'Reference context has a different full-mix balance and width', 'evidence':
        f"Current three-source mix: {report['measurements']['foundation']['spectrum']['low_mid_over_attack_db']:.1f} dB "
        '250–500 Hz / 2–5 kHz; '
        f"Bethel: {report['measurements']['bethel']['spectrum']['low_mid_over_attack_db']:.1f} dB; "
        f"Maverick: {report['measurements']['maverick']['spectrum']['low_mid_over_attack_db']:.1f} dB. "
        'Vocals, guitars, keys, bass and mastering all contribute to the reference bands. Their drum EQ cannot be inferred from this difference.',
        'scope': 'full_mix_context_only'})
    return findings, [
        {'title': 'Test weight versus definition first', 'proposal':
            'Compare a gentle −1 to −2 dB low shelf around 100–120 Hz against unchanged KIT, at matched KIT loudness. '
            'Evaluate kick weight and attack in the mix. This is a hypothesis prompted by the weight/body distribution, not a proven correction.'},
        {'title': 'Test attack separately if it is still difficult to follow', 'proposal':
            'Compare a broad +1 to +2 dB lift around 2–4 kHz separately. On the combined KIT it also affects snare and cymbals. '
            'If cymbals become harsh before drum definition improves, inspect the drum module/submix balance rather than keep boosting.'},
        {'title': 'Leave compression decisions separate from output gain', 'proposal':
            'The present passage does not demonstrate meaningful dynamic control by this chain. '
            'Use aligned, loudness-matched transient measurements and a louder passage before changing threshold, attack or ratio.'},
        {'title': 'Investigate peaks before making narrow cuts', 'proposal':
            'The listed spectral peaks locate energy concentrations. Confirm persistence and unwanted ringing across more than one passage before choosing a notch; drum fundamentals are not defects.'},
    ]


def build_package(signals, output, *, ffmpeg, context):
    if set(signals) != set(IDS) or output.exists():
        raise ValueError("Supply all five diagnostic signals and a new output directory")
    arrays = {k: audio_array(v) for k, v in signals.items()}
    output.mkdir(parents=True)
    measurements, loudness, matched, hashes = {}, {}, {}, {}
    for name, a in arrays.items():
        measurements[name] = analyze_signal(a)
        temporary = output/f'.{name}.wav';sf.write(temporary, a, RATE, subtype='FLOAT')
        loudness[name] = scan_loudness(temporary, ffmpeg);temporary.unlink()
    target = min(-24.0, *(-3-m['true_peak_dbtp']+m['integrated_lufs'] for m in loudness.values()))
    for name, a in arrays.items():
        gain = target-loudness[name]['integrated_lufs'];path = output/AUDIO[f'diagnostic_{name}_matched']
        for _ in range(3):
            sf.write(path, a*10**(gain/20), RATE, subtype='FLOAT')
            metric = scan_loudness(path, ffmpeg)
            if abs(metric['integrated_lufs']-target) <= .11:
                break
            gain += target-metric['integrated_lufs']
        if abs(metric['integrated_lufs']-target) > .15 or metric['true_peak_dbtp'] > -2.85:
            raise ValueError("Diagnostic listening copies failed verified scalar loudness matching")
        matched[name] = metric | {'gain_db': gain};hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {'kind': 'wcfi_kit_diagnostic', 'title': 'October 11 · KIT diagnostic',
        'audio': AUDIO, 'measurements': measurements, 'loudness': loudness, 'matched': matched,
        'target_lufs': target, 'pair': compare_pair(arrays['raw'], arrays['processed']),
        'context': context, 'literature': SOURCES, 'processing_applied': False,
        'reference_scope': 'full_mix_context_only', 'audio_models_run': 0,
        'method': 'Stereo channel-power Welch spectra at 5.86 Hz bin spacing; integrated 1/3-octave power normalized to total below 20 kHz. 10 ms RMS envelopes. Raw-selected paired energy events; no source separation, true SNR, perceptual masking score or quality score.',
        'limitations': ['Bethel and Maverick are complete released/broadcast mixes, not isolated drum buses. Their spectra and crest factors are not KIT EQ or compressor targets.',
            'The WCFI context is the current RED/KIT/BASS foundation; other source and return faders are down. It is not the completed band mix.',
            'The three recordings have different arrangements, song keys, tempos and processing; cross-reference differences are descriptive.',
            'Local PSD peaks are not confirmed resonances; energy-rise candidates do not identify individual drums. No clean/noise reference exists for true SNR.',
            'Pre/post Analysis Nodes precede KIT fader and pan; current Master context includes its saved fader gain. Alignment of repeated periods is not latency.']}
    report['findings'], report['next_tests'] = diagnostic_findings(report)
    package = {'kind': 'wcfi_kit_diagnostic_package', 'schema_version': 1, 'reports': {'KIT': report}, 'files': hashes}
    return package


def validate_package(folder):
    """Read a finite, checksummed diagnostic package; no arbitrary asset paths."""
    folder = Path(folder).resolve();path = folder/'study.json'
    size = path.stat().st_size
    if size > 4*1024*1024:
        raise ValueError("Diagnostic manifest is too large")
    if path.resolve().parent != folder:
        raise ValueError("Diagnostic manifest escaped its staging folder")
    raw = path.read_bytes();package = json.loads(raw)
    if size > 1024*1024 and package.get('kind') != 'wcfi_full_mix_combined_package':
        raise ValueError("Diagnostic manifest is too large")
    # Reject NaN/Infinity anywhere, even fields not currently rendered.
    json.dumps(package, allow_nan=False)
    if package.get('kind') == 'wcfi_full_mix_combined_package':
        from .full_mix_combined import validate_package as validate_full_mix_combined

        return validate_full_mix_combined(folder, package, raw)
    if package.get('kind') == 'wcfi_source_balance_benchmark_package':
        from .balance_benchmark import validate_package as validate_source_balance_benchmark

        return validate_source_balance_benchmark(folder, package, raw)
    if package.get('kind') == 'wcfi_full_mix_benchmark_package':
        from .full_mix_benchmark import validate_package as validate_full_mix_benchmark

        return validate_full_mix_benchmark(folder, package, raw)
    if package.get('kind') == 'wcfi_full_band_package':
        from .full_band import validate_package as validate_full_band

        return validate_full_band(folder, package, raw)
    if package.get('kind') == 'wcfi_updated_reference_package':
        from .updated_reference import validate_package as validate_updated_reference

        return validate_updated_reference(folder, package, raw)
    if package.get('kind') == 'wcfi_red_lowmid_package':
        from .red_lowmid import validate_package as validate_red_lowmid

        return validate_red_lowmid(folder, package, raw)
    if package.get('kind') == 'wcfi_kit_bass_package':
        from .kit_bass_study import validate_package as validate_overlap

        return validate_overlap(folder, package, raw)
    if package.get('kind') == 'wcfi_kit_compression_package':
        from .kit_compression import validate_package as validate_compression

        return validate_compression(folder, package, raw)
    if package.get('kind') in ('wcfi_kit_shelf_package', 'wcfi_kit_presence_package', 'wcfi_kit_combined_package'):
        from .kit_shelf import validate_package as validate_shelf

        return validate_shelf(folder, package, raw)
    if package.get('kind') != 'wcfi_kit_diagnostic_package' or package.get('schema_version') != 1:
        raise ValueError("Unsupported KIT diagnostic package")
    report = package['reports']['KIT']
    if (set(package['reports']) != {'KIT'} or report.get('kind') != 'wcfi_kit_diagnostic'
            or report.get('audio') != AUDIO or report.get('processing_applied') is not False
            or report.get('reference_scope') != 'full_mix_context_only' or report.get('audio_models_run') != 0
            or not isinstance(report.get('title'), str) or not 1 <= len(report['title']) <= 100
            or set(package['files']) != set(AUDIO.values())):
        raise ValueError("Diagnostic scope, title or assets are invalid")
    target = report['target_lufs']
    if type(target) not in (float, int) or not math.isfinite(target) or not -60 <= target <= -24:
        raise ValueError("Invalid diagnostic matching target")
    if set(report['measurements']) != set(IDS) or set(report['matched']) != set(IDS):
        raise ValueError("Diagnostic signal observations are incomplete")
    start = report['context']['start_seconds']
    if type(start) not in (float, int) or not 0 <= start <= 86400:
        raise ValueError("Invalid diagnostic passage")
    if not report.get('findings') or not report.get('next_tests'):
        raise ValueError("Diagnostic needs explicit evidence and proposed next tests")
    for name in IDS:
        duration = report['measurements'][name]['duration_seconds'];m = report['matched'][name]
        if type(duration) not in (float, int) or not .5 <= duration <= 20:
            raise ValueError("Invalid diagnostic duration")
        if abs(m['integrated_lufs']-target) > .15 or m['true_peak_dbtp'] > -2.85:
            raise ValueError("Diagnostic matching is unverified")
        filename = AUDIO[f'diagnostic_{name}_matched'];asset = (folder/filename).resolve()
        if asset.parent != folder or asset.stat().st_size > 16*1024*1024:
            raise ValueError("Diagnostic asset escaped its bounded location")
        info = sf.info(asset)
        if info.samplerate != RATE or info.channels != 2 or info.frames != round(duration*RATE):
            raise ValueError("Diagnostic asset format differs from its manifest")
        samples, _ = sf.read(asset, dtype='float32', always_2d=True)
        if not np.isfinite(samples).all() or hashlib.sha256(asset.read_bytes()).hexdigest() != package['files'][filename]:
            raise ValueError("Diagnostic audio is non-finite or its checksum changed")
    return package, hashlib.sha256(raw).hexdigest()
