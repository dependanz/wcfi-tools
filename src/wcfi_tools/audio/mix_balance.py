"""Local full-mix experiments, with contribution checks rather than quality scores."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import csd, sosfilt, tf2sos

from .kit_study import align_cycle, scan_loudness
from .reference_study import describe_mix

AUDIO_KEYS = {f"balance_{variant}{suffix}" for variant in ("before", "after") for suffix in ("", "_matched")}


def balance_variants(schema_version, report=None):
    if type(schema_version) is not int or schema_version not in (1, 2, 3, 4, 5, 6):
        raise ValueError("Not a supported balance study version")
    if schema_version == 6:
        descriptions = report.get("variants") if isinstance(report, dict) else None
        if (not isinstance(descriptions, list) or not 3 <= len(descriptions) <= 6
                or any(not isinstance(v, dict) for v in descriptions)):
            raise ValueError("A foundation study needs 3–6 described native variants")
        ids = tuple(v.get("id") for v in descriptions)
        if (any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", v) for v in ids)
                or ids[0] != "before" or len(set(ids)) != len(ids)):
            raise ValueError("Use distinct bounded foundation IDs starting with before")
        return ids
    if schema_version == 5:
        return ("before", "ac_down", "combined", *(
            f"ac{abs(ac)}_kit{kit}" for ac in (-24, -18, -12) for kit in (0, 3, 6)))
    if schema_version == 4:
        return ("before", "ac_down", "kit_up", "combined")
    return ("before", "after") if schema_version == 1 else ("before", "fader", "eq")


def balance_audio_keys(schema_version, report=None):
    return {f"balance_{v}{suffix}" for v in balance_variants(schema_version, report) for suffix in ("", "_matched")}


def bell_cut(audio, *, hz=330, gain_db=-3, q=1):
    """Bounded minimum-phase RBJ bell; warm one repeated period before returning."""
    audio = np.asarray(audio, dtype=np.float32)
    describe_mix(audio, 48000)
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (hz, gain_db, q))
            or not 200 <= hz <= 500 or not -3 <= gain_db <= 0 or not .5 <= q <= 2):
        raise ValueError("Use a 200–500 Hz bell cut, up to 3 dB, Q 0.5–2")
    a = 10**(gain_db/40)
    omega = 2*math.pi*hz/48000
    alpha = math.sin(omega)/(2*q)
    cosine = math.cos(omega)
    sos = tf2sos([1+alpha*a, -2*cosine, 1-alpha*a], [1+alpha/a, -2*cosine, 1-alpha/a])
    return sosfilt(sos, np.tile(audio, (2, 1)), axis=0)[len(audio):].astype(np.float32)


def substitute_source(master, source, filtered, *, sum_relative_error):
    """For a separately verified linear path only; preserve the rest of the mix."""
    if (type(sum_relative_error) not in (int, float) or not 0 <= sum_relative_error <= .001
            or master.shape != source.shape or source.shape != filtered.shape):
        raise ValueError("EQ substitution requires a verified linear sum and matching captures")
    for samples in (master, source, filtered):
        describe_mix(samples, 48000)
    return (master.astype(np.float64)-source+filtered).astype(np.float32)


def rank_low_mid(mix, contributions):
    """Require a verified linear sum; signed cross-power accounts for correlated bleed."""
    if not contributions or len(contributions) > 16:
        raise ValueError("Use 1–16 measured track contributions")
    total = sum(np.asarray(x, np.float64) for x in contributions.values())
    aligned, alignment = align_cycle(total, mix, minimum_correlation=.999)
    error = float(np.sqrt(np.mean((aligned-total)**2))/max(1e-12, np.sqrt(np.mean(aligned.astype(float)**2))))
    if error > .001:
        raise ValueError("Track contributions do not reconstruct the captured Master")
    master_power = describe_mix(aligned, 48000)["band_power"]["low_mid"]
    ranked = []
    for name, x in contributions.items():
        metrics = describe_mix(x, 48000)
        frequency, cross = csd(x, aligned, fs=48000, nperseg=8192, noverlap=4096, axis=0)
        covariance = float(cross[(frequency >= 200) & (frequency < 500)].real.mean(axis=1).sum())
        removed = describe_mix(aligned-x, 48000)["band_power"]["low_mid"]
        ranked.append({"track": name, "signed_mix_low_mid_percent": 100*covariance/max(master_power, 1e-30),
            "isolated_low_mid_power": metrics["band_power"]["low_mid"],
            "low_mid_over_presence_db": metrics["low_mid_over_presence_db"],
            "low_mid_drop_if_removed_db": 10*math.log10(max(master_power, 1e-30)/max(removed, 1e-30)),
            "sample_peak_dbfs": metrics["sample_peak_dbfs"]})
    return sorted(ranked, key=lambda item: item["signed_mix_low_mid_percent"], reverse=True), {
        "sum_relative_error": error, "alignment": alignment,
        "attribution_sum_percent": sum(x["signed_mix_low_mid_percent"] for x in ranked),
        "method": "Signed 200–500 Hz source/Master cross-power after verified linear summation. Values can be negative with cancellation; not perceptual masking scores."}


def build_balance(before, after, output, *, ffmpeg, report):
    """Write finite captures and independently verified scalar-matched listening copies."""
    return build_balance_versions({"before": before, "after": after}, output,
        ffmpeg=ffmpeg, report=report, schema_version=1)


def build_balance_versions(versions, output, *, ffmpeg, report, schema_version):
    expected = balance_variants(schema_version, report)
    if tuple(versions) != expected:
        raise ValueError("Use the complete ordered comparison variants")
    output = Path(output)
    if output.exists():
        raise ValueError("Choose a new balance package folder")
    before = versions["before"]
    if any(x.shape != before.shape for x in versions.values()):
        raise ValueError("Full-mix captures must have matching shapes")
    for samples in versions.values():
        describe_mix(samples, 48000)
    output.mkdir(parents=True)
    originals = {}
    for variant, samples in versions.items():
        path = output/f"MIX-balance_{variant}.wav"
        sf.write(path, samples, 48000, subtype="FLOAT")
        originals[variant] = describe_mix(samples, 48000) | scan_loudness(path, ffmpeg)
    target = min(-24, min(-3-m["true_peak_dbtp"]+m["integrated_lufs"] for m in originals.values()))
    matched = {}
    # The original-balance pair uses one common safe gain and retains level changes.
    safety = min(0, -3-max(m["true_peak_dbtp"] for m in originals.values()))
    for variant, samples in versions.items():
        sf.write(output/f"MIX-balance_{variant}.wav", samples*10**(safety/20), 48000, subtype="FLOAT")
        gain = target-originals[variant]["integrated_lufs"]
        path = output/f"MIX-balance_{variant}_matched.wav"
        for _ in range(4):
            sf.write(path, samples*10**(gain/20), 48000, subtype="FLOAT")
            checked = scan_loudness(path, ffmpeg)
            if abs(checked["integrated_lufs"]-target) <= .11:
                break
            gain += target-checked["integrated_lufs"]
        if abs(checked["integrated_lufs"]-target) > .15 or checked["true_peak_dbtp"] > -2.85:
            raise ValueError("Full-mix listening match failed")
        matched[variant] = checked | {"matching_gain_db": gain}
    report = report | {"kind": "wcfi_mix_balance_study", "duration_seconds": len(before)/48000,
        "target_lufs": target, "shared_original_gain_db": safety, "measurements": originals,
        "matched": matched, "preference_established": False,
        "audio": {key: f"MIX-{key}.wav" for key in balance_audio_keys(schema_version, report)}}
    package = {"kind": "wcfi_mix_balance_package", "schema_version": schema_version, "reports": {"MIX": report},
        "files": {name: hashlib.sha256((output/name).read_bytes()).hexdigest() for name in report["audio"].values()}}
    (output/"study.json").write_text(json.dumps(package, allow_nan=False), encoding="utf-8")
    return package
