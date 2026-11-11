"""Bounded, auditable, file-only seven-source KIT fault audition renderer.

Development data only. This script reads exact frozen sources and immutable
virtual gain traces. It never connects to Ableton or changes the original WAVs.
It applies no limiter, compression, spectral processing, or host automation.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from tempfile import TemporaryDirectory
import hashlib
import json
import math
from pathlib import Path
import shutil

import numpy as np
import soundfile as sf

from .mix_coordinator import TRACKS
from .mix_coordinator_trial import validate_sources
from .kit_study import scan_loudness

RATE = 48_000
BIN = RATE // 10
FAULT_START, FAULT_END = 200, 500  # 20.0-50.0 seconds, 100 ms bins
FAULT_DB = 10.0
VARIANTS = ("static", "legacy-100ms", "candidate-500ms")
SEGMENTS = {
    "before_fault": (0, FAULT_START),
    "fault": (FAULT_START, FAULT_END),
    "recovery_early": (FAULT_END, 532),
    "recovery_hold": (532, 753),
    "recovery_late": (753, 820),
    "after_recovery": (820, 1200),
}
EPS = 1e-8


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _close(a, b, tolerance=1e-8):
    return math.isclose(float(a), float(b), rel_tol=0, abs_tol=tolerance)


def validate_trace(path: Path, *, expected_bins: int, maximum_cut_db: float,
                   slew_db_per_second: float, must_be_zero=False):
    """Validate causality, monotonically increasing bin identities, and ramp budget."""
    rows = []
    previous_end = 0.0
    previous_target = 0.0
    limit = slew_db_per_second / 10.0
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index >= expected_bins:
                raise ValueError(f"Too many gain rows: {path}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at gain row {index}") from exc
            if not isinstance(row, dict) or row.get("output_is_virtual_only") is not True:
                raise ValueError("Gain trace is not a virtual-only KIT trace")
            if type(row.get("bin_index")) is not int or row["bin_index"] != index:
                raise ValueError("Gain trace has missing, repeated, or reordered bins")
            if not _close(row.get("time_end_seconds"), (index + 1) / 10):
                raise ValueError("Gain trace time grid differs from audio grid")
            values = [row.get(k) for k in (
                "gain_db_at_start", "gain_db_at_end", "next_bin_target_db", "gain_delta_db_in_bin")]
            if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
                raise ValueError("Nonfinite or missing gain values")
            start, end, target, delta = map(float, values)
            if (not _close(start, previous_end) or not _close(end - start, delta)):
                raise ValueError("Gain trace is discontinuous")
            if not all(-maximum_cut_db - EPS <= v <= EPS for v in (start, end, target)):
                raise ValueError("Gain trace escaped [-max_cut, 0] dB")
            if abs(delta) > limit + EPS:
                raise ValueError("Gain trace violated slew limit")
            # This bin's gain must come from the preceding bin's target, never
            # from its own completed observation.
            step = max(-limit, min(limit, previous_target - previous_end))
            if not _close(end, previous_end + step):
                raise ValueError("Gain trace violates one-bin causality")
            if must_be_zero and any(abs(v) > EPS for v in (start, end, target)):
                raise ValueError("Saved 100 ms KIT controller must be zero for this experiment")
            rows.append(row)
            previous_end, previous_target = end, target
    if len(rows) != expected_bins:
        raise ValueError(f"Gain trace must contain exactly {expected_bins} bins")
    return rows


def gain_for_audio(row: dict, frames: int = BIN) -> np.ndarray:
    """Sample-accurate dB linear ramp between virtual start/end gains."""
    start = float(row["gain_db_at_start"])
    end = float(row["gain_db_at_end"])
    # The last sample is at the end gain; no current observation is used.
    db = start + (end - start) * (np.arange(1, frames + 1) / frames)
    return np.power(10.0, db / 20.0)


def preflight(report_path: Path, audit_path: Path, decisions_path: Path,
              gain_run: Path, ffmpeg: Path):
    """Fail before creating output when source identity or trace differs."""
    report = load_json(report_path)
    audit = load_json(audit_path)
    gain_summary = load_json(gain_run / "summary.json")
    protocol = report["protocol"]
    candidate_protocol = gain_summary["protocol"]
    if (report.get("engineering_checks_pass") is not True or
        report.get("live_eligible") is not False or
        report.get("perceptual_improvement_established") is not False):
        raise ValueError("Frozen dry coordinator trial is not a validated offline baseline")
    if (protocol.get("sample_rate") != RATE or
        tuple(protocol.get("tracks", ())) != TRACKS or
        tuple(protocol.get("excluded", ())) != ("LAPEL", "MDMIC Talkback", "BASS") or
        protocol.get("selection_is_development_data") is not True):
        raise ValueError("Unexpected recorded experiment scope")
    if (candidate_protocol.get("source_selection") != "previously_examined_development_recording" or
        candidate_protocol.get("sample_rate_hz") != RATE or
        candidate_protocol.get("observation_bin_ms") != 100 or
        candidate_protocol.get("candidate_measurement_window_ms") != 500 or
        candidate_protocol.get("fault_interval_seconds") != [20, 50]):
        raise ValueError("Expected frozen development-only 500 ms virtual-gain contract")
    if candidate_protocol.get("report_sha256") != hash_file(report_path):
        raise ValueError("Virtual-gain report digest mismatch")
    if candidate_protocol.get("decisions_sha256") != hash_file(decisions_path):
        raise ValueError("Virtual-gain decisions digest mismatch")
    if candidate_protocol.get("saved_100ms_trace_replay_verified_bins") != 1200:
        raise ValueError("Missing frozen 100 ms trace replay qualification")
    if gain_summary.get("engineering_controls_pass") is not True:
        raise ValueError("Virtual-gain engineering controls did not pass")
    if (gain_summary.get("audio_rendered") is not False or
        gain_summary.get("gain_policy_applied_to_host") is not False):
        raise ValueError("Virtual gain provenance has unexpected live/audio effect")
    checks = gain_summary["engineering_controls"]
    if not all(checks.get(k) is True for k in (
        "100ms_replay_zero_gain", "500ms_original_no_gain",
        "500ms_sustained_fault_attenuated", "500ms_isolated_hit_no_gain",
        "500ms_red_pause_no_gain", "500ms_gap_no_gain")):
        raise ValueError("Virtual-gain scenario checks incomplete")
    if protocol.get("source_audit_sha256") != hash_file(audit_path):
        raise ValueError("Frozen source-audit digest mismatch")
    start, cal_end = protocol["calibration_source_frames"]
    eval_start, eval_end = protocol["evaluation_source_frames"]
    if (not (type(start) is type(cal_end) is type(eval_start) is type(eval_end) is int) or
        start < 0 or cal_end != eval_start or (eval_end - eval_start) != 1200 * BIN or
        (cal_end - start) != 300 * BIN):
        raise ValueError("Unexpected fixed calibration/evaluation frame windows")
    source_paths = validate_sources(audit, protocol["source_date"])
    source_info = report["source_provenance"]
    if set(source_info) != set(TRACKS):
        raise ValueError("Report provenance does not enumerate seven sources")
    for name in TRACKS:
        if source_paths[name] != Path(source_info[name]["path"]).resolve(strict=True):
            raise ValueError(f"Report and source manifest disagree for {name}")
    for name in ("RED", "KIT"):
        verified = candidate_protocol["source_verification"].get(name)
        if (not isinstance(verified, dict) or
            verified.get("verified_against_saved_trial") is not True or
            verified.get("decoded_window_sha256") != source_info[name]["decoded_window_sha256"]):
            raise ValueError(f"Virtual-gain {name} source binding mismatch")
    settings = protocol["coordinator_settings"]
    if (settings.get("enabled_support_tracks") != ["EGUITAR", "AC GUITAR", "KEYS", "KIT"] or
        settings["max_cut_db"] != 3.0 or settings["slew_db_per_second"] != 0.5):
        raise ValueError("Unexpected frozen controller configuration")
    if candidate_protocol["settings"] != settings:
        raise ValueError("Virtual gain settings differ from offline trial")
    if not ffmpeg.exists() or not ffmpeg.is_file():
        raise ValueError(f"FFmpeg executable not found: {ffmpeg}")
    # Check the exact PCM window of ALL seven stems, not only RED and KIT.
    source_hashes = {}
    file_before = {}
    for name in TRACKS:
        path = source_paths[name]
        stat = path.stat()
        file_before[name] = (stat.st_size, stat.st_mtime_ns)
        source_hash = hashlib.sha256()
        with sf.SoundFile(path, "r") as reader:
            if (reader.samplerate != RATE or reader.channels not in (1, 2)
                or reader.frames < eval_end):
                raise ValueError(f"Source shape/rate/length mismatch: {name}")
            reader.seek(start)
            remain = eval_end - start
            while remain:
                count = min(remain, RATE * 2)
                audio = reader.read(count, dtype="float32", always_2d=True)
                if len(audio) != count or not np.isfinite(audio).all():
                    raise ValueError(f"Nonfinite or truncated source: {name}")
                source_hash.update(audio.astype("<f4", copy=False).tobytes())
                remain -= count
        value = source_hash.hexdigest()
        if value != source_info[name]["decoded_window_sha256"]:
            raise ValueError(f"Saved decoded-window hash mismatch for {name}")
        source_hashes[name] = value
    legacy = validate_trace(
        gain_run / "100ms-kit_step_plus10-gain.jsonl", expected_bins=1200,
        maximum_cut_db=3.0, slew_db_per_second=0.5, must_be_zero=True)
    candidate = validate_trace(
        gain_run / "500ms-kit_step_plus10-gain.jsonl", expected_bins=1200,
        maximum_cut_db=3.0, slew_db_per_second=0.5)
    observed_min = min(float(row["gain_db_at_end"]) for row in candidate)
    expected = gain_summary["comparisons"]["500ms"]["kit_step_plus10"]
    if not _close(observed_min, expected["minimum_applied_gain_db"]):
        raise ValueError("500 ms trace differs from summarized gain envelope")
    if not _close(candidate[-1]["gain_db_at_end"], expected["final_gain_db"]):
        raise ValueError("500 ms trace final gain differs from summary")
    if sum(float(row["gain_db_at_end"]) < -EPS for row in candidate) != expected["nonzero_gain_bins"]:
        raise ValueError("500 ms trace nonzero correction count differs from summary")
    if sum(row.get("action_after_observation") == "attenuate" for row in candidate) != expected["action_counts"]["attenuate"]:
        raise ValueError("500 ms trace attenuate action count differs from summary")
    if abs(observed_min) < 0.05:
        raise ValueError("Candidate trace contains no meaningful controlled attenuation")
    return report, gain_summary, source_paths, source_hashes, file_before, legacy, candidate


def _scan(path, ffmpeg):
    metrics = scan_loudness(path, ffmpeg)
    if not (math.isfinite(metrics["integrated_lufs"]) and
            math.isfinite(metrics["true_peak_dbtp"])):
        raise ValueError("Nonfinite FFmpeg R128 measurement")
    return metrics


def _stream_scale(source: Path, destination: Path, gain_db: float, frames: int):
    factor = 10 ** (gain_db / 20)
    with sf.SoundFile(source, "r") as inp, sf.SoundFile(
            destination, "x", samplerate=RATE, channels=2, subtype="PCM_24") as out:
        if inp.frames != frames or inp.channels != 2 or inp.samplerate != RATE:
            raise ValueError("Unexpected dry-sum render format")
        while True:
            data = inp.read(RATE, dtype="float64", always_2d=True)
            if not len(data):
                break
            out.write(data * factor)


def render(report_path: Path, audit_path: Path, decisions_path: Path,
           gain_run: Path, output: Path, ffmpeg: Path) -> dict:
    if output.exists():
        raise ValueError("Output directory exists; choose a new immutable experiment directory")
    report, gain_summary, paths, hashes, original_stats, legacy, candidate = preflight(
        report_path, audit_path, decisions_path, gain_run, ffmpeg)
    start, end = report["protocol"]["evaluation_source_frames"]
    frames_total = end - start
    gains = np.asarray([report["protocol"]["coordinator_settings"]["baseline_gains_db"][t]
                        for t in TRACKS], dtype=np.float64)
    baseline_linear = np.power(10.0, gains / 20.0)
    segments = {v: {name: {"frames": 0, "sum_sq": 0.0, "abs_peak": 0.0}
                    for name in SEGMENTS} for v in VARIANTS}
    output.mkdir(parents=True, exist_ok=False)
    raw_metrics, auditions, measured, segment_loudness = {}, {}, {}, {}
    delta_max = 0.0
    with TemporaryDirectory(prefix="raw-", dir=output) as temporary:
        root = Path(temporary)
        raw = {name: root / f"{name}.wav" for name in VARIANTS}
        with ExitStack() as stack:
            readers = {name: stack.enter_context(sf.SoundFile(path)) for name, path in paths.items()}
            for reader in readers.values():
                reader.seek(start)
            writers = {v: stack.enter_context(sf.SoundFile(
                raw[v], "x", samplerate=RATE, channels=2, subtype="FLOAT"))
                for v in VARIANTS}
            for i in range(1200):
                data = []
                for name in TRACKS:
                    block = readers[name].read(BIN, dtype="float32", always_2d=True)
                    if len(block) != BIN or not np.isfinite(block).all():
                        raise ValueError(f"Invalid source frame in {name} at bin {i}")
                    if block.shape[1] == 1:
                        block = np.repeat(block, 2, axis=1)
                    data.append(block.astype(np.float64))
                x = np.stack(data, axis=1)
                x *= baseline_linear[None, :, None]
                if FAULT_START <= i < FAULT_END:
                    x[:, -1, :] *= 10 ** (FAULT_DB / 20.0)
                common = x.sum(axis=1)
                legacy_sum = common.copy()  # This frozen trace is proven zero.
                factor = gain_for_audio(candidate[i], frames=BIN)
                candidate_sum = common + x[:, -1, :] * (factor[:, None] - 1.0)
                # Independent recomposition of the exact seven-track candidate.
                reconstructed = x.copy()
                reconstructed[:, -1, :] *= factor[:, None]
                delta_max = max(delta_max, float(np.max(np.abs(candidate_sum - reconstructed.sum(axis=1)))))
                for name, block in (("static", common), ("legacy-100ms", legacy_sum),
                                    ("candidate-500ms", candidate_sum)):
                    if not np.isfinite(block).all():
                        raise ValueError("Nonfinite mix produced")
                    writers[name].write(block)
                    for seg, (lo, hi) in SEGMENTS.items():
                        if lo <= i < hi:
                            stat = segments[name][seg]
                            stat["frames"] += BIN
                            stat["sum_sq"] += float(np.square(block).sum())
                            stat["abs_peak"] = max(stat["abs_peak"], float(np.abs(block).max()))
                            break
        for variant in VARIANTS:
            raw_metrics[variant] = _scan(raw[variant], ffmpeg)
        if hash_file(raw["static"]) != hash_file(raw["legacy-100ms"]):
            raise ValueError("Legacy 100 ms render differs from its frozen static baseline")
        # One target for all versions. Headroom is measured, not inferred from
        # sample peaks or assumed to follow from attenuating one source.
        target_lufs = min(-24.0, *(m["integrated_lufs"] - m["true_peak_dbtp"] - 3.5
                                     for m in raw_metrics.values()))
        for variant in VARIANTS:
            gain_db = target_lufs - raw_metrics[variant]["integrated_lufs"]
            if not math.isfinite(gain_db) or abs(gain_db) > 36:
                raise ValueError("Audition gain outside bounded scalar range")
            filename = f"audition-{variant}.wav"
            dest = output / filename
            _stream_scale(raw[variant], dest, gain_db, frames_total)
            metrics = _scan(dest, ffmpeg)
            if metrics["true_peak_dbtp"] > -3.0 + 1e-6:
                raise ValueError(f"Audition exceeded -3 dBTP true-peak ceiling: {variant}")
            if abs(metrics["integrated_lufs"] - target_lufs) > .2:
                raise ValueError(f"Audition LUFS target mismatch: {variant}")
            measured[variant] = metrics
            auditions[variant] = {"file": filename, "gain_db": gain_db,
                                   "sha256": hash_file(dest)}
            # Compute actual EBU R128 window measurements on the final safe
            # file, not merely RMS of its unnormalized dry-sum predecessor.
            segment_loudness[variant] = {}
            with sf.SoundFile(dest, "r") as audition:
                for segment in ("fault", "recovery_hold", "recovery_late", "after_recovery"):
                    low, high = SEGMENTS[segment]
                    audition.seek(low * BIN)
                    segment_file = root / f"segment-{variant}-{segment}.wav"
                    with sf.SoundFile(segment_file, "x", samplerate=RATE,
                                      channels=2, subtype="FLOAT") as window_writer:
                        remaining = (high - low) * BIN
                        while remaining:
                            block = audition.read(min(remaining, RATE), dtype="float32",
                                                  always_2d=True)
                            if not len(block):
                                raise ValueError("Unexpected short matched-audition segment")
                            window_writer.write(block)
                            remaining -= len(block)
                    try:
                        segment_loudness[variant][segment] = {"status": "measured", **_scan(segment_file, ffmpeg)}
                    except (ValueError, RuntimeError) as exc:
                        # A silent or heavily gated segment has no reliable
                        # integrated LUFS; don't invent a value.
                        segment_loudness[variant][segment] = {
                            "status": "unavailable", "reason": type(exc).__name__}
                    segment_file.unlink()
    if delta_max > 1e-10:
        raise ValueError("Candidate recomposition null test failed")
    for name, path in paths.items():
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != original_stats[name]:
            raise ValueError(f"Input source changed during rendering: {name}")
    for variant in VARIANTS:
        for seg, stat in segments[variant].items():
            frames = stat.pop("frames")
            stat["rms_dbfs"] = 20 * math.log10(max(
                math.sqrt(stat.pop("sum_sq") / max(2 * frames, 1)), 1e-12))
            stat["sample_peak_dbfs"] = 20 * math.log10(max(stat.pop("abs_peak"), 1e-12))
            stat["seconds"] = frames / RATE
    summary = {
        "protocol": {"kind": "offline_kit_fault_mix_auditions_v1",
                     "source_selection": "previously_examined_development_recording",
                     "sample_rate": RATE, "channels": 2, "frames": frames_total,
                     "allowed_tracks": list(TRACKS), "omitted_chains": ["returns", "master Glue",
                        "installed legacy auto", "nonselected tracks", "other host effects"],
                     "fault": {"track": "KIT", "gain_db": FAULT_DB,
                               "start_seconds": 20, "end_seconds": 50},
                     "gain_only": True, "same_initial_static_balance": True,
                     "limiter_or_compressor": False, "host_writes": 0,
                     "live_eligible": False, "perceptual_improvement_established": False},
        "inputs": {"trial_report_sha256": hash_file(report_path),
                   "source_audit_sha256": hash_file(audit_path),
                   "decisions_sha256": hash_file(decisions_path),
                   "virtual_gain_summary_sha256": hash_file(gain_run / "summary.json"),
                   "baseline_trace_sha256": hash_file(gain_run / "100ms-kit_step_plus10-gain.jsonl"),
                   "candidate_trace_sha256": hash_file(gain_run / "500ms-kit_step_plus10-gain.jsonl"),
                   "seven_verified_source_windows": hashes,
                   "recorded_source_full_scale_sample_counts": {
                       name: report["source_provenance"][name].get("source_full_scale_samples")
                       for name in TRACKS}},
        "render_checks": {"originals_unchanged": True,
                          "legacy_and_static_dry_sum_identical": True,
                          "all_auditions_below_minus3_dbtp": True,
                          "loudness_matched_within_0_2_lu": True,
                          "candidate_recomposition_max_abs_error": delta_max},
        "loudness_target_lufs": target_lufs,
        "raw_full_mix_measurements": raw_metrics,
        "audition_full_mix_measurements": measured,
        "audition_segment_r128": segment_loudness,
        "auditions": auditions,
        "dry_sum_segment_measurements": segments,
        "limitations": [
            "Previously inspected development recording, not held-out material.",
            "Static gains are provisional and are not an accepted musical mix.",
            "Dry seven-stem sum, NOT an exact replica of the Ableton Live processing chain.",
            "Candidate uses existing virtual envelope with a constructed +10 dB KIT fault.",
            "Original KIT recording may already contain clipped/full-scale samples; inspect source provenance.",
            "Loudness matching uses scalar gains only; no compression or limiting.",
            "No blind listening evaluation or subjective quality evidence yet.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--gain-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = render(args.report, args.audit, args.decisions, args.gain_run,
                        args.output, args.ffmpeg)
        print(json.dumps({"output": str(args.output), "auditions": result["auditions"],
                          "loudness_target_lufs": result["loudness_target_lufs"],
                          "render_checks": result["render_checks"]}, indent=2))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"KIT offline render refused: {exc}\n")


if __name__ == "__main__":
    main()
