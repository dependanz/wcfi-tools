"""Bounded, role-specific DSP hypotheses for offline musical evaluation.

No devices, networks or DAW writes. Detector thresholds are versioned engineering
hypotheses, not learned mix preferences. Processing starts from the captured
current chain; it never pretends to emulate a named commercial plug-in.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import butter, lfilter, sosfilt, tf2sos, welch

from .role_analysis import audio_array, features, health, intervals

RATE = 48000
POLICY_VERSION = 1


def descriptors(audio):
    audio = audio_array(audio, RATE)
    f = features(audio, RATE)
    frequency, power = welch(audio, RATE, nperseg=8192, noverlap=4096, axis=0)
    power = np.mean(power, axis=1)
    def band(low, high, density=False):
        selected = power[(frequency >= low) & (frequency < high)]
        return float(np.mean(selected) if density else np.sum(selected))
    total = max(band(20, 20000), 1e-24)
    body = band(200, 500, True)
    neighbours = math.sqrt(max(band(100, 200, True)*band(500, 1200, True), 1e-30))
    levels = f["levels"][f["active"]]
    return {"health": health(f), "low_mid_contrast_db": 10*math.log10(max(body, 1e-24)/neighbours),
        "sub_30_fraction": band(20, 30)/total, "sub_80_fraction": band(20, 80)/total,
        "low_mid_fraction": band(200, 500)/total,
        "upper_level_tail_db": float(np.percentile(levels, 95)-np.percentile(levels, 50)) if len(levels) else 0,
        "active_p80_dbfs": float(np.percentile(levels, 80)) if len(levels) else -120,
        "semantic_activity_verified": False}


def decisions(audio, role, *, kit_context=None):
    """One frozen rule set for all sessions; no per-date threshold tuning."""
    if role not in ("red", "kit"):
        raise ValueError("Choose RED or KIT")
    audio = audio_array(audio, RATE)
    metrics = descriptors(audio)
    healthy_signal = metrics["health"]["sample_peak_dbfs"] > -60
    eq_bands = []
    rumble = metrics["sub_80_fraction"] if role == "red" else metrics["sub_30_fraction"]
    if healthy_signal and rumble > (.025 if role == "red" else .02):
        eq_bands.append({"kind": "highpass", "hz": 65 if role == "red" else 30, "order": 2})
    contrast = metrics["low_mid_contrast_db"]
    if healthy_signal and contrast > 4 and metrics["low_mid_fraction"] > .2:
        eq_bands.append({"kind": "bell", "hz": 300 if role == "red" else 350,
            "gain_db": -min(2.0, max(.75, .25*(contrast-3))), "q": .7})
    eq = {"enabled": bool(eq_bands), "bands": eq_bands,
        "reason": "Audition low-frequency cleanup and/or a broad body cut from measured concentration; musical fundamentals can also satisfy this gate."
                  if eq_bands else "No EQ candidate passes the fixed rumble/body-concentration gates; keep current.",
        "certainty": "listening_hypothesis_not_confirmed_tonal_defect"}
    comp_enabled = (healthy_signal and metrics["health"]["energy_active_fraction"] > .3
                    and metrics["health"]["crest_db"] > (9 if role == "red" else 12)
                    and metrics["upper_level_tail_db"] > 3)
    compression = {"enabled": comp_enabled, "ratio": 2 if role == "red" else 1.5,
        "threshold_dbfs": metrics["active_p80_dbfs"], "knee_db": 6 if role == "red" else 4,
        "attack_ms": 15 if role == "red" else 25, "release_ms": 100 if role == "red" else 120,
        "detector_rms_ms": 5, "maximum_reduction_db": 3 if role == "red" else 2,
        "reason": "Audition gentle reduction of the upper energy tail, with no makeup gain. Attack timing and low reduction caps protect dynamics; source activity remains unverified."
                  if comp_enabled else "Upper-tail/crest/activity gates do not justify this compression candidate; keep current.",
        "stereo_linked": True, "makeup_gain_db": 0, "certainty": "listening_hypothesis_not_preference"}
    deess = {"enabled": False, "events": [], "eligible_events": 0, "rejected_kit_overlap_events": 0,
        "maximum_reduction_db": 2.5, "attack_ms": 2, "release_ms": 35,
        "processor": "wideband_event_gain", "confirmed_sibilance": False,
        "reason": "KIT does not use a vocal sibilance detector." if role == "kit" else "No eligible high-frequency bursts or verified KIT comparison context."}
    if role == "red" and healthy_signal and kit_context is not None:
        kit_context = audio_array(kit_context, RATE)
        if kit_context.shape != audio.shape:
            raise ValueError("RED de-essing needs aligned, matching KIT context")
        f, kit = features(audio, RATE), features(kit_context, RATE)
        active = f["active"]
        baseline = float(np.median(f["high_body_db"][active])) if np.any(active) else 0
        mask = active & (f["high_fraction"] > .2) & (f["high_body_db"] > baseline+4)
        candidates = intervals(mask, f["hop"], RATE, minimum=.03, maximum=.25, offset=f["frame_offset_seconds"])
        # A drum HF burst must be both unusually bright and high in its own
        # level distribution. This is a bleed veto, not source separation.
        kit_hf = kit["bands"]["high"]
        kit_loud = kit_hf > np.percentile(kit_hf[kit["active"]], 75) if np.any(kit["active"]) else np.zeros_like(mask)
        kit_burst = kit["active"] & (kit["high_fraction"] > .15) & kit_loud
        accepted = []
        for event in candidates:
            overlap = float(np.mean(kit_burst[event["start_frame"]:event["end_frame"]]))
            if overlap > .5:
                deess["rejected_kit_overlap_events"] += 1
            else:
                accepted.append(event | {"kit_overlap_fraction": overlap})
        deess.update(enabled=bool(accepted), events=accepted, eligible_events=len(accepted),
            reason="Audition up to 2.5 dB of brief whole-vocal attenuation around eligible HF bursts. This can duck consonants/body and cause lisping; the detector has not confirmed sibilance."
                   if accepted else "All HF candidates were absent, too long or vetoed by KIT overlap; keep current.")
    return {"policy_version": POLICY_VERSION, "role": role, "metrics": metrics,
        "eq": eq, "compression": compression, "deess": deess,
        "application": "local_listening_preview_only", "native_plugin_equivalence": False}


def _bell(hz, gain_db, q):
    a = 10**(gain_db/40)
    omega = 2*math.pi*hz/RATE
    alpha = math.sin(omega)/(2*q)
    cosine = math.cos(omega)
    return tf2sos([1+alpha*a, -2*cosine, 1-alpha*a], [1+alpha/a, -2*cosine, 1-alpha/a])


def equalize(audio, recipe):
    audio = audio_array(audio, RATE)
    if not recipe["enabled"]:
        return audio.copy(), {"enabled": False, "identical_control": True}
    filtered = np.tile(audio, (2, 1)).astype(np.float64)
    if not 1 <= len(recipe["bands"]) <= 2:
        raise ValueError("Use one or two bounded EQ bands")
    for band in recipe["bands"]:
        if band["kind"] == "highpass":
            if band["order"] != 2 or band["hz"] not in (30, 65):
                raise ValueError("Unsupported role high-pass")
            sos = butter(2, band["hz"], fs=RATE, btype="highpass", output="sos")
        elif band["kind"] == "bell":
            if not (200 <= band["hz"] <= 500 and -2 <= band["gain_db"] <= 0 and .5 <= band["q"] <= 1.5):
                raise ValueError("Use a gentle low-mid bell")
            sos = _bell(band["hz"], band["gain_db"], band["q"])
        else:
            raise ValueError("Unsupported EQ band")
        filtered = sosfilt(sos, filtered, axis=0)
    return filtered[len(audio):].astype(np.float32), {"enabled": True, "bands": recipe["bands"],
        "warmup": "One known repeated source period", "identical_control": False}


def _smooth_control(target, attack_ms, release_ms, *, control_rate=1000):
    attack = math.exp(-1/(control_rate*attack_ms/1000))
    release = math.exp(-1/(control_rate*release_ms/1000))
    smoothed = np.empty(len(target), np.float64)
    state = 0.0
    for index, value in enumerate(target):
        coefficient = attack if value > state else release
        state = coefficient*state+(1-coefficient)*value
        smoothed[index] = state
    return smoothed


def compress(audio, recipe):
    audio = audio_array(audio, RATE)
    if not recipe["enabled"]:
        return audio.copy(), {"enabled": False, "identical_control": True, "maximum_reduction_db": 0}
    values = [recipe[k] for k in ("ratio", "threshold_dbfs", "knee_db", "attack_ms", "release_ms", "maximum_reduction_db")]
    if (not all(math.isfinite(v) for v in values) or not 1 <= recipe["ratio"] <= 2
            or not -100 <= recipe["threshold_dbfs"] <= 0 or not 0 < recipe["knee_db"] <= 6
            or not 10 <= recipe["attack_ms"] <= 30 or not 60 <= recipe["release_ms"] <= 200
            or not 0 < recipe["maximum_reduction_db"] <= 3 or recipe.get("makeup_gain_db") != 0):
        raise ValueError("Compression recipe exceeds the evaluation bounds")
    repeated = np.tile(audio, (2, 1)).astype(np.float64)
    power = np.mean(repeated**2, axis=1)
    coefficient = math.exp(-1/(RATE*.005))
    rms_power = lfilter([1-coefficient], [1, -coefficient], power)
    control_positions = np.arange(0, len(repeated), 48)
    level = 10*np.log10(np.maximum(rms_power[control_positions], 1e-24))
    difference = level-recipe["threshold_dbfs"]
    knee = recipe["knee_db"]
    slope = 1-1/recipe["ratio"]
    reduction = np.where(difference < -knee/2, 0,
        np.where(difference > knee/2, slope*difference, slope*(difference+knee/2)**2/(2*knee)))
    reduction = np.clip(reduction, 0, recipe["maximum_reduction_db"])
    smoothed = _smooth_control(reduction, recipe["attack_ms"], recipe["release_ms"])
    envelope = np.interp(np.arange(len(repeated)), control_positions, smoothed)[len(audio):]
    result = audio*10**(-envelope[:, None]/20)
    return result.astype(np.float32), {"enabled": True, "identical_control": False,
        "maximum_reduction_db": float(envelope.max()), "mean_reduction_db": float(envelope.mean()),
        "reduced_fraction": float(np.mean(envelope > .1)), "control_rate_hz": 1000,
        "interpolation_support_ms": 1, "stereo_linked": True,
        "warmup": "One known repeated source period", "makeup_gain_db": 0}


def deess(audio, recipe):
    audio = audio_array(audio, RATE)
    if not recipe["enabled"]:
        return audio.copy(), {"enabled": False, "identical_control": True, "maximum_reduction_db": 0}
    if not 0 < recipe["maximum_reduction_db"] <= 2.5:
        raise ValueError("De-essing is capped at 2.5 dB")
    target = np.zeros(math.ceil(len(audio)/48), np.float64)
    for event in recipe["events"]:
        start, end = event["start_seconds"], event["end_seconds"]
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= len(audio)/RATE):
            raise ValueError("Invalid de-essing event")
        target[max(0, int(start*1000)):min(len(target), math.ceil(end*1000))] = recipe["maximum_reduction_db"]
    repeated = np.tile(target, 2)
    curve = _smooth_control(repeated, 2, 35)[len(target):]
    envelope = np.interp(np.arange(len(audio)), np.arange(len(target))*48, curve)
    output = audio*10**(-envelope[:, None]/20)
    return output.astype(np.float32), {"enabled": True, "identical_control": False,
        "maximum_reduction_db": float(envelope.max()), "reduced_fraction": float(np.mean(envelope > .1)),
        "event_count": len(recipe["events"]), "processor": "wideband_event_gain", "confirmed_sibilance": False}


def render_variants(audio, recipe):
    audio = audio_array(audio, RATE)
    eq, eq_stats = equalize(audio, recipe["eq"])
    compression, compression_stats = compress(audio, recipe["compression"])
    deessed, deess_stats = deess(audio, recipe["deess"])
    chain, chain_comp = compress(eq, recipe["compression"])
    chain, chain_deess = deess(chain, recipe["deess"])
    variants = {"current": audio.copy(), "eq": eq, "compression": compression}
    if recipe["role"] == "red":
        variants["deess"] = deessed
    variants["chain"] = chain
    for rendered in variants.values():
        if rendered.shape != audio.shape or not np.isfinite(rendered).all():
            raise ValueError("Processing must preserve finite sample counts and channels")
    stats = {"eq": eq_stats, "compression": compression_stats, "deess": deess_stats,
        "chain": {"order": "EQ → compression → de-essing", "compression": chain_comp, "deess": chain_deess}}
    return variants, stats
