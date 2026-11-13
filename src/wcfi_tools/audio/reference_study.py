"""Local full-mix reference comparisons with verified gain matching, not quality scores."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import welch

from .kit_study import scan_loudness

PASSAGES = ("quiet", "loud", "fills")
REFERENCE_AUDIO = {f"reference_{p}_{s}_matched" for p in PASSAGES for s in ("wcfi", "bethel")}
BANDS = (("sub", 20, 80), ("bass", 80, 200), ("low_mid", 200, 500),
         ("mid", 500, 1000), ("presence", 1000, 3000), ("upper_mid", 3000, 6000),
         ("high", 6000, 12000), ("air", 12000, 20000))


def describe_mix(audio, rate):
    audio = np.asarray(audio, dtype=np.float64)
    if (rate != 48000 or audio.ndim != 2 or audio.shape[1] != 2 or not 24000 <= len(audio) <= 960000
            or not np.isfinite(audio).all()):
        raise ValueError("Use a finite 0.5–20 second stereo 48 kHz excerpt")
    frequency, power = welch(audio, fs=rate, nperseg=8192, noverlap=4096, axis=0)
    power = power.mean(axis=1)
    total = max(float(power[(frequency >= 20) & (frequency < 20000)].sum()), 1e-30)
    band_power = {name: float(power[(frequency >= low) & (frequency < high)].sum()) for name, low, high in BANDS}
    def ratio(a, b):
        return float(10*np.log10(max(a, 1e-30)/max(b, 1e-30)))
    left, right = audio.T
    mid, side = (left+right)/2, (left-right)/2
    em, es = float(np.mean(mid**2)), float(np.mean(side**2))
    denominator = math.sqrt(float(np.sum(left**2)*np.sum(right**2)))
    rms = float(np.sqrt(np.mean(audio**2)))
    peak = float(np.max(np.abs(audio)))
    return {"band_energy_percent": {name: 100*p/total for name, p in band_power.items()},
        "band_power": band_power, "low_mid_over_presence_db": ratio(band_power["low_mid"], band_power["presence"]),
        "low_end_over_low_mid_db": ratio(band_power["sub"]+band_power["bass"], band_power["low_mid"]),
        "sub_over_presence_db": ratio(band_power["sub"], band_power["presence"]),
        "high_over_presence_db": ratio(band_power["high"], band_power["presence"]),
        "side_energy_percent": 100*es/max(em+es, 1e-30),
        "lr_correlation": float(np.sum(left*right)/denominator) if denominator > 1e-20 else None,
        "mono_energy_change_db": ratio(em, (em+es)),
        "rms_dbfs": 20*math.log10(max(rms, 1e-12)), "sample_peak_dbfs": 20*math.log10(max(peak, 1e-12)),
        "crest_db": 20*math.log10(max(peak, 1e-12)/max(rms, 1e-12))}


def reference_source(report):
    return report.get("reference_key", "bethel")


def reference_audio_keys(report):
    return {f"reference_{p['id']}_{s}_matched" for p in report["pairs"] for s in ("wcfi", reference_source(report))}


def priorities(pairs, source_audit, *, reference_key="bethel", reference_name="Bethel"):
    """Use eligible performances only; one passage cannot establish repeatability."""
    if not pairs:
        return [{"id": "selection", "title": "Confirm a representative performance passage",
            "evidence_type": "listening_required", "evidence": "No performance passage is approved for reference comparison.",
            "next_test": "Choose a coherent band performance; jams and level-check signals cannot validate a musical mix.",
            "automatically_apply": False}]
    findings = []
    clipped = sorted({s["track"] for s in source_audit if s["full_scale_samples"] or s["possible_flat_top_samples"] >= 12})
    if clipped:
        findings.append({"id": "capture", "title": "Investigate input clipping first", "evidence_type": "source_measurement",
            "evidence": f"Full-scale/flat-top indicators occur in: {', '.join(clipped)}. Their exact upstream cause is unknown.",
            "next_test": "Check the affected instrument output and physical analogue mixer trim when connected. Existing clipping will remain in these recordings after software gain reduction.",
            "automatically_apply": False})
    descriptions = (
        ("space", "side_energy_percent", 8, "Stereo presentation", "percentage points",
         "Inspect the recorded stereo KEYS/KIT sources, centred instrument placement and the currently unused returns. Audition small placement/ambience changes while keeping the lead, kick and bass anchored; check mono."),
        ("low_mid", "low_mid_over_presence_db", 3, "Low-mid concentration", "dB",
         "Check the source list for strong 200–500 Hz content. Audition a small source-level balance or EQ change on one contributor while keeping RED fixed; do not copy the reference spectrum onto Master."),
        ("foundation", "sub_over_presence_db", 3, "Low-frequency foundation", "dB",
         "Compare bass notes and kick separately in context. Check their input presence and relative balance before adding a broad bass boost or cut."),
        ("foundation_body", "low_end_over_low_mid_db", 3, "Bass/kick foundation versus low mids", "dB",
         "Compare BASS, KIT and KEYS in context. Check their balance and which sources occupy 200–500 Hz before changing Master EQ. This measures the 20–200 Hz / 200–500 Hz energy ratio; song key and arrangement remain confounds."),
        ("brightness", "high_over_presence_db", 3, "High-frequency balance", "dB",
         "Distinguish vocal consonants from cymbals and guitar content by source listening before choosing EQ or de-essing. The full-mix ratio cannot identify the offending source."))
    candidates = []
    for identity, key, threshold, title, unit, next_test in descriptions:
        deltas = [p["wcfi"]["metrics"][key]-p[reference_key]["metrics"][key] for p in pairs]
        median = float(np.median(deltas))
        consistent = sum(abs(d) >= threshold and np.sign(d) == np.sign(median) for d in deltas)
        if consistent < min(2, len(pairs)):
            continue
        direction = "higher" if median > 0 else "lower"
        if identity == "low_mid":
            tracks = {s["track"] for s in source_audit if s["track"] not in ("LAPTOP", "MDMIC Talkback")}
            powers = {track: float(np.median([s["metrics"]["band_power"]["low_mid"] for s in source_audit if s["track"] == track])) for track in tracks}
            top = sorted(powers, key=lambda track: powers[track], reverse=True)[:3]
            next_test += f" Strong raw-source candidates in these windows: {', '.join(top)}. Raw source powers are not additive shares of the mix."
        evidence = (f"WCFI is {abs(median):.1f} {unit} {direction} in the one approved passage. Repeatability across performances is untested."
                    if len(pairs) == 1 else
                    f"WCFI is {abs(median):.1f} {unit} {direction} at the median; {consistent}/{len(pairs)} approved pairs exceed the screening threshold in that direction.")
        candidates.append((abs(median)/threshold, {"id": identity, "title": title, "evidence_type": "reference_difference_hypothesis",
            "evidence": evidence,
            "delta_by_passage": deltas, "next_test": next_test, "automatically_apply": False}))
    findings.extend(f for _, f in sorted(candidates, key=lambda c: c[0], reverse=True))
    findings.append({"id": "vocal_balance", "title": "Judge lead placement against the band", "evidence_type": "listening_required",
        "evidence": "Lead-vocal intelligibility, masking and reverb quality are not established by spectral ratios or stem RMS. Different singers and arrangements affect this comparison.",
        "next_test": f"Switch WCFI and {reference_name} at the matched level. Listen for how easily the lead words follow above the band. Establish the vocal/instrument balance before fine EQ; no automatic boost is prescribed.",
        "automatically_apply": False})
    return findings


def reviewed_reference(report, assessments):
    """Materialize operator eligibility separately from immutable captured evidence."""
    pairs = report["pairs"]
    included = [p | {"label": f"Band performance · {p['wcfi']['start_seconds']:g}–{p['wcfi']['start_seconds']+p['duration_seconds']:g}s"}
                for p in pairs if assessments.get(p["id"], {}).get("include") is True]
    ids = {p["id"] for p in included}
    source_audit = [s for s in report["source_audit"] if s.get("passage") in ids]
    return report | {"pairs": included, "source_audit": source_audit,
        "findings": priorities(included, source_audit, reference_key=reference_source(report),
            reference_name=report.get("reference_name", "Bethel")), "approved_passage_count": len(included),
        "passage_review": [{"id": p["id"], "label": p["label"],
            "start_seconds": p["wcfi"]["start_seconds"], "duration_seconds": p["duration_seconds"],
            **assessments.get(p["id"], {"include": False, "reason": "Awaiting operator confirmation", "origin": "unverified"})} for p in pairs],
        "performance_confirmation_required": True}


def build_reference(pairs, output, *, ffmpeg, source_audit, session_context, reference_name=None,
                    reference_kind="service_broadcast"):
    output = Path(output)
    version = 1 if reference_name is None else 2
    ids = {p["id"] for p in pairs}
    if (output.exists() or not 1 <= len(pairs) <= 3 or len(ids) != len(pairs) or not ids <= set(PASSAGES)
            or version == 1 and ids != set(PASSAGES)):
        raise ValueError("Use a new package folder and distinct bounded passage pairs")
    if version == 2 and (not isinstance(reference_name, str) or not 1 <= len(reference_name) <= 80
                        or reference_kind not in ("service_broadcast", "released_live_performance")):
        raise ValueError("Use a short reference name and supported performance type")
    reference_key = "bethel" if version == 1 else "reference"
    name = reference_name or "Bethel"
    sources = ("wcfi", reference_key)
    output.mkdir(parents=True)
    prepared, audio, report_pairs = [], {}, []
    for pair in pairs:
        report_pair = {k: v for k, v in pair.items() if k not in sources}
        for source in sources:
            item = pair[source]
            samples, rate = sf.read(item["path"], dtype="float32", always_2d=True)
            metrics = describe_mix(samples, rate)
            key = f"reference_{pair['id']}_{source}_matched"
            filename = f"MIX-{key}.wav"
            path = output/filename
            sf.write(path, samples, rate, subtype="FLOAT")
            original = scan_loudness(path, ffmpeg)
            gain = -24-original["integrated_lufs"]
            if not math.isfinite(gain) or abs(gain) > 36:
                raise ValueError("Reference is too quiet or loud for the comparison budget")
            prepared.append({"path": path, "samples": samples, "gain": gain, "key": key,
                "true_peak": original["true_peak_dbtp"], "source": source, "pair_id": pair["id"]})
            audio[key] = filename
            report_pair[source] = {k: v for k, v in item.items() if k != "path"} | {
                "metrics": metrics | original, "source_sha256": hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()}
        if sf.info(pair["wcfi"]["path"]).frames != sf.info(pair[reference_key]["path"]).frames:
            raise ValueError("Comparison excerpts need matching durations")
        report_pairs.append(report_pair)
    safety = min(0, -3-max(p["true_peak"]+p["gain"] for p in prepared))
    target = -24+safety
    for item in prepared:
        gain = item["gain"]+safety
        for attempt in range(4):
            sf.write(item["path"], item["samples"]*10**(gain/20), 48000, subtype="FLOAT")
            checked = scan_loudness(item["path"], ffmpeg)
            correction = target-checked["integrated_lufs"]
            if abs(correction) <= .11:
                break
            gain += correction
        if abs(checked["integrated_lufs"]-target) > .15 or checked["true_peak_dbtp"] > -2.85:
            raise ValueError("Reference loudness/true-peak verification failed")
        pair = next(p for p in report_pairs if p["id"] == item["pair_id"])
        pair[item["source"]].update(matched=checked, matching_gain_db=gain)
    report = {"kind": "wcfi_reference_study", "title": f"WCFI ↔ {name}", "pairs": report_pairs, "audio": audio,
        "target_lufs": target, "source_audit": source_audit, "session_context": session_context,
        "findings": [], "preference_established": False, "performance_confirmation_required": True,
        "method": "Scalar-only EBU R128 matching verified after writing; stereo 48 kHz; no EQ, limiter or model inference",
        "limitations": ["Different songs, performances, arrangements and section densities; not a controlled mix of identical stems.",
            "WCFI is the captured Master node before its fader, not the ATEM or encoded livestream.",
            f"{name} is decoded public audio; YouTube player normalization is not included.",
            "A repeated spectral/spatial difference is a test priority, not proof of an audible defect or an EQ prescription.",
            "No source separation, auditory quality model, vocal-mask detector or reverb-quality estimate was used.",
            "Short excerpts do not support choosing a broadcast loudness target or mastering compressor from LRA."]}
    if version == 2:
        report.update(reference_key=reference_key, reference_name=name, reference_kind=reference_kind)
        report["limitations"].append("Released live performances may include editing, overdubs and mastering beyond an everyday service broadcast."
            if reference_kind == "released_live_performance" else "Archived service audio may still include broadcast processing; its production chain is unknown.")
    package = {"kind": "wcfi_reference_package", "schema_version": version, "reports": {"MIX": report},
        "files": {name: hashlib.sha256((output/name).read_bytes()).hexdigest() for name in audio.values()}}
    (output/"study.json").write_text(json.dumps(package, allow_nan=False), encoding="utf-8")
    return package
