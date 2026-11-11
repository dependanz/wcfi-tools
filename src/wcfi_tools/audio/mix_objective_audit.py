"""Offline first-principles mixing diagnostics from seven exact recorded stems.

No host/DAW access, playback, neural inference, gain automation or audio export.
The returned spectral energy ratios are NOT validated psychoacoustic masking,
speech-intelligibility, singer-identity, or perceptual quality measurements.
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

TRACKS = ("RED", "YELLOW", "GREEN", "EGUITAR", "AC GUITAR", "KEYS", "KIT")
INDEX = {name: i for i, name in enumerate(TRACKS)}
BANDS_HZ = {
    "kick_40_180": (40, 180),
    "body_180_800": (180, 800),
    "mid_800_2500": (800, 2500),
    "presence_2500_7000": (2500, 7000),
    "speech_300_4500": (300, 4500),
    "drum_80_7000": (80, 7000),
}
EPS = 1e-20


def _db_power(p):
    return 10.0 * np.log10(np.maximum(np.asarray(p, dtype=np.float64), EPS))


def _dB_ratio(a, b):
    # Floor is for numerical stability, NOT an audibility threshold.
    return _db_power(a) - _db_power(b)


def _summary(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return {"status": "unavailable", "count": 0,
                "p10_db": None, "median_db": None, "p90_db": None}
    if not np.all(np.isfinite(v)):
        raise ValueError("Nonfinite objective values")
    return {"status": "measured", "count": int(v.size),
            "p10_db": float(np.percentile(v, 10)),
            "median_db": float(np.median(v)),
            "p90_db": float(np.percentile(v, 90))}


def _source_windows(report):
    proto = report["protocol"]
    if tuple(proto["tracks"]) != TRACKS or proto.get("source_date") != "2026-10-04":
        raise ValueError("Only the frozen October 4 seven-source study is accepted")
    rate = proto["sample_rate"]
    if rate != 48000:
        raise ValueError("Expected the verified 48000 Hz recording")
    cal_start, cal_end = proto["calibration_source_frames"]
    ev_start, ev_end = proto["evaluation_source_frames"]
    if cal_start < 0 or cal_end != ev_start or (ev_end - ev_start) != rate * 120:
        raise ValueError("The frozen calibration/evaluation windows differ")
    if any(v % (rate // 10) for v in (cal_start, cal_end, ev_end)):
        raise ValueError("Unaligned 100 ms source window")
    sources = report["source_provenance"]
    if set(sources) != set(TRACKS):
        raise ValueError("Source provenance must name exactly seven permitted stems")
    return rate, cal_start, cal_end, ev_end, sources


def _check_source_files(report):
    """Preflight ALL stems (hashing exactly calibration+evaluation) before output."""
    rate, cal_start, cal_end, ev_end, sources = _source_windows(report)
    details = {}
    for track in TRACKS:
        metadata = sources[track]
        path = Path(metadata["path"])
        st = path.stat()
        if st.st_size != metadata["size"] or st.st_mtime_ns != metadata["mtime_ns"]:
            raise ValueError(f"Source changed size or mtime: {track}")
        sha = hashlib.sha256()
        with sf.SoundFile(path, "r") as reader:
            if (reader.samplerate != rate or reader.channels != metadata["channels"]
                    or reader.frames != metadata["frames"] or reader.channels not in (1, 2)
                    or reader.frames < ev_end):
                raise ValueError(f"Source format or frame count changed: {track}")
            reader.seek(cal_start)
            remaining = ev_end - cal_start
            while remaining:
                data = reader.read(min(rate, remaining), dtype="float32", always_2d=True)
                if len(data) == 0 or not np.all(np.isfinite(data)):
                    raise ValueError(f"Invalid or shortened source: {track}")
                sha.update(data.astype("<f4", copy=False).tobytes())
                remaining -= len(data)
        got = sha.hexdigest()
        if got != metadata["decoded_window_sha256"]:
            raise ValueError(f"Decoded source hash mismatch: {track}")
        details[track] = {"verified_sha256": got, "channels": metadata["channels"],
                          "calibration_and_evaluation_checked": True,
                          "reported_full_scale_samples": metadata.get("source_full_scale_samples")}
    return details


def _band_powers(block, hann, masks, denominator):
    """Parseval-normalized, windowed mean powers per frequency band.

    For mono or stereo inputs, returns the mean of channel powers. This is an
    energy-accounting approximation, not an auditory excitation/masking model.
    """
    n = len(block)
    x = np.asarray(block, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] not in (1, 2) or n != len(hann):
        raise ValueError("Expected a full 100 ms mono/stereo source bin")
    X = np.fft.rfft(x * hann[:, None], axis=0)
    p = np.mean(np.abs(X) ** 2, axis=1)
    p[1:-1] *= 2.0
    p /= denominator
    return np.array([float(np.sum(p[mask])) for mask in masks], dtype=np.float64)


def _variants(baseline):
    base = np.asarray([baseline[t] for t in TRACKS], dtype=np.float64)
    if np.any(~np.isfinite(base)):
        raise ValueError("Nonfinite baseline")
    proposals = {
        "frozen_static": base.copy(),
        "lead_minus3db_probe": base.copy(),
        "kit_plus3db_probe": base.copy(),
        "both_probe": base.copy(),
        "unity_dry_probe": np.zeros(len(TRACKS), dtype=np.float64),
    }
    proposals["lead_minus3db_probe"][INDEX["RED"]] -= 3
    proposals["kit_plus3db_probe"][INDEX["KIT"]] += 3
    proposals["both_probe"][INDEX["RED"]] -= 3
    proposals["both_probe"][INDEX["KIT"]] += 3
    return proposals


def _capture_features(readers, rate, evaluation_frames, variants):
    length = rate // 10
    n_bins = evaluation_frames // length
    window = np.hanning(length)
    freq = np.fft.rfftfreq(length, d=1 / rate)
    masks = [(freq >= lo) & (freq < hi) for lo, hi in BANDS_HZ.values()]
    if not all(mask.any() for mask in masks):
        raise ValueError("Frequency grid has an empty requested band")
    denominator = length * float(np.sum(window ** 2))
    features = np.zeros((n_bins, len(TRACKS), len(BANDS_HZ)), dtype=np.float64)
    powers = np.zeros((n_bins, len(TRACKS)), dtype=np.float64)
    stems_peak = np.zeros(len(TRACKS), dtype=np.float64)
    mix_peak = {name: 0.0 for name in variants}
    mix_power_sum = {name: 0.0 for name in variants}
    multipliers = {name: 10 ** (gains / 20) for name, gains in variants.items()}
    for bin_index in range(n_bins):
        blocks = []
        for i, name in enumerate(TRACKS):
            native = readers[name].read(length, dtype="float32", always_2d=True)
            if len(native) != length or not np.all(np.isfinite(native)):
                raise ValueError(f"Invalid input bin {bin_index} for {name}")
            features[bin_index, i, :] = _band_powers(native, window, masks, denominator)
            powers[bin_index, i] = float(np.mean(np.asarray(native, dtype=np.float64) ** 2))
            stems_peak[i] = max(stems_peak[i], float(np.max(np.abs(native))))
            blocks.append(np.repeat(native, 2, axis=1) if native.shape[1] == 1 else native)
        stack = np.stack(blocks, axis=1).astype(np.float64, copy=False)
        for name, multiplier in multipliers.items():
            mix = np.sum(stack * multiplier[None, :, None], axis=1)
            mix_peak[name] = max(mix_peak[name], float(np.max(np.abs(mix))))
            mix_power_sum[name] += float(np.sum(mix * mix))
    return features, powers, stems_peak, mix_peak, mix_power_sum


def _select_onsets(raw_band_powers, raw_rms, baseline_kit_db, kit_threshold_dbfs):
    """Select strongest *candidate* drum attacks using source-only spectral flux.

    A top decile is a descriptive selection rule, NOT a beat transcription,
    musical-intent detector, or psychoacoustic audibility threshold.
    """
    spec = np.sqrt(np.maximum(raw_band_powers[:, [0, 1, 2, 3]], 0.0))
    flux = np.zeros(len(spec), dtype=np.float64)
    flux[1:] = np.sum(np.maximum(spec[1:] - spec[:-1], 0.0), axis=1)
    active = _db_power(raw_rms) + baseline_kit_db >= kit_threshold_dbfs
    eligible = np.flatnonzero(active & (flux > 0))
    if len(eligible) == 0:
        return np.array([], dtype=np.int64), flux, int(np.sum(active))
    count = max(1, int(math.ceil(len(eligible) * 0.10)))
    sorted_idx = eligible[np.argsort(-flux[eligible], kind="stable")[:count]]
    return np.sort(sorted_idx), flux, int(np.sum(active))


def evaluate_objectives(features, powers, variants, activity_thresholds,
                        *, mix_peak=None, mix_power_sum=None, frames_per_bin=4800):
    """Evaluate descriptive physical proxies; never nominate an automatic mix."""
    f = np.asarray(features, dtype=np.float64)
    r = np.asarray(powers, dtype=np.float64)
    if f.ndim != 3 or f.shape[1:] != (len(TRACKS), len(BANDS_HZ)) or r.shape != f.shape[:2]:
        raise ValueError("Incompatible energy feature arrays")
    if len(f) == 0 or not np.isfinite(f).all() or not np.isfinite(r).all() or (f < 0).any() or (r < 0).any():
        raise ValueError("Features must contain nonnegative finite powers")
    if set(activity_thresholds) != set(TRACKS):
        raise ValueError("Activity thresholds must cover exactly seven stems")
    static = variants["frozen_static"]
    n = len(f)
    red_active = _db_power(r[:, INDEX["RED"]]) + static[INDEX["RED"]] >= activity_thresholds["RED"]
    onset_indices, flux, kit_active = _select_onsets(
        f[:, INDEX["KIT"]], r[:, INDEX["KIT"]],
        static[INDEX["KIT"]], activity_thresholds["KIT"])
    spec_names = list(BANDS_HZ)
    speech_idx, drum_idx = spec_names.index("speech_300_4500"), spec_names.index("drum_80_7000")
    name_to_idx = INDEX
    result = {}
    for name, gain in variants.items():
        gains = np.asarray(gain, dtype=np.float64)
        if gains.shape != (len(TRACKS),) or not np.isfinite(gains).all():
            raise ValueError("Invalid probe gain")
        scale = 10 ** (gains / 10)
        if not np.isfinite(scale).all():
            raise ValueError("Overflowing probe gain")
        speech = f[:, :, speech_idx] * scale[None, :]
        all_others = np.sum(speech, axis=1) - speech[:, INDEX["RED"]]
        instr = np.sum(speech[:, [INDEX[t] for t in ("EGUITAR", "AC GUITAR", "KEYS", "KIT")]], axis=1)
        red = speech[:, INDEX["RED"]]
        lead_margin = _dB_ratio(red, all_others)
        lead_inst_margin = _dB_ratio(red, instr)
        backing = speech[:, INDEX["YELLOW"]] + speech[:, INDEX["GREEN"]]
        backing_margin = _dB_ratio(backing, red)
        drum = f[:, :, drum_idx] * scale[None, :]
        kit = drum[:, INDEX["KIT"]]
        accompaniment = np.sum(drum, axis=1) - kit
        drum_margin = _dB_ratio(kit, accompaniment)
        segment_names = {
            "before_0_20s": (0, min(200, n)),
            "main_20_50s": (min(200, n), min(500, n)),
            "bridge_approx_50_82s": (min(500, n), min(820, n)),
            "later_82_120s": (min(820, n), n),
        }
        segments = {}
        for label, (start, stop) in segment_names.items():
            valid_red = np.flatnonzero(red_active[start:stop]) + start
            ev = onset_indices[(onset_indices >= start) & (onset_indices < stop)]
            segments[label] = {
                "bins": int(stop - start),
                "red_activity_bins": int(len(valid_red)),
                "selected_kit_attack_candidates": int(len(ev)),
                "lead_vs_all_stems_speech_band_db": _summary(lead_margin[valid_red]),
                "lead_vs_instruments_speech_band_db": _summary(lead_inst_margin[valid_red]),
                "backing_vs_red_speech_band_db_unverified_identity": _summary(backing_margin[valid_red]),
                "kit_vs_all_other_stems_drum_band_at_candidate_attacks_db": _summary(drum_margin[ev]),
            }
        peak = None if mix_peak is None else float(mix_peak[name])
        total_mix_power = None if mix_power_sum is None else float(mix_power_sum[name])
        peak_db = None if peak is None else float(20 * np.log10(max(peak, 1e-10)))
        mix_rms = None if total_mix_power is None else float(_db_power(total_mix_power / (n * frames_per_bin * 2)))
        result[name] = {
            "gains_db": dict(zip(TRACKS, gains.tolist())),
            "source_power_sum_is_incoherent_approximation": True,
            "dry_stereo_sample_peak_dbfs_exact": peak_db,
            "dry_stereo_rms_dbfs_exact": mix_rms,
            "sample_peak_only_common_gain_for_minus3dbfs": (
                None if peak_db is None else min(0.0, -3.0 - peak_db)),
            "global": {
                "red_activity_bins": int(np.sum(red_active)),
                "kit_active_bins": kit_active,
                "selected_kit_attack_candidates": int(len(onset_indices)),
                "lead_vs_all_stems_speech_band_db": _summary(lead_margin[red_active]),
                "lead_vs_instruments_speech_band_db": _summary(lead_inst_margin[red_active]),
                "backing_vs_red_speech_band_db_unverified_identity": _summary(backing_margin[red_active]),
                "kit_vs_all_other_stems_drum_band_at_candidate_attacks_db": _summary(drum_margin[onset_indices]),
            },
            "segments": segments,
        }
    return result, {"red_active_bins": int(np.sum(red_active)), "kit_active_bins": kit_active,
                    "kit_attack_candidates": int(len(onset_indices)),
                    "kit_attack_selection": "strongest 10 percent positive source-only 100ms spectral-flux bins, among KIT-active bins",
                    "selected_attack_bin_indices": onset_indices.tolist(),
                    "no_inferred_beat_or_instrument_identity": True}


def run(report_path: Path, output: Path, audit_path: Path | None = None):
    if output.exists():
        raise ValueError("Output already exists; require a new immutable experiment directory")
    report_path = report_path.resolve(strict=True)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if audit_path is not None:
        audit_path = audit_path.resolve(strict=True)
        if hashlib.sha256(audit_path.read_bytes()).hexdigest() != report["protocol"]["source_audit_sha256"]:
            raise ValueError("Source audit hash mismatch")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if (len(audit["sources"]) != len(TRACKS)
                or {item["track"] for item in audit["sources"]} != set(TRACKS)):
            raise ValueError("Source audit contains unexpected/missing tracks")
        reported = report["source_provenance"]
        for item in audit["sources"]:
            # The saved manifest is the authority: no discovery of takes,
            # alternate dates, January source files, or excluded tracks.
            if Path(item["source_path"]).resolve() != Path(reported[item["track"]]["path"]).resolve():
                raise ValueError(f"Audit/report source differs for {item['track']}")
    verified = _check_source_files(report)
    rate, _, _, end, sources = _source_windows(report)
    start = report["protocol"]["evaluation_source_frames"][0]
    variants = _variants(report["protocol"]["coordinator_settings"]["baseline_gains_db"])
    with ExitStack() as stack:
        readers = {}
        for name in TRACKS:
            reader = stack.enter_context(sf.SoundFile(sources[name]["path"], mode="r"))
            reader.seek(start)
            readers[name] = reader
        features, rms, stems_peak, mix_peak, mix_power_sum = _capture_features(readers, rate, end - start, variants)
    # Source state rechecked before any output directory creation.
    for name, meta in sources.items():
        st = Path(meta["path"]).stat()
        if st.st_size != meta["size"] or st.st_mtime_ns != meta["mtime_ns"]:
            raise ValueError(f"Source changed during analysis: {name}")
    metrics, selection = evaluate_objectives(
        features, rms, variants,
        report["protocol"]["coordinator_settings"]["activity_thresholds_dbfs"],
        mix_peak=mix_peak, mix_power_sum=mix_power_sum, frames_per_bin=rate // 10)
    summary = {
        "protocol": {
            "kind": "seven_stem_offline_physical_proxy_audit_v1", "sample_rate": rate,
            "analysis_window_seconds": 120, "bin_duration_seconds": .1,
            "tracks": list(TRACKS), "bands_hz": BANDS_HZ,
            "no_fault_injection": True,
            "source_date": "2026-10-04", "source_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "audio_host_or_live_access": False, "host_writes": 0, "audio_files_written": 0,
            "input_files_modified": False, "mix_recommendation_made": False,
            "perceptual_quality_established": False,
            "sample_peak_is_not_true_peak": True,
            "vocal_or_drum_intelligibility_established": False,
        },
        "source_verification": verified,
        "recorded_source_full_scale_counts": {t: sources[t].get("source_full_scale_samples") for t in TRACKS},
        "selection": selection,
        "probe_results": metrics,
        "limitations": [
            "Spectral source-power ratios approximate overlap; they are not human auditory masking or subjective clarity measurements.",
            "Source-only spectral-flux events are candidates, not annotated kicks or snares.",
            "Backing-stem energy does not establish presence of singing rather than bleed/noise.",
            "Raw and calibrated stems are previously examined development material, not an independent holdout.",
            "All static gain perturbations are diagnostic probes, not listening-validated prescriptions.",
            "Dry stereo sample peak is exact for the reconstructed samples but DOES NOT certify intersample true peak, loudness or the Ableton master graph.",
            "Selection of objective weights and musical genre policy remains scientifically underdetermined by physical signals alone.",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--audit", type=Path)
    args = p.parse_args()
    try:
        result = run(args.report, args.output, args.audit)
        short = {
            "output": str(args.output.resolve()),
            "protocol": result["protocol"]["kind"],
            "source_fingerprints_verified": len(result["source_verification"]),
            "selection": {k:v for k,v in result["selection"].items() if k != "selected_attack_bin_indices"},
            "probe_global": {k: v["global"] for k,v in result["probe_results"].items()},
        }
        print(json.dumps(short, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        p.exit(1, f"Objective mix audit stopped: {exc}\n")


if __name__ == "__main__":
    main()
