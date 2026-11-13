"""Captured native compression comparison plus a verified linear-path EQ preview.

No device, DAW, network or playback writes. A linear null test is required before
replacing KIT inside a captured Master mix. This is not a plug-in emulation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.fft import irfft, rfft
from scipy.signal import sosfilt, tf2sos, welch

from ..soundcheck.mix import parse_measurements
from .role_analysis import features, health, observe
from .roles import get_profile

VARIANTS = ("current", "compression", "eq", "both")
STUDY_AUDIO = {f"study_{scope}_{variant}{suffix}" for scope in ("kit", "mix")
               for variant in VARIANTS for suffix in ("", "_matched")}
BYPASS_VARIANTS = ("bypass", *VARIANTS)
BYPASS_STUDY_AUDIO = {f"study_{scope}_{variant}{suffix}" for scope in ("kit", "mix")
                      for variant in BYPASS_VARIANTS for suffix in ("", "_matched")}


def study_audio_keys(schema_version):
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise ValueError("Not a supported KIT study version")
    return STUDY_AUDIO if schema_version == 1 else BYPASS_STUDY_AUDIO


def align_cycle(reference, target, *, minimum_correlation=.95):
    """Align one complete known repeated passage; never use this as DAW latency."""
    if reference.shape != target.shape or len(reference) < 24000:
        raise ValueError("Comparison needs matching complete loop windows")
    x, y = np.asarray(reference, np.float64), np.asarray(target, np.float64)
    cross = np.sum(np.conj(rfft(x, axis=0, workers=1))*rfft(y, axis=0, workers=1), axis=1)
    scores = irfft(cross, n=len(x), workers=1)
    lag = int(np.argmax(scores))
    matched = np.roll(target, -lag, axis=0)
    denominator = np.sqrt(np.sum(x*x)*np.sum(y*y))
    correlation = float(scores[lag]/max(denominator, 1e-30))
    if denominator < 1e-12 or correlation < minimum_correlation:
        raise ValueError("The repeated audio does not establish a reliable cyclic match")
    return matched, {"roll_target_samples": -lag, "correlation": min(1.0, correlation),
                     "method": "complete_known_loop_correlation_not_latency"}


def low_shelf(audio, rate, *, hz=120, gain_db=-2):
    """Smooth RBJ low shelf, slope 1; repeated-period warmup avoids loop-edge state."""
    if not 40 <= hz <= 250 or not -3 <= gain_db <= 0 or rate != 48000:
        raise ValueError("Study EQ supports a bounded low shelf at 48 kHz")
    a = 10**(gain_db/40)
    omega = 2*math.pi*hz/rate
    cosine, sine = math.cos(omega), math.sin(omega)
    alpha = sine/math.sqrt(2)
    root = 2*math.sqrt(a)*alpha
    b = [a*((a+1)-(a-1)*cosine+root), 2*a*((a-1)-(a+1)*cosine),
         a*((a+1)-(a-1)*cosine-root)]
    denominator = [(a+1)+(a-1)*cosine+root, -2*((a-1)+(a+1)*cosine),
                   (a+1)+(a-1)*cosine-root]
    sos = tf2sos(b, denominator)
    warmed = sosfilt(sos, np.tile(audio, (2, 1)), axis=0)[len(audio):]
    return warmed.astype(np.float32)


def validate_linear_path(snapshot):
    by_name = {t["name"].upper(): t for t in snapshot["tracks"]}
    kit, group, master = (by_name[n] for n in ("KIT", "INSTRUMENTS", "MASTER"))
    for track in (kit, group):
        if (track["mute"] or track["solo"] or track["volume"]["display"] != "0.0 dB"
                or track["pan"]["value"] != 0 or track["volume"]["automation_state"] != 0
                or any(p["value"] != 0 for p in track["sends"])):
            raise ValueError("KIT-to-Master EQ preview requires active unity faders, centred pan and no sends")
    if (kit["group_id"] != group["id"] or kit["routing"]["output_routing_type"] != group["name"]
            or group["routing"]["output_routing_type"].lower() != "master" or group["devices"]):
        raise ValueError("KIT must reach Master through a group with no processing")
    nodes = [i for i, d in enumerate(master["devices"]) if d["name"] == "WCFI Analysis Node"]
    if nodes != [0, 1]:
        raise ValueError("Master comparison requires two leading pass-through Analysis Nodes")
    return {"path": "KIT post-effects → unity KIT fader → unity INSTRUMENTS group → Master node",
            "kit_contribution_gain": 1.0, "master_fader_included": False,
            "master_fader_display": master["volume"]["display"],
            "scope": "Captured Master node before its fader, not ATEM or final encoded stream"}


def allowed_session_change(baseline, candidate, *, parameter="Attack"):
    """One native attack or bypass change only; all other host controls must match."""
    if parameter not in ("Attack", "Device On"):
        raise ValueError("Only compressor attack or bypass is supported")
    expected = copy.deepcopy(baseline)
    expected.pop("inspected_at", None)
    actual = copy.deepcopy(candidate)
    actual.pop("inspected_at", None)
    changes = []
    for old, new in zip(expected["tracks"], actual["tracks"], strict=True):
        if old["name"] != "KIT":
            continue
        for d_old, d_new in zip(old["devices"], new["devices"], strict=True):
            if d_old["class_name"] != "Compressor2":
                continue
            for p_old, p_new in zip(d_old["parameters"], d_new["parameters"], strict=True):
                if p_old["name"] == parameter:
                    if parameter == "Device On" and (p_old["value"] != 1 or p_new["value"] != 0):
                        raise ValueError("Bypass capture requires an enabled compressor switched off")
                    changes.append({"parameter": parameter, "before": p_old["display"], "after": p_new["display"]})
                    p_old["value"], p_old["display"] = p_new["value"], p_new["display"]
    if len(changes) != 1 or expected != actual:
        raise ValueError("Capture sessions differ by more than the intended compressor change")
    return changes


def measurements(audio, rate):
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(float)**2)))
    f, p = welch(audio, fs=rate, nperseg=8192, axis=0)
    p = p.mean(axis=1)
    bands = {name: float(np.sum(p[(f >= low) & (f < high)])/max(np.sum(p), 1e-30))
             for name, low, high in (("sub_30", 0, 30), ("low_30_100", 30, 100),
                 ("body_100_250", 100, 250), ("lowmid_250_500", 250, 500),
                 ("presence_2000_5000", 2000, 5000))}
    def db(value):
        return float(20*np.log10(max(value, 1e-6)))
    return {"sample_peak_dbfs": db(peak), "rms_dbfs": db(rms), "crest_db": db(peak)-db(rms),
            "band_energy_fractions": bands}


def scan_loudness(path, ffmpeg):
    result = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-nostats", "-threads", "1",
        "-filter_threads", "1", "-i", str(path), "-af", "ebur128=peak=sample+true", "-f", "null", "-"],
        capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Local FFmpeg loudness scan failed")
    metrics = parse_measurements(result.stderr)
    if metrics["integrated_lufs"] is None or metrics["true_peak_dbtp"] is None:
        raise ValueError("Comparison audio is too quiet for loudness matching")
    return metrics


def verify_mix_delta(delta_mix, delta_kit):
    aligned_delta, alignment = align_cycle(delta_mix, delta_kit, minimum_correlation=.995)
    residual = delta_mix.astype(float)-aligned_delta
    relative_error = float(np.sqrt(np.mean(residual**2))/max(1e-12, np.sqrt(np.mean(delta_mix.astype(float)**2))))
    if relative_error > .001:
        raise ValueError("Captured full-mix change did not null against KIT; EQ mix substitution is unsafe")
    return alignment, relative_error


def match_kit_contribution(kit_versions, mix_versions, loudness, *, measured_match_gains=None):
    """Equalize KIT contribution without changing the remaining mix waveform."""
    if set(kit_versions) != set(mix_versions) or set(kit_versions) != set(loudness):
        raise ValueError("KIT matching requires the same variants and loudness observations")
    if measured_match_gains is not None and set(measured_match_gains) != set(kit_versions):
        raise ValueError("Measured matching gains must cover every KIT variant")
    gains = {v: (loudness["current"]-loudness[v] if measured_match_gains is None else
                 measured_match_gains[v]-measured_match_gains["current"]) for v in kit_versions}
    if not all(math.isfinite(g) and abs(g) <= 36 for g in gains.values()):
        raise ValueError("KIT loudness differences exceed the comparison budget")
    remainder = mix_versions["current"]-kit_versions["current"]
    return {v: remainder+audio*10**(gains[v]/20) for v, audio in kit_versions.items()}, gains


def write_auditions(kit_versions, mix_versions, output, rate, ffmpeg, *, match_kit_in_mix=False,
                    kit_to_mix_gain=1.0):
    """Finite copies; matched KIT contribution uses the verified path gain to the mix."""
    variants = tuple(kit_versions)
    if set(variants) != set(mix_versions):
        raise ValueError("KIT and mix variants must match")
    if not math.isfinite(kit_to_mix_gain) or not 0 < kit_to_mix_gain <= 64:
        raise ValueError("KIT contribution needs a bounded positive path gain")
    audio_files, stats, matching = {}, {}, {}
    for scope, versions in (("kit", kit_versions), ("mix", mix_versions)):
        stats[scope] = {}
        for variant, audio in versions.items():
            key = f"study_{scope}_{variant}"
            filename = "KIT-"+key+".wav"
            sf.write(output/filename, audio, rate, subtype="FLOAT")
            audio_files[key] = filename
            stats[scope][variant] = measurements(audio, rate) | {"loudness": scan_loudness(output/filename, ffmpeg)}
        if scope == "mix" and match_kit_in_mix:
            kit_reference = stats["kit"]["current"]["loudness"]["integrated_lufs"]
            comparison, kit_gains = match_kit_contribution(
                {v: a*kit_to_mix_gain for v, a in kit_versions.items()}, versions,
                {v: stats["kit"][v]["loudness"]["integrated_lufs"] for v in variants},
                measured_match_gains=matching["kit"]["variant_gain_db"])
            neutral_stats = {}
            for v, audio in comparison.items():
                # These finite files are overwritten with their final safe gain below.
                path = output/f"KIT-study_mix_{v}_matched.wav"
                sf.write(path, audio, rate, subtype="FLOAT")
                neutral_stats[v] = scan_loudness(path, ffmpeg)
            common = min(-24-neutral_stats["current"]["integrated_lufs"],
                         -3-max(s["true_peak_dbtp"] for s in neutral_stats.values()))
            gains = {v: common for v in variants}
            matching[scope] = {"target_lufs": neutral_stats["current"]["integrated_lufs"]+common,
                "kit_contribution_matched": True, "kit_reference_lufs": kit_reference,
                "kit_to_mix_gain": kit_to_mix_gain,
                "kit_variant_gain_db": kit_gains, "common_audition_gain_db": common,
                "method": "Match KIT integrated loudness to Current chain; keep other tracks fixed, then apply one common audition gain"}
        else:
            comparison = versions
            gains = {v: -24-stats[scope][v]["loudness"]["integrated_lufs"] for v in variants}
            common = min(0, -3-max(stats[scope][v]["loudness"]["true_peak_dbtp"]+gains[v] for v in variants))
            gains = {v: gains[v]+common for v in variants}
            matching[scope] = {"target_lufs": -24+common, "common_safety_gain_db": common,
                "method": "FFmpeg EBU R128 integrated loudness, 0.1 LU scanner precision"}
        matching[scope].update(variant_gain_db=gains, true_peak_ceiling_dbtp=-3)
        original_safety = min(0, -3-max(stats[scope][v]["loudness"]["true_peak_dbtp"] for v in variants))
        matching[scope]["original_balance_safety_gain_db"] = original_safety
        for variant, audio in versions.items():
            sf.write(output/audio_files[f"study_{scope}_{variant}"], audio*10**(original_safety/20), rate, subtype="FLOAT")
        matched_metrics = {}
        for variant, audio in comparison.items():
            key = f"study_{scope}_{variant}_matched"
            filename = "KIT-"+key+".wav"
            sf.write(output/filename, audio*10**(gains[variant]/20), rate, subtype="FLOAT")
            audio_files[key] = filename
            matched_metrics[variant] = scan_loudness(output/filename, ffmpeg)
            if scope == "kit":
                # Gating and scanner quantization can make one scalar estimate
                # miss its target. Re-measure finite audio instead of assuming.
                for _ in range(3):
                    correction = matching[scope]["target_lufs"]-matched_metrics[variant]["integrated_lufs"]
                    if abs(correction) <= .11:
                        break
                    gains[variant] += correction
                    sf.write(output/filename, audio*10**(gains[variant]/20), rate, subtype="FLOAT")
                    matched_metrics[variant] = scan_loudness(output/filename, ffmpeg)
            if matched_metrics[variant]["true_peak_dbtp"] > -2.85:
                raise ValueError("Matched comparison exceeded its true-peak ceiling")
            if scope == "kit" and abs(matched_metrics[variant]["integrated_lufs"]-matching[scope]["target_lufs"]) > .15:
                raise ValueError(f"KIT loudness matching failed verification: {variant}")
        matching[scope]["measured_matched_loudness"] = matched_metrics
    return audio_files, stats, matching


def build_study(baseline_folder, candidate_folder, output, *, ffmpeg, restored_snapshot,
                bypass_folder=None, passage=None):
    if output.exists():
        raise ValueError("Use a new study staging folder")
    baseline_folder, candidate_folder = Path(baseline_folder), Path(candidate_folder)
    meta = [json.loads((p/"KIT.json").read_text(encoding="utf-8")) for p in (baseline_folder, candidate_folder)]
    path = validate_linear_path(meta[0]["session_snapshot"])
    validate_linear_path(meta[1]["session_snapshot"])
    changes = allowed_session_change(meta[0]["session_snapshot"], meta[1]["session_snapshot"])
    folders = [("baseline", baseline_folder), ("compression", candidate_folder)]
    bypass_meta = None
    if bypass_folder is not None:
        bypass_folder = Path(bypass_folder)
        bypass_meta = json.loads((bypass_folder/"KIT.json").read_text(encoding="utf-8"))
        validate_linear_path(bypass_meta["session_snapshot"])
        allowed_session_change(meta[0]["session_snapshot"], bypass_meta["session_snapshot"], parameter="Device On")
        if not isinstance(passage, dict) or passage.get("id") not in ("quiet", "loud", "fills"):
            raise ValueError("Bypass study requires an identified passage candidate")
        folders.append(("bypass", bypass_folder))
    expected, restored = copy.deepcopy(meta[0]["session_snapshot"]), copy.deepcopy(restored_snapshot)
    expected.pop("inspected_at", None)
    restored.pop("inspected_at", None)
    if expected != restored or restored["transport"]["is_playing"] or restored["transport"]["record_mode"]:
        raise ValueError("Captured native compressor settings were not verified restored")
    arrays = {}
    shape = None
    for label, folder in folders:
        for track, stage in (("KIT", "before"), ("KIT", "after"), ("MASTER", "after"), ("RED", "after")):
            audio, rate = sf.read(folder/f"{track}-{stage}.wav", dtype="float32", always_2d=True)
            if (rate != 48000 or audio.shape[1] != 2 or not 24000 <= len(audio) <= 960000
                    or shape is not None and audio.shape != shape or not np.isfinite(audio).all()):
                raise ValueError("Use matching complete 0.5–20 second stereo 48 kHz loop captures")
            shape = audio.shape
            arrays[label, track, stage] = audio
    duration = shape[0]/48000
    if bypass_folder is not None and passage.get("duration_seconds") != duration:
        raise ValueError("Passage length does not match its captured cycle")
    pre = arrays["baseline", "KIT", "before"]
    candidate_pre, source_alignment = align_cycle(pre, arrays["compression", "KIT", "before"], minimum_correlation=.99999)
    if not np.allclose(pre, candidate_pre, atol=1e-7, rtol=0):
        raise ValueError("KIT input changed or dropped samples between captures")
    base_kit, base_kit_alignment = align_cycle(pre, arrays["baseline", "KIT", "after"])
    comp_kit, comp_kit_alignment = align_cycle(pre, arrays["compression", "KIT", "after"])
    base_mix = arrays["baseline", "MASTER", "after"]
    comp_mix, mix_alignment = align_cycle(base_mix, arrays["compression", "MASTER", "after"], minimum_correlation=.98)
    delta_kit, delta_mix = comp_kit-base_kit, comp_mix-base_mix
    path_alignment, relative_error = verify_mix_delta(delta_mix, delta_kit)
    offset = path_alignment["roll_target_samples"]
    base_kit, comp_kit = np.roll(base_kit, offset, axis=0), np.roll(comp_kit, offset, axis=0)
    kit_versions = {"current": base_kit, "compression": comp_kit,
                    "eq": low_shelf(base_kit, rate), "both": low_shelf(comp_kit, rate)}
    mix_versions = {"current": base_mix, "compression": comp_mix,
                    "eq": base_mix+(kit_versions["eq"]-base_kit),
                    "both": comp_mix+(kit_versions["both"]-comp_kit)}
    bypass_validation = None
    if bypass_folder is not None:
        bypass_pre, bypass_source_alignment = align_cycle(pre, arrays["bypass", "KIT", "before"], minimum_correlation=.99999)
        if not np.allclose(pre, bypass_pre, atol=1e-7, rtol=0):
            raise ValueError("KIT input changed between current and bypass captures")
        bypass_kit, bypass_alignment = align_cycle(pre, arrays["bypass", "KIT", "after"], minimum_correlation=.99999)
        if not np.allclose(pre, bypass_kit, atol=1e-6, rtol=0):
            raise ValueError("Compressor bypass did not reproduce the pre-effects signal")
        bypass_kit = np.roll(bypass_kit, offset, axis=0)
        bypass_mix, bypass_mix_alignment = align_cycle(base_mix, arrays["bypass", "MASTER", "after"], minimum_correlation=.8)
        bypass_path_alignment, bypass_error = verify_mix_delta(bypass_mix-base_mix, bypass_kit-base_kit)
        if bypass_path_alignment["roll_target_samples"] != 0:
            raise ValueError("Bypass contribution and Master timing do not agree")
        kit_versions = {"bypass": bypass_kit, **kit_versions}
        mix_versions = {"bypass": bypass_mix, **mix_versions}
        bypass_validation = {"source_alignment": bypass_source_alignment, "kit_alignment": bypass_alignment,
            "mix_alignment": bypass_mix_alignment, "linear_mix_null_relative_error": bypass_error,
            "post_matches_raw_at_unity": True, "output_gain_also_bypassed": True}
    output.mkdir(parents=True)
    audio_files, stats, matching = write_auditions(kit_versions, mix_versions, output, rate, ffmpeg,
                                                match_kit_in_mix=bypass_folder is not None)
    comparison = {"kind": "kit_eq_compression_study", "duration_seconds": duration,
        "variants": [{"id": "current", "label": "Current chain", "origin": "Captured from Ableton"},
            {"id": "compression", "label": "Slower attack", "origin": "Captured from Ableton with 15 ms attack"},
            {"id": "eq", "label": "Less low end", "origin": "Local EQ preview on captured current KIT"},
            {"id": "both", "label": "Attack + EQ", "origin": "Captured 15 ms attack plus local EQ preview"}],
        "eq": {"type": "low_shelf", "hz": 120, "gain_db": -2, "placement": "after compressor",
               "status": "Listening hypothesis; not installed or applied in Ableton"},
        "compression_change": changes, "path": path, "matching": matching, "measurements": stats,
        "restore": {"verified": True, "inspected_at": restored_snapshot["inspected_at"],
                    "scope": "All host-exposed session controls match the baseline after capture"},
        "validation": {"source_alignment": source_alignment, "base_kit_alignment": base_kit_alignment,
            "compression_kit_alignment": comp_kit_alignment, "mix_alignment": mix_alignment,
            "kit_to_master_alignment": path_alignment, "linear_mix_null_relative_error": relative_error},
        "notes": ["Spectral energy proportions are descriptors, not proof that an EQ change is needed.",
            f"{stats['kit']['current']['band_energy_fractions']['sub_30']*100:.2f}% of KIT energy is below 30 Hz; no automatic rumble filter was added.",
            "Native compression captures are real. EQ full-mix versions use validated linear substitution, not an Ableton EQ render.",
            "The baseline compressor attack is restored after capture. The previous output-gain fix is retained.",
            "Compare kick/snare definition, bass clarity and lead-vocal intelligibility; keep current if a candidate does not help.",
            "Source clipping remains; neither EQ nor compression reconstructs clipped transients."]}
    if bypass_folder is not None:
        comparison["passage"] = passage
        comparison["variants"].insert(0, {"id": "bypass", "label": "Compressor bypass",
            "origin": "Actual Ableton capture with compressor disabled; matched listening retains comparable KIT loudness"})
        comparison["validation"]["bypass"] = bypass_validation
        comparison["analysis"] = {"input": observe(pre, rate, "kit"),
            "versions": {v: observe(audio, rate, "kit", stage="post_fx") for v, audio in kit_versions.items()},
            "preference_established": False,
            "scope": "Measured descriptors, not confirmed kick/snare detection or a professional-quality score"}
        comparison["notes"] += [
            "Matched full mixes hold KIT integrated loudness equal to Current chain and keep the other tracks fixed; total mix LUFS may differ slightly.",
            "Device bypass also bypasses compressor Output Gain. Use matched listening to judge its processing, not the unmatched level difference.",
            "Quiet/loud/busy passage selection uses energy heuristics. Chorus/fill identity needs listening confirmation.",
            "These small KIT changes do not diagnose the overall quality gap to a professional church production."]
    report = {"kind": "wcfi_kit_context_study", "comparison": comparison, "audio": audio_files,
              "proposals": [], "provenance": {"post_fx_kind": "captured_context_study"}}
    # An already-open older frontend can still show honest baseline observations
    # until the operator refreshes, instead of crashing on the new report kind.
    profile = get_profile("kit").describe()
    profile["analyses"] = []
    report.update(profile=profile, duration_seconds=duration, sample_rate=rate, channels=2,
        elapsed_seconds=0, paired_alignment_available=False, chain_revision=None,
        before={"metrics": health(features(pre, rate)), "analyses": {}, "findings": []},
        after={"metrics": health(features(base_kit, rate)), "analyses": {}, "findings": [{
            "kind": "context_study_available", "note": "A KIT full-mix comparison is ready. Refresh this app window to see its EQ and compression listening controls."}]},
        apply={"supported": False, "reason": "Listening comparison only; native settings were restored"})
    result = {"kind": "wcfi_kit_study_package", "schema_version": 2 if bypass_folder is not None else 1, "reports": {"KIT": report},
              "files": {filename: hashlib.sha256((output/filename).read_bytes()).hexdigest() for filename in audio_files.values()}}
    (output/"study.json").write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
    return result
