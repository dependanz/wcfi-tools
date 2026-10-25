"""Known operations on real rehearsal audio, not labels from an audio LLM."""
from __future__ import annotations

import json
import time

import numpy as np
from scipy.signal import butter, sosfilt

from ..soundcheck.profiles import write_json
from .role_analysis import analyze_pair, render_proposals
from .role_replay import render_report, save_observation, take_windows
from .roles import get_profile


def _bursts(audio, rate):
    rng = np.random.default_rng(81423)
    noise = sosfilt(butter(4, [6000, 10000], fs=rate, btype="bandpass", output="sos"),
                    rng.normal(size=len(audio)))
    noise /= max(float(np.sqrt(np.mean(noise**2))), 1e-12)
    result = audio.copy()
    ranges = []
    for second in (1.5, 3.5, 5.5):
        first, count = round(second * rate), round(.12 * rate)
        if first + count >= len(audio):
            continue
        result[first:first + count] += (.2 * noise[first:first + count] * np.hanning(count))[:, None]
        ranges.append((second, second + .12))
    return result, ranges


def validate_take(take, output, *, start=29, seconds=10):
    if not 8 <= seconds <= 20:
        raise ValueError("Validation needs 8–20 seconds")
    if output.exists():
        raise ValueError("Choose a new output directory; previous results are immutable")
    windows = take_windows(take, start=start, seconds=seconds)
    output.mkdir(parents=True, exist_ok=False)
    # This protocol is written before any analysis. Cases are sensitivity/control
    # tests of known operations, not human truth about professional sound.
    write_json(output / "protocol.json", {"version": 1,
        "design": "Same real RED/KIT passage; identity, silence, quiet input, post gain, clipped post audio, HF perturbation and drum transient flattening.",
        "source_range_seconds": [start, start + seconds],
        "checks": ["identical inputs give zero paired change", "no automatic boost on silence/quiet input",
            "post-gain attenuation meets configured sample-peak ceiling and preserves waveform shape",
            "attenuation is not reported as clipping repair", "RED detects short HF perturbations as candidates only",
            "KIT never runs vocal sibilance processing", "KIT crest loss is described without claiming a quality preference",
            "antiphase stereo does not cancel in analysis"],
        "human_quality_ground_truth": False, "neural_training": False,
        "limits": "One take and one fixed fault recipe. Passing checks is a regression/sensitivity result, not general detection accuracy or a listening test."})
    started = time.perf_counter()
    checks, reports = [], {}

    def check(name, passed, evidence=None):
        checks.append({"name": name, "passed": bool(passed), "evidence": evidence})

    normalized = {}
    for name, (audio, rate, provenance) in windows.items():
        base = audio * (10 ** (-18 / 20) / max(float(np.max(np.abs(audio))), 1e-12))
        normalized[name] = base
        profile = get_profile(name)
        identity = analyze_pair(base, base, rate, profile, aligned=True)
        check(name + ":identity_change_zero", all(abs(v) < 1e-8 for v in identity["paired_change"].values()))
        for label, signal in (("silence", np.zeros_like(base)), ("quiet", base * .0001)):
            result = analyze_pair(signal, signal, rate, profile, aligned=True)
            check(name + ":" + label + "_not_boosted", not result["proposals"])
        hot = base * 10 ** (24 / 20)
        hot_result = analyze_pair(base, hot, rate, profile, aligned=True)
        gain_actions = [a for a in hot_result["proposals"] if a["kind"] == "software_gain"]
        corrected, _ = render_proposals(hot, rate, gain_actions)
        peak = float(np.max(np.abs(corrected)))
        check(name + ":gain_headroom", bool(gain_actions) and peak <= 10 ** (-6 / 20) + 1e-6,
              {"sample_peak_linear": peak, "gain_db": gain_actions[0]["gain_db"] if gain_actions else None})
        check(name + ":gain_shape_preserved", bool(gain_actions) and np.allclose(
            corrected, hot * 10 ** (gain_actions[0]["gain_db"] / 20), atol=1e-7))
        clipped = np.clip(base * 10 ** (36 / 20), -1, 1)
        clipped_result = analyze_pair(base, clipped, rate, profile, aligned=True)
        gain_actions = [a for a in clipped_result["proposals"] if a["kind"] == "software_gain"]
        compensated, _ = render_proposals(clipped, rate, gain_actions)
        check(name + ":clipped_signal_flagged", any("clipping" in f["kind"] for f in clipped_result["after"]["findings"]))
        check(name + ":compensation_not_reconstruction", bool(gain_actions) and np.allclose(
            compensated, clipped * 10 ** (gain_actions[0]["gain_db"] / 20), atol=1e-7))
        reports[name + "-headroom-test"] = save_observation(output, name + "-headroom-test", base,
            hot, rate, profile, provenance=provenance | {"test_operation": "+24 dB after the before node"},
            aligned=True, chain_state={"kind": "controlled_local_gain", "gain_db": 24}, previews=True)
    _, rate, _ = windows["RED"]
    perturbed, ranges = _bursts(normalized["RED"], rate)
    red_result = analyze_pair(normalized["RED"], perturbed, rate, "red", aligned=True)
    events = red_result["after"]["analyses"]["sibilance_candidates"]["events"]
    hits = sum(any(e["start_seconds"] < hi and e["end_seconds"] > lo for e in events) for lo, hi in ranges)
    check("RED:HF_perturbation_candidates", hits == len(ranges) and len(ranges) == 3,
          {"injected_bursts": len(ranges), "overlapping_candidates": hits})
    check("RED:candidates_not_claimed_sibilance", not red_result["after"]["analyses"]["sibilance_candidates"]["confirmed_sibilance"])
    wrong_role = analyze_pair(perturbed, perturbed, rate, "kit", aligned=True)
    check("KIT:no_vocal_sibilance_analysis", "sibilance_candidates" not in wrong_role["after"]["analyses"]
          and not any(a["kind"] == "hf_burst_reduction" for a in wrong_role["proposals"]))
    reports["RED-HF-test"] = save_observation(output, "RED-HF-test", normalized["RED"], perturbed,
        rate, get_profile("red"), provenance={"test_operation": "Three synthetic 120ms HF noise bursts; not real labelled esses"},
        aligned=True, chain_state={"kind": "known_HF_perturbation"}, previews=True)
    fixed = reports["RED-HF-test"]["candidate"]
    check("RED:HF_preview_remains_experimental", any(a["eligibility"] == "listening_preview_only" for a in fixed["applied"]))
    # Independent check in the injected band, rather than reusing the proposal detector.
    corrected, applied = render_proposals(perturbed, rate, red_result["proposals"], include_experimental=True)
    software_gain = sum(a["gain_db"] for a in applied if a["kind"] == "software_gain")
    corrected /= 10 ** (software_gain / 20)
    band = butter(4, [6500, 10000], fs=rate, btype="bandpass", output="sos")
    original_band = sosfilt(band, perturbed, axis=0)
    corrected_band = sosfilt(band, corrected, axis=0)
    injected = np.zeros(len(perturbed), bool)
    for lo, hi in ranges:
        injected[round((lo+.03)*rate):round((hi-.03)*rate)] = True
    band_change_db = float(10*np.log10(max(np.mean(corrected_band[injected]**2), 1e-20)
                                      / max(np.mean(original_band[injected]**2), 1e-20)))
    check("RED:known_HF_band_reduced", band_change_db < -.5, {"change_db": band_change_db})
    selected = np.zeros(len(perturbed), bool)
    for action in applied:
        if action["kind"] == "hf_burst_reduction":
            for event in action["events"]:
                selected[round(event["start_seconds"]*rate):round(event["end_seconds"]*rate)] = True
    check("RED:outside_selected_events_unchanged", np.allclose(corrected[~selected], perturbed[~selected], atol=1e-7))
    kit = normalized["KIT"]
    flattened = np.tanh(kit * 30).astype(np.float32)
    flattened *= np.sqrt(np.mean(kit.astype(float)**2) / max(np.mean(flattened.astype(float)**2), 1e-20))
    result = analyze_pair(kit, flattened, rate, "kit", aligned=True)
    check("KIT:transient_change_detected", any(f["kind"] == "drum_transient_change" for f in result["after"]["findings"]), result["paired_change"])
    check("KIT:no_compressor_added_from_crest_alone", not any(a["kind"] == "compressor" for a in result["proposals"]))
    stereo = np.column_stack((kit[:, 0], -kit[:, 0]))
    result = analyze_pair(stereo, stereo, rate, "kit", aligned=True)
    check("KIT:antiphase_energy_preserved", result["before"]["metrics"]["rms_dbfs"] > -70
          and result["before"]["analyses"]["stereo_health"]["correlation"] < -.99)
    summary = {"kind": "wcfi_role_validation", "checks": checks,
        "passed": sum(c["passed"] for c in checks), "total": len(checks),
        "elapsed_seconds": time.perf_counter() - started,
        "professional_audio_quality_validated": False, "human_listening": "pending",
        "audio_uploads": 0, "gpu_used": False, "daw_writes": 0,
        "report": str((output / "report.html").resolve())}
    write_json(output / "validation.json", summary)
    render_report(output, reports, title="RED / KIT controlled sensitivity checks")
    (output / "validation.txt").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
