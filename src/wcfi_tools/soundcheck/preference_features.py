"""Small deterministic stereo features. No models, devices, DAW or network IO.

These are descriptors, not universal EQ targets or a professional-quality score.
Global signal level is deliberately absent from the preference feature vector.
"""
from __future__ import annotations

import itertools
import math

BAND_EDGES = (20, 60, 120, 250, 500, 1000, 2000, 4000, 8000, 12001)
FEATURE_NAMES = tuple(
    f"band_{a}_{b}_{stat}" for stat in ("mean_db", "std_db")
    for a, b in itertools.pairwise(BAND_EDGES)
) + ("crest_db", "level_p90_p10_db", "level_p50_p10_db", "level_p90_p50_db",
     "spectral_flux_mean", "spectral_flux_p90", "flatness_mean", "flatness_std",
     "centroid_mean", "centroid_std", "lr_correlation", "side_energy_fraction",
     "absolute_lr_balance", "lr_correlation_std")
ARTIFACT_NAMES = ("log_spectral_error", "spectral_distribution_error",
                  "envelope_error", "envelope_correlation_loss", "waveform_error",
                  "lr_correlation_error", "side_fraction_error", "band_shape_error")


def validate_audio(audio, rate):
    import numpy as np

    x = np.asarray(audio, dtype="float32")
    if x.ndim == 1:
        x = x[:, None]
    if (x.ndim != 2 or x.shape[1] not in (1, 2) or not 16000 <= rate <= 96000
            or not rate <= len(x) <= rate * 120 or not np.isfinite(x).all()):
        raise ValueError("Use finite mono/stereo excerpts of 1–120 seconds, 16–96 kHz")
    if float(np.mean(x.astype('float64')**2)) < 1e-14:
        raise ValueError("Silent audio has no supported preference representation")
    return x


def load_audio(path):
    import soundfile as sf
    from scipy.signal import resample_poly

    info = sf.info(path)
    if not 1 <= info.duration <= 120 or info.channels not in (1, 2):
        raise ValueError("Audio exceeds the excerpt/channel budget")
    x, rate = sf.read(path, dtype="float32", always_2d=True)
    x = validate_audio(x, rate)
    if rate != 24000:
        divisor = math.gcd(rate, 24000)
        x = resample_poly(x, 24000//divisor, rate//divisor).astype('float32')
    return x, 24000


def spectrum(audio, rate, *, width=2048, hop=512):
    """Bound FFT scratch memory by batching, preserve channel power (not mono sum)."""
    import numpy as np
    from scipy.fft import rfft, rfftfreq

    frames = np.lib.stride_tricks.sliding_window_view(audio, width, axis=0)[::hop]
    window = np.hanning(width).astype('float32')
    pieces = []
    for start in range(0, len(frames), 128):
        value = rfft(frames[start:start+128] * window, axis=-1, workers=1)
        pieces.append(np.mean(value.real**2 + value.imag**2, axis=1))
    return np.concatenate(pieces), rfftfreq(width, 1/rate)


def extract(audio, rate):
    import numpy as np

    x = validate_audio(audio, rate)
    # Remove overall gain before every learned descriptor. Keep level only as a baseline.
    rms = float(np.sqrt(np.mean(x.astype('float64')**2)))
    normalized = x/rms
    power, frequencies = spectrum(normalized, rate)
    valid = (frequencies >= 20) & (frequencies < 12001)
    total = np.maximum(np.sum(power[:, valid], axis=1), 1e-20)
    bands = np.array([np.sum(power[:, (frequencies >= a) & (frequencies < b)], axis=1)
                      for a, b in itertools.pairwise(BAND_EDGES)]).T
    proportions = np.maximum(bands/total[:, None], 1e-10)
    band_db = 10*np.log10(proportions)
    active = total > np.max(total)*1e-6
    band_db = band_db[active]
    distribution = power[:, valid]/total[:, None]
    flux = np.sum(np.maximum(np.diff(distribution, axis=0), 0), axis=1)
    flatness = np.exp(np.mean(np.log(np.maximum(power[:, valid], 1e-20)), axis=1))/np.maximum(
        np.mean(power[:, valid], axis=1), 1e-20)
    centroid = distribution @ (frequencies[valid]/12000)
    frame = max(1, round(rate*.05))
    short = normalized[:len(normalized)//frame*frame].reshape(-1, frame, x.shape[1])
    levels = 10*np.log10(np.maximum(np.mean(short.astype('float64')**2, axis=(1, 2)), 1e-12))
    p10, p50, p90 = np.quantile(levels, [.1, .5, .9])
    if x.shape[1] == 2:
        left, right = normalized[:, 0].astype('float64'), normalized[:, 1].astype('float64')
        energy_l, energy_r = np.mean(left**2), np.mean(right**2)
        correlation = np.mean(left*right)/max(np.sqrt(energy_l*energy_r), 1e-20)
        side = np.mean((left-right)**2)/max(2*(energy_l+energy_r), 1e-20)
        balance = abs(energy_l-energy_r)/(energy_l+energy_r)
        l, r = short[:, :, 0].astype('float64'), short[:, :, 1].astype('float64')
        correlations = np.mean(l*r, axis=1)/np.maximum(np.sqrt(np.mean(l*l, axis=1)*np.mean(r*r, axis=1)), 1e-20)
        corr_std = np.std(correlations)
    else:
        correlation, side, balance, corr_std = 1., 0., 0., 0.
    values = np.r_[np.mean(band_db, axis=0), np.std(band_db, axis=0),
        20*np.log10(np.max(np.abs(normalized))), p90-p10, p50-p10, p90-p50,
        np.mean(flux), np.quantile(flux, .9), np.mean(flatness[active]), np.std(flatness[active]),
        np.mean(centroid[active]), np.std(centroid[active]), correlation, side, balance, corr_std]
    if len(values) != len(FEATURE_NAMES) or not np.isfinite(values).all():
        raise ValueError("Non-finite or mismatched feature vector")
    return dict(zip(FEATURE_NAMES, map(float, values))), {
        "rms_dbfs": 20*math.log10(rms), "seconds": len(x)/rate,
        "sample_peak_dbfs": 20*math.log10(max(float(np.max(np.abs(x))), 1e-20)),
        "at_or_above_full_scale_fraction": float(np.mean(np.abs(x) >= 1)),
        "not_true_peak": True}


def artifact_errors(audio, reference, rate):
    """Full-reference fidelity distances; not a veto on creative mix differences.

    ODAQ conditions are supplied aligned. Global RMS is removed so a level-only
    difference is not learned as an artifact. Absolute clipping is separate health.
    """
    import numpy as np

    x, ref = validate_audio(audio, rate), validate_audio(reference, rate)
    if x.shape != ref.shape:
        raise ValueError("Artifact comparisons need aligned excerpts of identical shape")
    x = x/np.sqrt(np.mean(x.astype('float64')**2))
    ref = ref/np.sqrt(np.mean(ref.astype('float64')**2))
    a, frequencies = spectrum(x, rate)
    b, _ = spectrum(ref, rate)
    valid = frequencies >= 20
    a, b = a[:, valid], b[:, valid]
    floor = max(float(np.max(b))*1e-6, 1e-12)
    db_a, db_b = 10*np.log10(np.maximum(a, floor)), 10*np.log10(np.maximum(b, floor))
    log_error = np.mean(np.abs(db_a-db_b))/60
    dist_a = a/np.maximum(a.sum(axis=1, keepdims=True), 1e-20)
    dist_b = b/np.maximum(b.sum(axis=1, keepdims=True), 1e-20)
    distribution_error = np.mean(np.sum(np.abs(dist_a-dist_b), axis=1))/2
    envelope_a, envelope_b = np.sqrt(a.sum(axis=1)), np.sqrt(b.sum(axis=1))
    envelope_a /= max(float(np.sqrt(np.mean(envelope_a**2))), 1e-20)
    envelope_b /= max(float(np.sqrt(np.mean(envelope_b**2))), 1e-20)
    envelope_error = np.sqrt(np.mean((envelope_a-envelope_b)**2))
    denominator = np.linalg.norm(envelope_a-envelope_a.mean())*np.linalg.norm(envelope_b-envelope_b.mean())
    corr = (np.dot(envelope_a-envelope_a.mean(), envelope_b-envelope_b.mean())/denominator
            if denominator > 1e-12 else float(np.allclose(envelope_a, envelope_b)))
    waveform_error = np.sqrt(np.mean((x.astype('float64')-ref)**2))
    metrics_a, _ = extract(x, rate)
    metrics_b, _ = extract(ref, rate)
    band_shape = np.mean([abs(metrics_a[n]-metrics_b[n]) for n in FEATURE_NAMES[:9]])/60
    values = [log_error, distribution_error, envelope_error, max(0, 1-corr), waveform_error,
              abs(metrics_a['lr_correlation']-metrics_b['lr_correlation']),
              abs(metrics_a['side_energy_fraction']-metrics_b['side_energy_fraction']), band_shape]
    if not np.isfinite(values).all():
        raise ValueError("Invalid artifact errors")
    return dict(zip(ARTIFACT_NAMES, map(float, values)))
