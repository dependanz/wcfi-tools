"""Conservative waveform alignment for independently observed audio streams."""
from __future__ import annotations

import numpy as np
from scipy.signal import correlate, resample_poly

RATE = 48000


def estimate_delay(source, target, *, rate=RATE, max_ms=500):
    """Overlap-normalized waveform correlation. Positive = target trails source.

    This estimates observed waveform alignment, not a plugin's declared latency.
    Narrowband/repetitive signals and weak correlations produce no lag result.
    """
    count = min(len(source), len(target))
    if count < rate // 2:
        return {"available": False, "reason": "Collecting audio"}
    x, y = np.asarray(source[-count:]), np.asarray(target[-count:])
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        return {"available": False, "reason": "Invalid samples"}
    if min(np.sqrt(np.mean(x.astype(float) ** 2)), np.sqrt(np.mean(y.astype(float) ** 2))) < 1e-5:
        return {"available": False, "reason": "Signal too quiet"}
    # Anti-alias before reducing the search to 6 kHz (0.167 ms resolution).
    x, y = resample_poly(x.astype(float), 1, 8), resample_poly(y.astype(float), 1, 8)
    x -= np.mean(x)
    y -= np.mean(y)
    size = len(x)
    bound = min(round(max_ms * rate / 8000), size // 3)
    lags = np.arange(-bound, bound + 1)
    lo_x, hi_x = np.maximum(0, -lags), np.minimum(size, size - lags)
    lo_y, hi_y = np.maximum(0, lags), np.minimum(size, size + lags)
    n = hi_x - lo_x

    def sums(v, lo, hi):
        prefix = np.concatenate(([0.0], np.cumsum(v)))
        return prefix[hi] - prefix[lo]

    sx, sy = sums(x, lo_x, hi_x), sums(y, lo_y, hi_y)
    ex = sums(x * x, lo_x, hi_x) - sx * sx / n
    ey = sums(y * y, lo_y, hi_y) - sy * sy / n
    numerator = correlate(y, x, method="fft")[size - 1 + lags] - sx * sy / n
    coefficients = np.clip(numerator / np.sqrt(np.maximum(ex * ey, 1e-30)), -1, 1)
    best = int(np.argmax(np.abs(coefficients)))
    score = float(abs(coefficients[best]))
    away = np.abs(lags - lags[best]) > round(.003 * rate / 8)
    second = float(np.max(np.abs(coefficients[away]), initial=0))
    reason = "Weak waveform match" if score < .55 else "Repetitive / ambiguous waveform" if score - second < .015 else None
    return {"available": reason is None, "lag_ms": float(lags[best] * 8000 / rate) if reason is None else None,
            "correlation": score, "peak_separation": score - second,
            "polarity": "inverted" if coefficients[best] < 0 else "same", "reason": reason,
            "resolution_ms": 8000 / rate}
