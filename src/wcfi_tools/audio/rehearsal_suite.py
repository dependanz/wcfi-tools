"""Three-session RED/KIT evaluation, bounded headroom policy and saved auditions.

This module never opens a device or writes to Live. Native experiments are
performed separately and must be verified/restored before package publication.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from .kit_study import scan_loudness
from .review_apply import gain_target

PASSAGES = ("oct04", "oct11", "oct18")
SCOPES = {"red": ("raw", "current", "candidate"),
          "kit": ("raw", "current", "candidate"), "mix": ("current", "candidate")}
HEADROOM_TOLERANCE_DB = .25


def suite_audio_keys():
    return {f"suite_{p}_{scope}_{variant}{suffix}" for p in PASSAGES
            for scope, variants in SCOPES.items() for variant in variants for suffix in ("", "_matched")}


def correction_plan(report):
    """Only role-authorized attenuation; a descriptor is never an EQ instruction."""
    role = report["profile"]["name"]
    if role not in ("red", "kit"):
        raise ValueError("Suite supports RED and KIT profiles")
    track = report["chain_state"]["track"]
    plan = {"track": role.upper(), "kind": "keep", "gain_db": 0.0,
            "label": "Keep current", "target_stage": "unchanged",
            "headroom_tolerance_db": HEADROOM_TOLERANCE_DB,
            "reason": "Current output is within the configured sample-peak ceiling plus 0.25 dB tolerance. No validated tonal or dynamics rule authorizes another edit."}
    if report["after"]["metrics"]["sample_peak_dbfs"] < -65:
        plan["reason"] = "Insufficient signal; silence or bleed never authorizes an automatic boost."
        return plan
    if role == "red":
        volume = track["volume"]
        if volume["automation_state"] != 0 or volume["display"] == "-inf dB":
            return plan | {"reason": "Fader is automated or at silence; no automatic edit is eligible."}
        try:
            current_db = float(volume["display"].split()[0])
        except (ValueError, IndexError):
            return plan | {"reason": "Cannot verify the current fader's dB value."}
        delta = min(0.0, report["profile"]["peak_ceiling_dbfs"]
                    - report["after"]["metrics"]["sample_peak_dbfs"] - current_db)
        if delta >= -HEADROOM_TOLERANCE_DB:
            return plan
        delta = max(-report["profile"]["max_attenuation_db"], delta)
        return plan | {"kind": "fader", "gain_db": delta, "to_db": current_db+delta,
            "from_raw": volume["value"], "track_id": track["id"],
            "label": f"Fader {delta:.2f} dB", "target_stage": "after_mixer",
            "reason": "Test downstream sample-peak headroom using the existing fader. RED has no supported post-effects gain processor. Its after-effects node level is unchanged; this is not a vocal-balance target or clipping repair."}
    if (report["after"]["metrics"]["sample_peak_dbfs"] <= report["profile"]["peak_ceiling_dbfs"] + HEADROOM_TOLERANCE_DB
            or not any(p["kind"] == "software_gain" for p in report["proposals"])):
        return plan
    try:
        _state, _device, parameter, value = gain_target(report)
    except ValueError as exc:
        return plan | {"reason": f"Headroom correction withheld: {exc}"}
    delta = value-parameter["value"]
    return plan | {"kind": "parameter", "gain_db": delta, "from_raw": parameter["value"],
        "to_value": value, "parameter_id": parameter["id"], "target_stage": "after_effects",
        "label": f"Output gain {delta:.2f} dB",
        "reason": "Test the configured sample-peak headroom using the existing Compressor Output Gain. No threshold, ratio, attack, EQ or routing change is authorized by this rule."}


def build_suite(passages, versions, output, *, ffmpeg,
                context_scope="Older raw inputs replayed through one current Ableton chain, not historical broadcasts. Master node precedes its host mixer."):
    """Finite actual captures, safe originals and verified scalar LUFS matches."""
    if tuple(p["id"] for p in passages) != PASSAGES or set(versions) != set(PASSAGES):
        raise ValueError("Use the three ordered session passages")
    output = Path(output)
    if output.exists():
        raise ValueError("Choose a new suite output directory")
    output.mkdir(parents=True)
    audio_map, files = {}, {}
    for passage in passages:
        identifier = passage["id"]
        passage["listening"] = {}
        for scope, variants in SCOPES.items():
            source = versions[identifier][scope]
            if tuple(source) != variants:
                raise ValueError("Use complete ordered suite variants")
            stats = {}
            for variant, samples in source.items():
                if (samples.shape != (round(passage["duration_seconds"]*48000), 2)
                        or not np.isfinite(samples).all()):
                    raise ValueError("Invalid suite samples")
                key = f"suite_{identifier}_{scope}_{variant}"
                path = output/f"MIX-{key}.wav"
                sf.write(path, samples, 48000, subtype="FLOAT")
                stats[variant] = scan_loudness(path, ffmpeg)
            if any(not math.isfinite(s["integrated_lufs"]) for s in stats.values()):
                raise ValueError("Cannot loudness-match a silent suite signal")
            target = min(-24, min(-3-s["true_peak_dbtp"]+s["integrated_lufs"] for s in stats.values()))
            safety = min(0, -3-max(s["true_peak_dbtp"] for s in stats.values()))
            matched = {}
            for variant, samples in source.items():
                for suffix in ("", "_matched"):
                    key = f"suite_{identifier}_{scope}_{variant}{suffix}"
                    path = output/f"MIX-{key}.wav"
                    gain = target-stats[variant]["integrated_lufs"] if suffix else safety
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
                            raise ValueError("Suite loudness match failed")
                        matched[variant] = checked | {"gain_db": gain}
                    audio_map[key] = path.name
                    files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            passage["listening"][scope] = {"target_lufs": target, "original_safety_gain_db": safety,
                "original_measurements": stats, "matched": matched}
    report = {"kind": "wcfi_rehearsal_suite", "title": "RED & KIT · three-session evaluation",
        "passages": passages, "audio": audio_map, "restored": True, "preference_established": False,
        "scope": context_scope,
        "limits": ["Experimental role-specific deterministic checks; no automatic professional-quality claim.",
            "Known complete source loops establish cyclic comparison alignment, not host latency or a shared node clock.",
            "No automatic boost, EQ, de-essing or compression setting is chosen from spectral shape or energy alone.",
            "Gain-only changes largely disappear in matched solo comparisons; assess balance in the full mix or use original relative levels.",
            "Input clipping alerts cannot identify the exact analogue stage or reconstruct missing peaks.",
            "All native test edits and input settings were restored. Saved auditions never control Ableton."]}
    package = {"kind": "wcfi_rehearsal_suite_package", "schema_version": 1,
        "reports": {"MIX": report}, "files": files}
    (output/"study.json").write_text(json.dumps(package, allow_nan=False), encoding="utf-8")
    return package


def validate_suite_package(folder):
    """Validate all bounded assets and numerical checks before publication."""
    folder = Path(folder).resolve()
    manifest = folder/"study.json"
    if manifest.stat().st_size > 2*1024*1024:
        raise ValueError("Suite manifest is too large")
    raw = manifest.read_bytes()
    package = json.loads(raw)
    if (package.get("kind") != "wcfi_rehearsal_suite_package"
            or type(package.get("schema_version")) is not int or package["schema_version"] != 1):
        raise ValueError("Not a supported rehearsal suite")
    report = package["reports"]["MIX"]
    if (report.get("kind") != "wcfi_rehearsal_suite" or report.get("restored") is not True
            or report.get("preference_established") is not False
            or [p["id"] for p in report["passages"]] != list(PASSAGES)):
        raise ValueError("Suite needs three sessions and confirmed restoration")
    keys = suite_audio_keys()
    if (set(report["audio"]) != keys or set(package["files"]) != {f"MIX-{k}.wav" for k in keys}
            or any(report["audio"][k] != f"MIX-{k}.wav" for k in keys)):
        raise ValueError("Invalid suite asset names")
    for passage in report["passages"]:
        seconds = passage["duration_seconds"]
        if type(seconds) not in (int, float) or not .5 <= seconds <= 20:
            raise ValueError("Use bounded suite windows")
        if (type(passage.get("operator_approved")) is not bool
                or type(passage.get("start_seconds")) not in (int, float)
                or not 0 <= passage["start_seconds"] <= 86400
                or any(not isinstance(passage.get(k), str) or not 1 <= len(passage[k]) <= 160
                       for k in ("date", "take_id", "take_title"))):
            raise ValueError("Suite needs explicit source and performance metadata")
        validation = passage["validation"]
        error = validation["full_mix_delta_relative_error"]
        if type(error) not in (int, float) or not 0 <= error <= .001:
            raise ValueError("Suite full-mix change is unverified")
        unchanged = all(passage["tracks"][r]["decision"]["kind"] == "keep" for r in ("RED", "KIT"))
        if (validation.get("identical_control") is not unchanged
                or validation.get("candidate_native_capture") is not (not unchanged)):
            raise ValueError("Suite must distinguish native changes from unchanged controls")
        for name in ("RED", "KIT"):
            decision = passage["tracks"][name]["decision"]
            if (decision["kind"] not in ("keep", "fader", "parameter")
                    or type(decision["gain_db"]) not in (int, float)
                    or not -12 <= decision["gain_db"] <= 0
                    or (decision["kind"] == "keep") != (decision["gain_db"] == 0)):
                raise ValueError("Suite only supports bounded attenuation and unchanged controls")
        for scope, variants in SCOPES.items():
            listening = passage["listening"][scope]
            target = listening["target_lufs"]
            if type(target) not in (int, float) or not -60 <= target <= -18:
                raise ValueError("Invalid suite listening target")
            for variant in variants:
                matched = listening["matched"][variant]
                if (not math.isfinite(matched["integrated_lufs"])
                        or abs(matched["integrated_lufs"]-target) > .15
                        or not math.isfinite(matched["true_peak_dbtp"])
                        or matched["true_peak_dbtp"] > -2.85):
                    raise ValueError("Suite loudness matching is unverified")
                for suffix in ("", "_matched"):
                    key = f"suite_{passage['id']}_{scope}_{variant}{suffix}"
                    filename = report["audio"][key]
                    path = (folder/filename).resolve()
                    if path.parent != folder or path.stat().st_size > 16*1024*1024:
                        raise ValueError("Suite assets must stay inside staging")
                    info = sf.info(path)
                    if info.samplerate != 48000 or info.channels != 2 or info.frames != round(seconds*48000):
                        raise ValueError("Suite audio format does not match")
                    samples, _rate = sf.read(path, dtype="float32", always_2d=True)
                    if (not np.isfinite(samples).all() or np.max(np.abs(samples)) > .999
                            or hashlib.sha256(path.read_bytes()).hexdigest() != package["files"][filename]):
                        raise ValueError("Suite audio or checksum is invalid")
    return report, package["files"], hashlib.sha256(raw).hexdigest()
