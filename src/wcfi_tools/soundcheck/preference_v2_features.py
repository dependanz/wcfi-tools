"""Reference-free, gain-invariant artifact descriptors; no semantic quality claims."""
from __future__ import annotations

import itertools
import math

from .preference_features import FEATURE_NAMES, extract, spectrum, validate_audio

DETAIL_BANDS = (20, 120, 500, 2000, 5000, 10000, 16000, 20000)
DETAIL_NAMES = tuple(f'flatness_{a}_{b}_{q}' for a, b in itertools.pairwise(DETAIL_BANDS)
                     for q in ('p10', 'p50', 'p90')) + (
    'rolloff_95', 'rolloff_99', 'hole_depth_p90', 'hole_depth_p99', 'hole_fraction',
    'narrow_peak_p90', 'narrow_peak_p99', 'peak_temporal_instability',
    'bandwidth_drop_p95', 'envelope_rise_p90', 'envelope_rise_p99',
    'onset_pre_energy', 'onset_post_energy', 'onsets_per_second',
    'relative_flat_top_fraction', 'mains50_concentration', 'mains60_concentration')
ALL_NAMES = FEATURE_NAMES + DETAIL_NAMES


def extract_native(audio, rate):
    """Only the candidate waveform is accepted; no reference or condition labels."""
    import numpy as np
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks, resample_poly, welch

    original = validate_audio(audio, rate)
    if rate < 40000:
        raise ValueError('Native artifact features require at least 40 kHz to assess bandwidth')
    rms = np.sqrt(np.mean(original.astype('float64')**2))
    x = original/rms
    divisor = math.gcd(rate, 24000)
    low_rate = resample_poly(original, 24000//divisor, rate//divisor).astype('float32') if rate != 24000 else original
    base, health = extract(low_rate, 24000)
    power, frequency = spectrum(x, rate, width=4096, hop=2048)
    active = power.sum(axis=1) > power.sum(axis=1).max()*1e-6
    power = power[active]
    flatness = []
    for a, b in itertools.pairwise(DETAIL_BANDS):
        band = power[:, (frequency >= a) & (frequency < b)]
        values = np.exp(np.log(np.maximum(band, 1e-20)).mean(axis=1))/np.maximum(band.mean(axis=1), 1e-20)
        flatness.extend(np.quantile(values, [.1, .5, .9]).tolist())
    selected = (frequency >= 20) & (frequency <= 20000)
    spectral = power[:, selected]
    selected_f = frequency[selected]
    cumulative = spectral.cumsum(axis=1)/np.maximum(spectral.sum(axis=1, keepdims=True), 1e-20)
    rolloffs = [float(np.mean(selected_f[np.argmax(cumulative >= q, axis=1)])/20000) for q in (.95, .99)]
    # Local log-spectrum curvature makes gaps/isolated tones visible, without a clean target.
    floor = np.maximum(spectral.max(axis=1, keepdims=True)*1e-8, 1e-20)
    log_power = 10*np.log10(np.maximum(spectral, floor))
    smooth = gaussian_filter1d(log_power, 8, axis=1, mode='nearest')
    holes = np.maximum(smooth-log_power, 0)
    peaks = np.maximum(log_power-smooth, 0)
    unstable = np.abs(np.diff(peaks, axis=0))
    mean_log = 10*np.log10(np.maximum(spectral.mean(axis=0), 1e-20))
    broad = gaussian_filter1d(mean_log, 8, mode='nearest')
    drops = np.maximum(broad[:-16]-broad[16:], 0)
    curvature = [float(np.quantile(holes, .9)), float(np.quantile(holes, .99)), float(np.mean(holes > 12)),
                 float(np.quantile(peaks, .9)), float(np.quantile(peaks, .99)),
                 float(np.mean(unstable)) if len(unstable) else 0., float(np.quantile(drops, .95))]
    block = round(rate*.005)
    envelope = np.mean(x[:len(x)//block*block].reshape(-1, block, x.shape[1]).astype('float64')**2, axis=(1, 2))
    log_envelope = 10*np.log10(np.maximum(envelope, 1e-12))
    rise = np.maximum(np.diff(log_envelope), 0)
    attacks, _ = find_peaks(rise, height=4, distance=20)
    pre, post = [], []
    for index in attacks:
        if index < 8 or index+24 >= len(envelope):
            continue
        peak = max(float(np.max(envelope[index:index+5])), 1e-20)
        pre.append(min(4., float(np.mean(envelope[index-8:index-2]))/peak))
        post.append(min(4., float(np.mean(envelope[index+5:index+24]))/peak))
    dynamics = [float(np.quantile(rise, .9)), float(np.quantile(rise, .99)),
                float(np.median(pre)) if pre else 0., float(np.median(post)) if post else 0.,
                len(pre)/(len(x)/rate)]
    maximum = float(np.max(np.abs(x)))
    flat = (np.abs(np.diff(x, axis=0)) < maximum*1e-5) & (np.abs(x[1:]) > maximum*.95)
    flat_fraction = float(np.mean(flat[1:] & flat[:-1]))
    # Narrow mains harmonics compared with neighboring frequencies; tonal music can mimic these.
    factor = max(1, rate//6000)
    slow = resample_poly(x, 1, factor)
    f, psd = welch(slow, fs=rate/factor, nperseg=8192, noverlap=4096, axis=0)
    psd = np.mean(psd, axis=1)
    hum = []
    for mains in (50, 60):
        center, neighbor = 0., 0.
        for harmonic in range(1, 9):
            distance = np.abs(f-mains*harmonic)
            center += float(np.mean(psd[distance < 1.5]))
            neighbor += float(np.mean(psd[(distance > 4) & (distance < 10)]))
        hum.append(float(np.log10(max(center, 1e-20)/max(neighbor, 1e-20))))
    values = flatness + rolloffs + curvature + dynamics + [flat_fraction] + hum
    details = dict(zip(DETAIL_NAMES, map(float, values)))
    if len(values) != len(DETAIL_NAMES) or not np.isfinite(values).all():
        raise ValueError('Invalid no-reference artifact feature vector')
    return base | details, health | {'native_rate': rate, 'native_sample_peak': float(np.max(np.abs(original))),
        'native_full_scale_fraction': float(np.mean(np.abs(original) >= 1))}
