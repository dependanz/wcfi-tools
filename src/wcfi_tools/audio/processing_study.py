"""Versioned local musical-processing comparisons and verified audition assets."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from .kit_study import scan_loudness
from .rehearsal_suite import PASSAGES

SCOPES = {"red": ("current", "eq", "compression", "deess", "chain"),
          "kit": ("current", "eq", "compression", "chain"),
          "mix": ("current", "red_eq", "red_compression", "red_deess", "red_chain",
                  "kit_eq", "kit_compression", "kit_chain", "both_chain")}


def processing_audio_keys():
    return {f"process_{p}_{scope}_{v}{suffix}" for p in PASSAGES for scope, variants in SCOPES.items()
            for v in variants for suffix in ("", "_matched")}


def signal_metrics(audio):
    rms = max(float(np.sqrt(np.mean(audio.astype(np.float64)**2))), 1e-24)
    peak = max(float(np.max(np.abs(audio))), 1e-24)
    return {"sample_peak_dbfs": 20*math.log10(peak), "rms_dbfs": 20*math.log10(rms),
            "crest_db": 20*math.log10(peak/rms)}


def _write_group(output, passage, scope, originals, matched_source, ffmpeg):
    identifier = passage["id"]
    stats, prepared = {}, {}
    for variant, audio in originals.items():
        path = output/f"MIX-process_{identifier}_{scope}_{variant}.wav"
        sf.write(path, audio, 48000, subtype="FLOAT")
        stats[variant] = scan_loudness(path, ffmpeg) | signal_metrics(audio)
    matching_stats = {}
    for variant, audio in matched_source.items():
        if audio is originals[variant]:
            matching_stats[variant] = stats[variant]
        else:
            path = output/f"MIX-process_{identifier}_{scope}_{variant}_matched.wav"
            sf.write(path, audio, 48000, subtype="FLOAT")
            matching_stats[variant] = scan_loudness(path, ffmpeg)
    if any(not math.isfinite(s["integrated_lufs"]) for s in matching_stats.values()):
        raise ValueError("Cannot match a silent processing source")
    target = min(-24, min(-3-s["true_peak_dbtp"]+s["integrated_lufs"] for s in matching_stats.values()))
    safety = min(0, -3-max(s["true_peak_dbtp"] for s in stats.values()))
    files, audio_map = {}, {}
    for variant, audio in originals.items():
        for suffix in ("", "_matched"):
            key = f"process_{identifier}_{scope}_{variant}{suffix}"
            path = output/f"MIX-{key}.wav"
            samples = matched_source[variant] if suffix else audio
            gain = target-matching_stats[variant]["integrated_lufs"] if suffix else safety
            for _ in range(4 if suffix else 1):
                sf.write(path, samples*10**(gain/20), 48000, subtype="FLOAT")
                if not suffix:
                    break
                checked = scan_loudness(path, ffmpeg)
                if abs(checked["integrated_lufs"]-target) <= .11:
                    break
                gain += target-checked["integrated_lufs"]
            if suffix:
                if abs(checked["integrated_lufs"]-target) > .15 or checked["true_peak_dbtp"] > -2.85:
                    raise ValueError("Processing comparison match failed")
                prepared[variant] = checked | {"global_audition_gain_db": gain}
            audio_map[key] = path.name
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"target_lufs": target, "shared_original_gain_db": safety,
        "original": stats, "matched": prepared}, files, audio_map


def write_passage(output, passage, master, role_versions, *, ffmpeg):
    """Preserve the captured remainder; normalize treated sources before matching the mix."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    frames = round(passage["duration_seconds"]*48000)
    if master.shape != (frames, 2) or not np.isfinite(master).all():
        raise ValueError("Invalid captured mix")
    files, audio_map, listening, source_gains = {}, {}, {}, {}
    for scope in ("red", "kit"):
        versions = role_versions[scope]
        if tuple(versions) != SCOPES[scope] or any(a.shape != master.shape or not np.isfinite(a).all() for a in versions.values()):
            raise ValueError("Use complete finite processing variants")
        stats, assets, mapped = _write_group(output, passage, scope, versions, versions, ffmpeg)
        listening[scope] = stats
        files.update(assets);audio_map.update(mapped)
        current_lufs = stats["original"]["current"]["integrated_lufs"]
        source_gains[scope] = {v: current_lufs-s["integrated_lufs"] for v, s in stats["original"].items()}
        if any(not math.isfinite(g) or abs(g) > 6 for g in source_gains[scope].values()):
            raise ValueError("Treatment requires too much source loudness compensation")
    mixes, context, residuals = {"current": master}, {"current": master}, {}
    for variant in SCOPES["mix"][1:]:
        touched = [("red", "chain"), ("kit", "chain")] if variant == "both_chain" else [tuple(variant.split("_", 1))]
        original_mix = master.astype(np.float64).copy()
        context_mix = master.astype(np.float64).copy()
        remainder = master.astype(np.float64).copy()
        for scope, treatment in touched:
            current, candidate = role_versions[scope]["current"], role_versions[scope][treatment]
            original_mix += candidate-current
            context_mix += candidate*10**(source_gains[scope][treatment]/20)-current
            remainder -= current
        mixes[variant] = original_mix.astype(np.float32)
        context[variant] = context_mix.astype(np.float32)
        reconstructed = context[variant].astype(float).copy()
        for scope, treatment in touched:
            reconstructed -= role_versions[scope][treatment]*10**(source_gains[scope][treatment]/20)
        error = float(np.sqrt(np.mean((reconstructed-remainder)**2))/max(1e-12, np.sqrt(np.mean(remainder**2))))
        if error > 1e-5:
            raise ValueError("Other-track remainder changed during substitution")
        residuals[variant] = error
    stats, assets, mapped = _write_group(output, passage, "mix", mixes, context, ffmpeg)
    listening["mix"] = stats
    files.update(assets);audio_map.update(mapped)
    passage.update(listening=listening, source_loudness_compensation_db=source_gains,
        remainder_relative_errors=residuals,
        matching_method="Match each treated source to its current integrated LUFS before reinserting it; then scalar-match each full-mix audition. Original-level mode contains no compensation.")
    return files, audio_map


def validate_processing_package(folder):
    folder = Path(folder).resolve()
    manifest = folder/"study.json"
    if manifest.stat().st_size > 2*1024*1024:
        raise ValueError("Processing study manifest is too large")
    raw = manifest.read_bytes();package = json.loads(raw)
    if (package.get("kind") != "wcfi_processing_study_package" or type(package.get("schema_version")) is not int
            or package["schema_version"] != 1):
        raise ValueError("Not a supported processing study")
    report = package["reports"]["MIX"]
    if (report.get("kind") != "wcfi_processing_study" or report.get("live_writes") != 0
            or report.get("preference_established") is not False
            or [p["id"] for p in report["passages"]] != list(PASSAGES)):
        raise ValueError("Processing study must identify local-only, unvalidated preferences")
    keys = processing_audio_keys()
    if (set(report["audio"]) != keys or set(package["files"]) != {f"MIX-{k}.wav" for k in keys}
            or any(report["audio"][k] != f"MIX-{k}.wav" for k in keys)):
        raise ValueError("Invalid processing asset names")
    for passage in report["passages"]:
        duration = passage["duration_seconds"]
        if (type(duration) not in (int, float) or not .5 <= duration <= 20
                or type(passage.get("operator_approved")) is not bool
                or type(passage.get("baseline_sum_relative_error")) not in (int, float)
                or not 0 <= passage["baseline_sum_relative_error"] <= .001
                or set(passage["remainder_relative_errors"]) != set(SCOPES["mix"][1:])
                or any(type(v) not in (int, float) or not 0 <= v <= 1e-5 for v in passage["remainder_relative_errors"].values())):
            raise ValueError("Processing study needs verified linear source substitution")
        for role in ("red", "kit"):
            compensation = passage["source_loudness_compensation_db"][role]
            if (set(compensation) != set(SCOPES[role])
                    or any(type(g) not in (int, float) or not -6 <= g <= 6 for g in compensation.values())):
                raise ValueError("Source loudness compensation is unverified")
            recipe = passage["tracks"][role]["recipe"]
            if (recipe.get("application") != "local_listening_preview_only"
                    or recipe.get("native_plugin_equivalence") is not False or recipe.get("role") != role
                    or role == "kit" and recipe["deess"]["enabled"] is not False):
                raise ValueError("Processing study must preserve role and local DSP provenance")
        for scope, variants in SCOPES.items():
            target = passage["listening"][scope]["target_lufs"]
            if type(target) not in (int, float) or not -60 <= target <= -18:
                raise ValueError("Invalid processing loudness target")
            for variant in variants:
                matched = passage["listening"][scope]["matched"][variant]
                if (not math.isfinite(matched["integrated_lufs"]) or abs(matched["integrated_lufs"]-target) > .15
                        or not math.isfinite(matched["true_peak_dbtp"]) or matched["true_peak_dbtp"] > -2.85):
                    raise ValueError("Processing loudness matching is unverified")
                for suffix in ("", "_matched"):
                    key = f"process_{passage['id']}_{scope}_{variant}{suffix}"
                    filename = report["audio"][key];path = (folder/filename).resolve()
                    if path.parent != folder or path.stat().st_size > 16*1024*1024:
                        raise ValueError("Processing assets must stay inside staging")
                    info = sf.info(path)
                    if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
                        raise ValueError("Invalid processing audio format")
                    audio, _ = sf.read(path, dtype="float32", always_2d=True)
                    if (not np.isfinite(audio).all() or np.max(np.abs(audio)) > .999
                            or hashlib.sha256(path.read_bytes()).hexdigest() != package["files"][filename]):
                        raise ValueError("Invalid processing audio or checksum")
    return report, package["files"], hashlib.sha256(raw).hexdigest()
