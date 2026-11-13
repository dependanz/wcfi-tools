"""Bounded analysis of raw mono/stereo rehearsal recordings.

Measures each channel independently; it does not claim to separate bleed from
the intended performer, infer musical balance, or repair upstream clipping.
"""

from __future__ import annotations

from pathlib import Path

from .profiles import ROLES, file_hash, number


def analyze_audio(path: Path, role: str, *, seconds: float = 90, start: float = 0,
                  max_boost_db: float = 12, max_cut_db: float = 24,
                  peak_ceiling_dbfs: float = -12, eq_mode: str = "gentle_cuts") -> dict:
    import numpy as np
    import soundfile as sf
    from scipy.signal import welch

    number(seconds, "seconds", 10, 300)
    number(start, "start", 0, 86400)
    number(max_boost_db, "max_boost_db", 0, 12)
    number(max_cut_db, "max_cut_db", 0, 24)
    number(peak_ceiling_dbfs, "peak_ceiling_dbfs", -24, -6)
    if role not in ROLES or eq_mode not in {"highpass", "gentle_cuts"}:
        raise ValueError("Unsupported source role or EQ mode")
    before = path.stat()
    digest = file_hash(path)
    with sf.SoundFile(path) as stream:
        rate = stream.samplerate
        if stream.channels not in {1, 2} or not 16000 <= rate <= 192000:
            raise ValueError("Use a mono/stereo file sampled at 16–192 kHz")
        stream.seek(min(int(start * rate), len(stream)))
        audio = stream.read(int(seconds * rate), dtype="float32", always_2d=True)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Recording is still changing; stop recording before calibration")
    result = {"file": str(path.resolve()), "audio_sha256": digest, "role": role,
              "status": "blocked", "reasons": [], "notes": [], "gain_db": None,
              "eq": [], "sample_rate": rate, "channels": audio.shape[1], "start_seconds": start,
              "duration_seconds": round(len(audio) / rate, 3)}
    if len(audio) < rate * 10:
        result["reasons"].append("At least 10 seconds of recording is required")
        return result
    if not np.isfinite(audio).all():
        result["reasons"].append("Audio contains non-finite samples")
        return result
    frame_size = int(rate * .25)
    count = len(audio) // frame_size
    frames = audio[:count * frame_size].reshape(count, frame_size, audio.shape[1])
    power = np.mean(frames.astype(np.float64) ** 2, axis=1)
    # Use the louder side, not an L+R sum: antiphase audio must not appear silent.
    frame_db = 10 * np.log10(np.maximum(np.max(power, axis=1), 1e-12))
    peak = float(np.max(np.abs(audio)))
    peak_db = float(20 * np.log10(max(peak, 1e-6)))
    threshold = max(-55.0, float(np.percentile(frame_db, 90)) - 18)
    active = frame_db > threshold
    active_seconds = float(np.sum(active) * .25)
    result["metrics"] = {"sample_peak_dbfs": round(peak_db, 2),
                         "active_seconds": active_seconds,
                         "active_fraction": round(float(np.mean(active)), 3),
                         "activity_threshold_dbfs": round(threshold, 2),
                         "near_full_scale_fraction": round(float(np.mean(np.abs(audio) >= .999)), 8)}
    if active_seconds < 8 or np.mean(active) < .1:
        result["reasons"].append("Too little usable signal; quiet audio/bleed is not boosted")
        return result
    active_db = float(np.percentile(frame_db[active], 70))
    result["metrics"]["active_rms_dbfs"] = round(active_db, 2)
    if peak_db >= -.1:
        result["reasons"].append("Input reaches full scale; check mixer/preamp for clipping")
    if active_db < -45:
        result["reasons"].append("Input is too quiet for reliable automatic gain")
    if audio.shape[1] == 2:
        channel_db = 10 * np.log10(np.maximum(np.mean(power[active], axis=0), 1e-12))
        balance = float(channel_db[0] - channel_db[1])
        left, right = audio[:, 0].astype(float), audio[:, 1].astype(float)
        denominator = float(np.sqrt(np.dot(left, left) * np.dot(right, right)))
        correlation = float(np.dot(left, right) / denominator) if denominator > 0 else 0.0
        result["metrics"].update(left_minus_right_db=round(balance, 2),
                                  stereo_correlation=round(correlation, 3))
        if abs(balance) > 6:
            result["reasons"].append("Stereo sides differ by over 6 dB; verify input mapping")
        if correlation < -.25:
            result["reasons"].append("Strong negative stereo correlation; verify polarity/cabling")

    recipe = ROLES[role]
    desired_gain = recipe["target_dbfs"] - active_db
    # Reserve 3 dB beyond measured sample peak for unseen transients/filter changes.
    peak_limited_gain = peak_ceiling_dbfs - 3 - peak_db
    gain = min(desired_gain, max_boost_db, peak_limited_gain)
    if desired_gain > max_boost_db:
        result["reasons"].append("Required boost exceeds limit; improve level at the mixer")
    if gain < -max_cut_db:
        result["reasons"].append("Required attenuation exceeds limit; reduce mixer level")
    if result["reasons"]:
        return result
    result["gain_db"] = round(gain, 2)
    result["status"] = "ready"
    result["metrics"]["estimated_sample_peak_after_gain_dbfs"] = round(peak_db + gain, 2)
    if gain < desired_gain - .1:
        result["notes"].append("Gain reduced to preserve input headroom; target level not reached")
    if recipe["highpass_hz"]:
        result["eq"].append({"kind": "highpass", "frequency_hz": recipe["highpass_hz"],
                             "q": .707, "gain_db": 0})

    indices = np.flatnonzero(active)
    indices = indices[np.linspace(0, len(indices) - 1, min(160, len(indices))).astype(int)]
    frequency, psd = welch(frames[indices], fs=rate, nperseg=min(4096, frame_size), axis=1)
    mean_psd = np.mean(psd, axis=(0, 2))
    centers = np.array([125, 250, 500, 1000, 2000, 4000, 8000])
    energy = np.array([np.sum(mean_psd[(frequency >= c / np.sqrt(2)) &
                                      (frequency < min(c * np.sqrt(2), rate / 2))])
                       for c in centers])
    db = 10 * np.log10(np.maximum(energy, 1e-20))
    normalized = db - np.median(db[2:5])
    result["metrics"]["octave_shape_db"] = [round(float(v), 2) for v in normalized]
    if eq_mode == "gentle_cuts" and recipe["shape"] is not None:
        # A single sustained note is not enough evidence for spectral EQ.
        if float(np.max(energy) / max(np.sum(energy), 1e-20)) > .8:
            result["notes"].append("Narrow-band performance: broad EQ skipped; play varied material")
        else:
            excess = normalized - np.array(recipe["shape"]) - 6
            for i in np.argsort(excess)[::-1][:2]:
                if excess[i] < 2 or centers[i] >= rate / 2:
                    continue
                result["eq"].append({"kind": "bell", "frequency_hz": int(centers[i]),
                                     "q": .7, "gain_db": -round(min(3., float(excess[i]) * .5), 2)})
    result["notes"].append("Full-band bleed cannot be distinguished from the intended source")
    return result
