"""Bounded local passage candidates; energy and onset patterns are not song labels."""
from __future__ import annotations

import numpy as np
import soundfile as sf
from scipy.signal import find_peaks


def select_passages(path, *, seconds=12):
    """Scan small blocks, then choose three disjoint active passages without a model.

    Loud does not mean chorus, and busy does not mean fill. Keep that uncertainty
    alongside the measurements instead of presenting an inferred song structure.
    """
    if type(seconds) not in (float, int) or not 8 <= seconds <= 20:
        raise ValueError("Choose an 8–20 second comparison window")
    levels, peaks = [], []
    with sf.SoundFile(path) as source:
        rate = source.samplerate
        hop = round(rate*.02)
        for block in source.blocks(blocksize=hop, dtype="float32", always_2d=True):
            if len(block) != hop:
                break
            levels.append(10*np.log10(max(float(np.mean(block.astype(float)**2)), 1e-12)))
            peaks.append(float(np.max(np.abs(block))))
    levels, peaks = np.asarray(levels), np.asarray(peaks)
    attacks, _ = find_peaks(levels, prominence=4, distance=4)
    count = round(seconds/.02)
    candidates = []
    for start in range(0, len(levels)-count+1, 100):
        window = levels[start:start+count]
        hits = attacks[(attacks >= start) & (attacks < start+count)]
        if len(hits) < 8 or np.mean(window > -45) < .5:
            continue
        intervals = np.diff(hits)*.02
        density = np.histogram((hits-start)*.02, bins=np.arange(0, seconds+2, 2))[0]
        candidates.append({"start_seconds": round(start*.02, 2), "duration_seconds": seconds,
            "rms_dbfs": round(float(10*np.log10(np.mean(10**(window/10)))), 2),
            "detected_energy_attacks": len(hits),
            "attack_interval_cv": round(float(np.std(intervals)/max(np.mean(intervals), .02)), 3),
            "peak_two_second_attack_count": int(max(density)),
            "sample_peak_dbfs": round(float(20*np.log10(max(peaks[start:start+count].max(), 1e-6))), 2)})
    if len(candidates) < 3:
        raise ValueError("Not enough active drum passages for three comparisons")
    quiet_target = np.percentile([c["rms_dbfs"] for c in candidates], 15)
    quiet = min(candidates, key=lambda c: abs(c["rms_dbfs"]-quiet_target))

    def separated(candidate, selected):
        return all(abs(candidate["start_seconds"]-s["start_seconds"]) >= seconds+2 for s in selected)

    loud_pool = [c for c in candidates if separated(c, [quiet])]
    loud = max(loud_pool, key=lambda c: c["rms_dbfs"])
    busy_pool = [c for c in candidates if separated(c, [quiet, loud])]
    if not busy_pool:
        raise ValueError("Not enough separated active windows")
    busy = max(busy_pool, key=lambda c: c["peak_two_second_attack_count"] + c["attack_interval_cv"]*3)
    return [{"id": identity, "label": label, **candidate,
             "selection_method": "20 ms energy / heuristic energy-attack candidates, not source separation",
             "musical_label_verified": False, "selection_note": note}
            for identity, label, candidate, note in (
                ("quiet", "Quieter playing", quiet, "Active passage near the lower 15th percentile of KIT window levels; not silence."),
                ("loud", "Loud section · chorus candidate", loud, "Highest measured KIT window level outside the quiet passage; chorus identity is unverified."),
                ("fills", "Busy playing · fill candidate", busy, "Dense, irregular energy attacks; musical fills are unverified."))]
