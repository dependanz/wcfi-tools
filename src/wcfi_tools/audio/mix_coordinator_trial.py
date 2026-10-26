"""Reproducible file-only trial of the gain coordinator; never controls a host.

The three renders isolate unity summing, an exploratory static role balance,
and that same balance with causal corrections. The initial recipe is a listening
candidate, not a validated musical target or an accepted user preference.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from .mix_coordinator import CoordinatorSettings, MixCoordinator, TRACKS, EXCLUDED_TRACKS
from .kit_study import scan_loudness

RATE = 48000
ROLE_OFFSETS_DB = {"RED": 0., "YELLOW": -10., "GREEN": -10., "EGUITAR": -9.,
                   "AC GUITAR": -12., "KEYS": -10., "KIT": -3.}


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stereo(audio):
    if audio.shape[1] == 1:
        return np.repeat(audio, 2, axis=1)
    if audio.shape[1] != 2:
        raise ValueError("Only mono/stereo inputs are supported")
    return audio


def validate_sources(audit, date):
    """Allow exact named arrangement sources only, never enumerate take lanes."""
    if not isinstance(date, str) or len(date) != 10 or date[5:7] == "01":
        raise ValueError("An explicit non-January source date is required")
    sources = audit["sources"]
    if len(sources) != len(TRACKS) or {s["track"] for s in sources} != set(TRACKS):
        raise ValueError("Manifest must contain exactly the seven allowed tracks")
    if audit.get("identity_mapping") is not True:
        raise ValueError("This runner requires verified identity source-time mapping")
    by_name = {s["track"]: s for s in sources}
    paths = {}
    for name in TRACKS:
        s = by_name[name]
        p = Path(s["source_path"]).resolve(strict=True)
        if (s.get("source_date_from_filename") != date or f"[{date} " not in p.name
                or not p.name.startswith(name + " ") or p.suffix.lower() != ".wav"
                or s.get("disabled") != "false" or s.get("sample_volume") != 1
                or s.get("pan") != 0 or s.get("start_relative_beats") != 0):
            raise ValueError(f"Unverified source mapping for {name}")
        paths[name] = p
    return paths


def calibration(levels):
    """Use preceding calibration bins only; no later audio sets these values."""
    if levels.ndim != 2 or levels.shape[1] != len(TRACKS) or not np.isfinite(levels).all():
        raise ValueError("Expected finite calibration bin levels for seven tracks")
    q75 = np.percentile(levels, 75, axis=0)
    raw_thresholds = np.maximum(-55., q75 - 12.)
    active = levels >= raw_thresholds
    counts = active.sum(axis=0)
    if counts[0] < 50:
        raise ValueError("Calibration needs at least five seconds of lead energy")
    medians = np.array([np.median(levels[active[:, i], i]) if counts[i] >= 50 else q75[i]
                        for i in range(len(TRACKS))])
    gains = np.zeros(len(TRACKS))
    gains[0] = np.clip(-26. - medians[0], -12., 6.)
    anchor = medians[0] + gains[0]
    for i, name in enumerate(TRACKS[1:], 1):
        if counts[i] >= 50:
            gains[i] = np.clip(anchor + ROLE_OFFSETS_DB[name] - medians[i],
                               -18., 0. if name in ("YELLOW", "GREEN") else 6.)
    post = levels + gains
    thresholds = raw_thresholds + gains
    refs, enabled, joint_counts, eligibility = {}, [], {}, {}
    for i, name in enumerate(TRACKS[1:], 1):
        joint = active[:, 0] & active[:, i]
        joint_counts[name] = int(joint.sum())
        weak_backing = name in ("YELLOW", "GREEN") and medians[i] < -45.
        if joint.sum() >= 50 and not weak_backing:
            refs[name] = float(np.median(post[joint, i] - post[joint, 0]))
            enabled.append(name)
            eligibility[name] = "qualified_energy_reference_not_verified_source_intent"
        else:
            refs[name] = float(np.clip(medians[i] + gains[i] - anchor, -80, 40))
            thresholds[i] = 0.  # No qualified reference: conservatively disable.
            eligibility[name] = ("backing_reference_unverified_low_energy" if weak_backing
                                 else "insufficient_joint_activity")
    return {"baseline_gains_db": dict(zip(TRACKS, gains.tolist())),
            "reference_support_to_lead_db": refs,
            "activity_thresholds_dbfs": dict(zip(TRACKS, thresholds.tolist())),
            "enabled_support_tracks": enabled,
            "automatic_eligibility": eligibility,
            "raw_activity_thresholds_dbfs": dict(zip(TRACKS, raw_thresholds.tolist())),
            "active_bins": dict(zip(TRACKS, counts.tolist())),
            "joint_active_bins": joint_counts,
            "active_median_dbfs": dict(zip(TRACKS, medians.tolist())),
            "recipe": {"lead_anchor_dbfs": -26., "support_offsets_from_lead_db": ROLE_OFFSETS_DB,
                       "baseline_limits_db": [-18, 6], "backing_boost_allowed": False,
                       "provisional_backing_reference_minimum_dbfs": -45.,
                       "musical_preference_validated": False}}


def run(audit_path, output, *, source_date, calibration_start, calibration_seconds=30,
        evaluation_seconds=120, ffmpeg):
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new output directory for each immutable trial")
    if (not all(math.isfinite(v) for v in (calibration_start, calibration_seconds, evaluation_seconds))
            or calibration_start < 1 or not 5 <= calibration_seconds <= 60
            or not 10 <= evaluation_seconds <= 300
            or any(abs(v * 10 - round(v * 10)) > 1e-7
                   for v in (calibration_start, calibration_seconds, evaluation_seconds))):
        raise ValueError("Use bounded interior windows in complete 100ms bins")
    audit_path = Path(audit_path).resolve(strict=True)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    paths = validate_sources(audit, source_date)
    ffmpeg = Path(ffmpeg).resolve(strict=True)
    start = round(calibration_start * RATE)
    ncal = round(calibration_seconds * RATE)
    neval = round(evaluation_seconds * RATE)
    output.mkdir(parents=True)
    protocol = {"source_date": source_date, "tracks": list(TRACKS),
                "excluded": list(EXCLUDED_TRACKS),
                "other_tracks_omitted": ["AMBIENT", "LAPTOP", "return tracks"],
                "calibration_source_frames": [start, start + ncal],
                "evaluation_source_frames": [start + ncal, start + ncal + neval],
                "sample_rate": RATE, "role_offsets_db": ROLE_OFFSETS_DB,
                "automatic_correction_db": [-3, 0], "slew_db_per_second": .5,
                "evidence_seconds": 3, "drift_deadband_db": 2,
                "evidence_window_seconds": 5, "unsupported_reset_seconds": 1,
                "provisional_backing_reference_minimum_dbfs": -45.,
                "comparison": "dry unity vs static starting balance vs static plus coordinator",
                "current_Ableton_master_replica": False, "host_writes": False,
                "legacy_auto_and_master_glue_emulated": False,
                "source_audit_sha256": digest(audit_path),
                "implementation_sha256": digest(Path(__file__).with_name("mix_coordinator.py")),
                "runner_sha256": digest(__file__), "selection_is_development_data": True}
    save(output / "protocol.json", protocol)
    stats, hashes = {}, {name: hashlib.sha256() for name in TRACKS}
    cal_levels = []
    with ExitStack() as stack:
        readers = {}
        for name, path in paths.items():
            st = path.stat()
            f = stack.enter_context(sf.SoundFile(path, "r"))
            if f.samplerate != RATE or f.channels not in (1, 2) or f.frames <= start+ncal+neval:
                raise ValueError("Source format or duration changed")
            stats[name] = {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                           "channels": f.channels, "frames": f.frames,
                           "source_peak": 0., "source_full_scale_samples": 0}
            f.seek(start)
            readers[name] = f

        def read(frames):
            block = []
            for name in TRACKS:
                x = readers[name].read(frames, dtype="float32", always_2d=True)
                if len(x) != frames or not np.isfinite(x).all():
                    raise ValueError("Short/nonfinite source data")
                hashes[name].update(x.astype("<f4", copy=False).tobytes())
                stats[name]["source_peak"] = max(stats[name]["source_peak"], float(np.abs(x).max()))
                stats[name]["source_full_scale_samples"] += int((np.abs(x) >= 1-2**-23).sum())
                block.append(stereo(x))
            return np.stack(block, axis=1)

        for _ in range(ncal // (RATE//10)):
            x = read(RATE//10)
            cal_levels.append(10*np.log10(np.maximum(np.mean(x.astype(float)**2, axis=(0,2)), 1e-24)))
        plan = calibration(np.asarray(cal_levels))
        save(output / "calibration.json", plan)
        kwargs = {key: plan[key] for key in ("baseline_gains_db", "reference_support_to_lead_db",
                                           "activity_thresholds_dbfs")}
        kwargs["enabled_support_tracks"] = tuple(plan["enabled_support_tracks"])
        settings = CoordinatorSettings(**kwargs)
        protocol["coordinator_settings"] = settings.as_dict()
        save(output / "protocol.json", protocol)
        coordinator = MixCoordinator(settings, RATE)
        gains = 10**(np.array([plan["baseline_gains_db"][t] for t in TRACKS])/20)
        raw_paths = {key: output / f"{key}-raw.wav" for key in ("before", "static", "after")}
        writers = {key: stack.enter_context(sf.SoundFile(p, "w", samplerate=RATE, channels=2, subtype="FLOAT"))
                   for key, p in raw_paths.items()}
        peaks = {key: 0. for key in writers}
        correction_min = {t: 0. for t in TRACKS}
        correction_max = {t: 0. for t in TRACKS}
        envelope_rows = []
        max_slew = 0.
        previous_correction = np.zeros(len(TRACKS))
        for index in range(neval // (RATE//10)):
            x = read(RATE//10)
            processed = coordinator.process(x)
            variants = {"before": x.astype(float).sum(axis=1),
                        "static": (x * gains[None,:,None]).sum(axis=1),
                        "after": processed.sum(axis=1)}
            for key, y in variants.items():
                if not np.isfinite(y).all():
                    raise ValueError("Nonfinite render")
                writers[key].write(y)
                peaks[key] = max(peaks[key], float(np.abs(y).max()))
            corrections = coordinator.current_correction_db
            current = np.array([corrections[t] for t in TRACKS])
            max_slew = max(max_slew, float(np.max(np.abs(current-previous_correction))) / .1)
            previous_correction = current
            for t in TRACKS:
                correction_min[t] = min(correction_min[t], corrections[t])
                correction_max[t] = max(correction_max[t], corrections[t])
            envelope_rows.append({"end_seconds": (index+1)/10, "correction_db": corrections})
        save(output / "decisions.json", coordinator.events)
        save(output / "gain-envelopes.json", envelope_rows)

    for name, p in paths.items():
        st = p.stat()
        stats[name]["unchanged_size_and_mtime"] = (st.st_size == stats[name]["size"] and st.st_mtime_ns == stats[name]["mtime_ns"])
        stats[name]["decoded_window_sha256"] = hashes[name].hexdigest()
    common_gain = min(0., -3.-20*math.log10(max(max(peaks.values()), 1e-12)))
    auditions, raw_loudness = {}, {}
    for key, p in raw_paths.items():
        raw_loudness[key] = scan_loudness(p, ffmpeg)
    # One scalar per complete file; no compression, limiting, or time-varying master.
    target = min(-24., *(v["integrated_lufs"] - v["true_peak_dbtp"] - 3.5 for v in raw_loudness.values()))
    for key, p in raw_paths.items():
        match_gain = target - raw_loudness[key]["integrated_lufs"]
        for suffix, dbgain in (("common", common_gain), ("matched", match_gain)):
            dest = output / f"{key}-{suffix}.wav"
            with sf.SoundFile(p) as f, sf.SoundFile(dest, "w", samplerate=RATE, channels=2, subtype="PCM_24") as w:
                while True:
                    audio = f.read(RATE, dtype="float64", always_2d=True)
                    if not len(audio): break
                    w.write(audio * 10**(dbgain/20))
            measured = scan_loudness(dest, ffmpeg)
            auditions[f"{key}_{suffix}"] = {"path": str(dest.resolve()), "gain_db": dbgain,
                                           "sha256": digest(dest), **measured}
    checks = {"finite_output": True, "exact_evaluation_frames": all(sf.info(p).frames == neval for p in raw_paths.values()),
              "automatic_corrections_bounded": all(-3-1e-9 <= correction_min[t] <= correction_max[t] <= 1e-9 for t in TRACKS),
              "lead_unchanged_by_automatic_controller": correction_min["RED"] == correction_max["RED"] == 0,
              "slew_bound": max_slew <= .5+1e-8,
              "sources_unchanged_size_and_mtime": all(s["unchanged_size_and_mtime"] for s in stats.values()),
              "matched_loudness": max(auditions[k+"_matched"]["integrated_lufs"] for k in raw_paths)-min(auditions[k+"_matched"]["integrated_lufs"] for k in raw_paths) <= .2,
              "matched_true_peak_below_minus3": all(auditions[k+"_matched"]["true_peak_dbtp"] <= -3 for k in raw_paths)}
    report = {"protocol": protocol, "calibration": plan, "source_provenance": stats,
              "correction_min_db": correction_min, "correction_max_db": correction_max,
              "measured_max_bin_average_slew_db_per_second": max_slew,
              "unnormalized_sum_sample_peak_dbfs": {k:20*math.log10(max(v,1e-12)) for k,v in peaks.items()},
              "common_audition_gain_db": common_gain, "matched_target_lufs": target,
              "raw_loudness": raw_loudness, "auditions": auditions, "checks": checks,
              "engineering_checks_pass": all(checks.values()), "live_eligible": False,
              "perceptual_improvement_established": False,
              "limitation": "Energy is not semantic voice/bleed/intent detection. Static role balance is unaccepted. These files omit installed legacy auto, master Glue, returns and nonselected tracks."}
    save(output / "report.json", report)
    return report


def main():
    from ..commands.role_analysis import limit_resources
    limit_resources()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-date", required=True)
    parser.add_argument("--calibration-start", type=float, required=True)
    parser.add_argument("--calibration-seconds", type=float, default=30.)
    parser.add_argument("--evaluation-seconds", type=float, default=120.)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.audit, args.output, source_date=args.source_date,
                 calibration_start=args.calibration_start, calibration_seconds=args.calibration_seconds,
                 evaluation_seconds=args.evaluation_seconds, ffmpeg=args.ffmpeg)
    print(json.dumps({k:report[k] for k in ("engineering_checks_pass", "correction_min_db", "checks")}, indent=2))


if __name__ == "__main__":
    main()
