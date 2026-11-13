"""Reference-free physical signal evidence. Alerts are not musical quality scores."""
from __future__ import annotations

RULES = {'version': 2, 'window_seconds': 2, 'minimum_seconds': 8,
         'mains_harmonics': 5, 'mains_line_power_fraction': .02,
         'mains_neighbor_prominence_db': 10., 'persistent_block_fraction': .75,
         'flat_top_fraction': .0001, 'flat_relative_difference': 1e-6,
         'minimum_flat_run_samples': 3,
         'long_line_search_hz': .15, 'long_neighbor_min_hz': .3,
         'long_neighbor_max_hz': 1., 'minimum_prominent_harmonics': 2,
         'scope': 'Suspicious stationary mains lines / flat peaks; not confirmed hum or professional mix quality'}


def analyze(samples, rate):
    """Only candidate samples, no clean reference or learned score needed.

    Two-second Hann windows resolve 50/60 Hz families from nearby pitched notes.
    Require coherent line power, neighbor prominence and persistence together.
    Exact mains-frequency instruments/test tones remain inherently ambiguous.
    """
    import numpy as np
    from scipy.fft import rfft, rfftfreq

    x = np.asarray(samples, dtype='float32')
    if x.ndim == 1:
        x = x[:, None]
    if (x.ndim != 2 or x.shape[1] not in (1, 2) or not 16000 <= rate <= 96000
            or not rate*8 <= len(x) <= rate*120 or not np.isfinite(x).all()):
        raise ValueError('Use finite mono/stereo candidate audio, 8–120 seconds at 16–96 kHz')
    rms = float(np.sqrt(np.mean(x.astype('float64')**2)))
    if rms < 1e-8:
        return {'status': 'insufficient_signal', 'alerts': [], 'metrics': {}, 'professional_quality_score': None}
    y = x/rms
    width = round(rate*RULES['window_seconds'])
    blocks = y[:len(y)//width*width].reshape(-1, width, x.shape[1])
    window = np.hanning(width).astype('float32')
    fft = rfft(blocks*window[None, :, None], axis=1, workers=1)
    # Sum of squared sinusoid amplitudes /2 estimates power in coherent lines.
    powers = (fft.real**2+fft.imag**2)*2/(float(window.sum())**2)
    power = np.mean(powers, axis=2)
    total = np.mean(blocks.astype('float64')**2, axis=(1, 2))
    frequencies = rfftfreq(width, 1/rate)
    # Full-excerpt resolution distinguishes persistent narrow lines from nearby
    # bass notes that can dominate the coarse block's neighbor bins.
    long_window = np.hanning(len(y)).astype('float32')
    long_fft = rfft(y*long_window[:, None], axis=0, workers=1)
    long_power = np.mean(long_fft.real**2+long_fft.imag**2, axis=1)
    long_frequencies = rfftfreq(len(y), 1/rate)
    families = {}
    alerts = []
    for mains in (50, 60):
        centers, neighbors = np.zeros(len(blocks)), np.zeros(len(blocks))
        harmonic_prominence = []
        for harmonic in range(1, RULES['mains_harmonics']+1):
            f = mains*harmonic
            centers += power[:, np.argmin(np.abs(frequencies-f))]
            indices = [np.argmin(np.abs(frequencies-(f+offset))) for offset in (-4, -3, -2, 2, 3, 4)]
            neighbors += np.mean(power[:, indices], axis=1)
            distance = np.abs(long_frequencies-f)
            line = float(np.max(long_power[distance <= RULES['long_line_search_hz']]))
            nearby = float(np.mean(long_power[(distance >= RULES['long_neighbor_min_hz'])
                                             & (distance <= RULES['long_neighbor_max_hz'])]))
            harmonic_prominence.append(float(10*np.log10(max(line, 1e-20)/max(nearby, 1e-20))))
        fraction = centers/np.maximum(total, 1e-20)
        prominence = 10*np.log10(np.maximum(centers, 1e-20)/np.maximum(neighbors, 1e-20))
        prominent_count = sum(v >= RULES['mains_neighbor_prominence_db'] for v in harmonic_prominence)
        hits = ((fraction >= RULES['mains_line_power_fraction'])
                & (prominent_count >= RULES['minimum_prominent_harmonics']))
        persistence = float(np.mean(hits))
        families[str(mains)] = {'median_line_fraction': float(np.median(fraction)),
                               'median_neighbor_prominence_db': float(np.median(prominence)),
                               'long_harmonic_prominence_db': harmonic_prominence,
                               'prominent_harmonics': prominent_count,
                               'persistent_block_fraction': persistence}
        if persistence >= RULES['persistent_block_fraction']:
            alerts.append({'type': 'suspected_mains', 'frequency_hz': mains,
                'note': 'Persistent coherent mains-frequency lines; an intentional exact-frequency tone can imitate this. Inspect source/power/cabling.'})
    peak = float(np.max(np.abs(y)))
    equal = (np.abs(np.diff(y, axis=0)) <= peak*RULES['flat_relative_difference'])
    tops = np.abs(y[1:]) >= peak*.95
    flat = equal & tops
    flat_fraction = float(np.mean(flat[1:] & flat[:-1]))
    if flat_fraction >= RULES['flat_top_fraction']:
        alerts.append({'type': 'flat_peaks', 'note': 'Repeated flat peaks suggest clipping or hard limiting; prior lossy encoding can conceal flat tops.'})
    return {'status': 'measured', 'alerts': alerts,
            'metrics': {'mains': families, 'flat_top_fraction': flat_fraction,
                        'analyzed_seconds': len(blocks)*width/rate, 'blocks': len(blocks)},
            'professional_quality_score': None, 'auto_fix_authorized': False}
