"""Local role-aware observations and bounded proposals; no DAW/network/device IO.

Input is a finite sample window, not a filename or audio callback. The same
analyser accepts recorded replay and rolling-node windows. Semantic singing
activity is deliberately unknown; energy activity must never authorize boosts.
"""
from __future__ import annotations

import hashlib
import json
import time

import numpy as np
from scipy.fft import rfft, rfftfreq
from scipy.signal import butter, find_peaks, sosfilt

from .alignment import estimate_delay
from .roles import get_profile


def db(amplitude):
    return float(20 * np.log10(max(float(amplitude), 1e-6)))


def revision(state):
    """Fingerprint supplied chain/mixer state, never pretend to read a plug-in."""
    if state is None:
        return None
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()


def audio_array(samples, rate):
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio[:, None]
    if (audio.ndim != 2 or audio.shape[1] not in (1, 2) or type(rate) is not int
            or not 32000 <= rate <= 96000 or not rate // 2 <= len(audio) <= rate * 30):
        raise ValueError("Use 0.5–30 seconds of mono/stereo audio at 32–96 kHz")
    if not np.isfinite(audio).all():
        raise ValueError("Non-finite audio: reject this observation; no proposal is safe")
    return audio


def features(samples, rate):
    """Channel powers are averaged; antiphase stereo never cancels into silence."""
    audio = audio_array(samples, rate)
    width, hop = 2048, round(rate * .01)
    frames = np.lib.stride_tricks.sliding_window_view(audio, width, axis=0)[::hop]
    frame_power = np.mean(frames.astype(np.float64) ** 2, axis=(1, 2))
    levels = 10 * np.log10(np.maximum(frame_power, 1e-12))
    spectrum = np.mean(np.abs(rfft(frames * np.hanning(width).astype(np.float32),
                                  axis=-1, workers=1)) ** 2, axis=1)
    frequencies = rfftfreq(width, 1 / rate)
    bands = {}
    for name, low, high in (("low", 30, 150), ("body", 150, 3500),
                            ("high", 4500, 12000)):
        bands[name] = np.sum(spectrum[:, (frequencies >= low) & (frequencies < high)], axis=1)
    total = np.maximum(np.sum(spectrum, axis=1), 1e-20)
    energy_active = levels > max(-65, float(np.percentile(levels, 95)) - 24)
    return {"audio": audio, "rate": rate, "hop": hop, "frame_offset_seconds": width / (2 * rate), "levels": levels,
            "active": energy_active, "bands": bands,
            "high_fraction": bands["high"] / total,
            "high_body_db": 10 * np.log10(np.maximum(bands["high"], 1e-20)
                                         / np.maximum(bands["body"], 1e-20))}


def intervals(mask, hop, rate, *, minimum=.03, maximum=.3, offset=0):
    padded = np.r_[False, mask, False].astype(np.int8)
    changes = np.diff(padded)
    result = []
    for start, end in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1), strict=True):
        duration = (end - start) * hop / rate
        if minimum <= duration <= maximum:
            result.append({"start_seconds": start * hop / rate + offset,
                           "end_seconds": min(len(mask) * hop / rate, end * hop / rate) + offset,
                           "start_frame": int(start), "end_frame": int(end)})
    return result


def health(f):
    audio = f["audio"]
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    rail = int(np.count_nonzero(np.abs(np.abs(audio) - 1) <= 2**-23))
    # Multiple identical near-maximum samples can indicate already scaled clipping.
    # This remains a heuristic: quantization/intentional waveform shapes can imitate it.
    equal = np.abs(np.diff(audio, axis=0)) <= 1e-8
    tops = np.abs(audio[1:]) >= max(.001, peak * .995)
    flat = int(np.count_nonzero((equal & tops)[1:] & (equal & tops)[:-1]))
    active = f["levels"][f["active"]]
    return {"sample_peak_dbfs": db(peak), "rms_dbfs": db(rms),
            "crest_db": db(peak) - db(rms), "full_scale_samples": rail,
            "above_full_scale_samples": int(np.count_nonzero(np.abs(audio) > 1 + 2**-23)),
            "possible_flat_top_samples": flat,
            "dc_offset": [float(v) for v in np.mean(audio, axis=0)],
            "energy_active_fraction": float(np.mean(f["active"])),
            "energy_active_level_p10_p50_p90_dbfs": [float(v) for v in np.percentile(active, [10, 50, 90])] if len(active) else None,
            "semantic_source_activity": "unverified",
            "sample_peak_is_not_true_peak": True}


def sibilance_candidates(f, context):
    active = f["active"]
    ratio = f["high_body_db"]
    baseline = float(np.median(ratio[active])) if np.any(active) else 0.0
    mask = active & (f["high_fraction"] > .2) & (ratio > baseline + 4)
    events = intervals(mask, f["hop"], f["rate"], offset=f["frame_offset_seconds"])
    overlap = None
    if context is not None:
        count = min(len(context["high_fraction"]), len(mask))
        reference = context["high_fraction"][:count]
        kit_active = context["active"][:count]
        kit_high = kit_active & (reference > .15)
        selected = np.zeros(count, bool)
        for event in events:
            selected[event["start_frame"]:event["end_frame"]] = True
        if np.any(selected):
            overlap = float(np.mean(kit_high[selected]))
    ambiguous = overlap is not None and overlap > .5
    return {"candidate_count": len(events), "events": events[:30],
            "high_body_baseline_db": baseline, "kit_high_frequency_overlap_fraction": overlap,
            "bleed_ambiguous": ambiguous, "confirmed_sibilance": False,
            "interpretation": "Short HF bursts are candidates, not confirmed vocal sibilance. Energy activity is not singing detection."}


def drum_dynamics(f):
    peaks, _ = find_peaks(f["levels"], prominence=6, distance=max(1, round(.06 * f["rate"] / f["hop"])))
    peaks = peaks[f["levels"][peaks] > -45]
    levels = f["levels"][f["active"]]
    return {"detected_energy_attacks": len(peaks),
            "attack_times_seconds": [float(v * f["hop"] / f["rate"] + f["frame_offset_seconds"]) for v in peaks[:100]],
            "energy_level_spread_p90_p10_db": float(np.ptp(np.percentile(levels, [10, 90]))) if len(levels) else None,
            "interpretation": "Energy attacks are not individual drum identifications. Dynamics alone do not establish a need for compression."}


def observe(samples, rate, profile, *, stage="pre_fx", context=None):
    if isinstance(profile, str):
        profile = get_profile(profile)
    f = features(samples, rate)
    metrics = health(f)
    findings = []
    if metrics["full_scale_samples"] or metrics["possible_flat_top_samples"] >= 12:
        findings.append({"kind": "possible_upstream_clipping" if stage == "pre_fx" else "possible_clipping_at_output",
            "stage": stage, "evidence": {k: metrics[k] for k in ("full_scale_samples", "possible_flat_top_samples")},
            "certainty": "suspected_cause_measured_samples",
            "note": "Check the source and physical analogue trim when appropriate. This observation cannot identify the exact upstream cause; software attenuation does not reconstruct clipped audio."})
    if metrics["sample_peak_dbfs"] < -65:
        findings.append({"kind": "insufficient_signal", "stage": stage,
                         "note": "Silence/quiet bleed is not evidence of a quiet performer. No automatic boost."})
    modules = {}
    for name in profile.analyses:
        if name == "signal_health":
            modules[name] = {"full_scale_samples": metrics["full_scale_samples"],
                             "above_full_scale_samples": metrics["above_full_scale_samples"],
                             "possible_flat_top_samples": metrics["possible_flat_top_samples"]}
        elif name == "headroom":
            modules[name] = {"sample_peak_dbfs": metrics["sample_peak_dbfs"],
                             "configured_ceiling_dbfs": profile.peak_ceiling_dbfs}
        elif name == "sibilance_candidates":
            modules[name] = sibilance_candidates(f, context)
        elif name == "vocal_dynamics":
            modules[name] = {"energy_level_percentiles": metrics["energy_active_level_p10_p50_p90_dbfs"],
                "vocal_activity_verified": False, "automatic_leveling_eligible": False,
                "note": "Level variation is measured; a validated singing/bleed detector is still required for automatic vocal leveling."}
        elif name == "transient_dynamics":
            modules[name] = drum_dynamics(f)
        elif name == "cymbal_balance":
            modules[name] = {"high_frequency_energy_fraction": float(np.mean(f["high_fraction"][f["active"]])) if np.any(f["active"]) else 0,
                "note": "Brightness descriptor, not proof of harsh cymbals. No universal drum spectrum or automatic EQ cut."}
        elif name == "stereo_health":
            if f["audio"].shape[1] == 2:
                left, right = f["audio"].astype(np.float64).T
                denominator = np.sqrt(np.sum(left * left) * np.sum(right * right))
                modules[name] = {"correlation": float(np.sum(left * right) / denominator) if denominator else None,
                    "left_minus_right_rms_db": db(np.sqrt(np.mean(left * left))) - db(np.sqrt(np.mean(right * right)))}
            else:
                modules[name] = {"available": False, "reason": "Mono input"}
    return {"stage": stage, "metrics": metrics, "analyses": modules, "findings": findings}


def propose(observation, profile):
    """Only attenuation has a numerical authorization criterion in version 1."""
    metrics = observation["metrics"]
    actions = []
    needed = profile.peak_ceiling_dbfs - metrics["sample_peak_dbfs"]
    if needed < -.05:
        gain = max(-profile.max_attenuation_db, needed)
        actions.append({"kind": "software_gain", "gain_db": gain,
            "eligibility": "numerically_testable", "placement": "after existing effects, before the after-effects node",
            "reason": "Preserve configured sample-peak headroom; this is not a mix-balance decision or clipping repair.",
            "limited_by_max_attenuation": gain > needed + .01})
    sibilance = observation["analyses"].get("sibilance_candidates")
    if sibilance and sibilance["candidate_count"] and not sibilance["bleed_ambiguous"]:
        actions.insert(0, {"kind": "hf_burst_reduction", "cutoff_hz": 4500,
            "maximum_reduction_db": 3.0, "events": sibilance["events"],
            "eligibility": "listening_preview_only", "requires_human_validation": True,
            "reason": "Experimental treatment of candidate HF bursts; not confirmed sibilance.",
            "processor": "WCFI reference DSP, not an emulation of Pro-DS or Pro-Q"})
    return actions


def analyze_pair(before, after, rate, profile, *, chain_state=None, aligned=False,
                 context=None, estimate_alignment=False):
    """Analyse aligned copies when known; never guess timing from equal lengths."""
    started = time.perf_counter()
    profile = get_profile(profile) if isinstance(profile, str) else profile
    before = audio_array(before, rate)
    after = audio_array(after, rate)
    declared_alignment = bool(aligned)
    if before.shape != after.shape:
        raise ValueError("Paired windows must have matching sample counts and channels")
    alignment = {"available": bool(aligned), "method": "caller_declared" if aligned else "unverified"}
    if estimate_alignment:
        channel = int(np.argmax(np.sum(before.astype(np.float64)**2, axis=0)))
        tail = min(len(before), rate*2)
        alignment = estimate_delay(before[-tail:, channel], after[-tail:, channel], rate=rate, max_ms=200)
        alignment["method"] = "conservative_waveform_estimate_not_plugin_or_hardware_latency"
        aligned = alignment["available"]
        if aligned:
            lag = round(alignment["lag_ms"] * rate / 1000)
            alignment["lag_samples"] = lag
            count = len(before) - abs(lag)
            if count < rate // 2:
                aligned = alignment["available"] = False
                alignment["reason"] = "Insufficient aligned overlap; each stage is analysed independently"
            else:
                pre_start, post_start = max(0, -lag), max(0, lag)
                before, after = before[pre_start:pre_start+count], after[post_start:post_start+count]
                if context is not None:
                    context = context[pre_start:pre_start+count]
    context_features = features(context, rate) if context is not None else None
    pre = observe(before, rate, profile, stage="pre_fx", context=context_features)
    post = observe(after, rate, profile, stage="post_fx", context=context_features)
    change = None
    if aligned:
        change = {"rms_change_db": post["metrics"]["rms_dbfs"] - pre["metrics"]["rms_dbfs"],
                  "sample_peak_change_db": post["metrics"]["sample_peak_dbfs"] - pre["metrics"]["sample_peak_dbfs"],
                  "crest_change_db": post["metrics"]["crest_db"] - pre["metrics"]["crest_db"]}
        if profile.role == "drum_bus" and change["crest_change_db"] < -3:
            post["findings"].append({"kind": "drum_transient_change", "stage": "post_fx",
                "certainty": "measured_change_unverified_preference", "evidence": change,
                "note": "The chain reduces peak-to-average contrast by more than 3 dB. Review compression/limiting attack and reduction against the same performance; intentional processing may be appropriate."})
    return {"schema_version": 1, "kind": "wcfi_role_analysis", "profile": profile.describe(),
        "sample_rate": rate, "channels": before.shape[1], "frames": len(before),
        "duration_seconds": len(before) / rate, "chain_state": chain_state,
        "chain_revision": revision(chain_state), "alignment_declared": declared_alignment,
        "paired_alignment_available": bool(aligned),
        "alignment": alignment,
        "paired_change": change, "before": pre, "after": post,
        "proposals": propose(post, profile), "elapsed_seconds": time.perf_counter() - started,
        "automatic_daw_writes": False, "audio_uploads": 0,
        "limits": ["Experimental deterministic descriptors and policies; no professional mix-quality certification.",
                   "A post-effects node is before the host fader/pan; final mix balance is not assessed here.",
                   "No voice separation, validated singing detector, hum remover or source-clipping reconstruction is implemented.",
                   "Timbre proposals require blinded human listening; numerical improvement is not audible improvement."]}


def render_proposals(audio, rate, actions, *, include_experimental=False):
    """Reference local preview only. Never mutates a DAW or an input file."""
    out = audio_array(audio, rate).copy()
    applied = []
    for action in actions:
        if action["eligibility"] == "listening_preview_only" and not include_experimental:
            continue
        if action["kind"] == "software_gain":
            out *= 10 ** (action["gain_db"] / 20)
        elif action["kind"] == "hf_burst_reduction":
            high = sosfilt(butter(2, action["cutoff_hz"], fs=rate, btype="highpass", output="sos"), out, axis=0)
            envelope = np.zeros(len(out), np.float32)
            ramp = max(1, round(.01 * rate))
            for event in action["events"]:
                lo = max(0, round(event["start_seconds"] * rate))
                hi = min(len(out), round(event["end_seconds"] * rate))
                if hi <= lo:
                    continue
                indices = np.arange(hi - lo)
                envelope[lo:hi] = np.maximum(envelope[lo:hi], np.minimum(1, np.minimum(indices + 1, hi - lo - indices) / ramp))
            out += high * ((10 ** (-action["maximum_reduction_db"] / 20) - 1) * envelope[:, None])
        else:
            raise ValueError("Unsupported reference DSP action")
        applied.append(action)
    if not np.isfinite(out).all():
        raise RuntimeError("Reference processor returned invalid audio")
    return out.astype(np.float32), applied
