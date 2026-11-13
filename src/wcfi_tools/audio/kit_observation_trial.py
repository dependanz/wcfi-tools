"""Offline KIT/RED measurement study, not a new gain controller.

Compare 100 ms observations with a causal trailing 500 ms POWER average.
Only exact RED/KIT source paths and frame ranges from an existing trial are read.
No playback, host connection, network, waveform export, or gain application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .coordinator_diagnostics import BALANCE_REASONS, read_json, sha256
from .mix_coordinator import CoordinatorSettings

PAIR = ("RED", "KIT")
SCENARIOS = ("original", "kit_step_plus10", "kit_single_hit_plus10", "lead_pause", "gap")


def db(power):
    return 10 * np.log10(np.maximum(power, 1e-16))


@dataclass(frozen=True)
class PairReference:
    window_bins: int
    baseline_db: tuple[float, float]
    thresholds_dbfs: tuple[float, float]
    reference_db: float
    raw_lead_threshold_dbfs: float
    calibration_joint_bins: int | None = None


class PowerWindow:
    def __init__(self, bins: int):
        if type(bins) is not int or bins not in (1, 5):
            raise ValueError("Only the frozen 100 ms and 500 ms windows are supported")
        self.bins = bins
        self.values = deque(maxlen=bins)

    def clear(self):
        self.values.clear()

    def push(self, powers):
        powers = np.asarray(powers, dtype=np.float64)
        if powers.shape != (2,) or not np.isfinite(powers).all() or (powers < 0).any():
            self.clear()
            raise ValueError("Expected two finite nonnegative raw powers")
        self.values.append(powers.copy())
        if len(self.values) < self.bins:
            return None
        return np.mean(np.asarray(self.values), axis=0)


def calibrate_500ms(powers, baseline: PairReference) -> PairReference:
    """Fit the new measurement's thresholds/reference on earlier calibration only."""
    powers = np.asarray(powers, dtype=np.float64)
    if powers.ndim != 2 or powers.shape[1] != 2 or len(powers) < 54:
        raise ValueError("Calibration needs at least 54 paired 100 ms bins")
    window = PowerWindow(5)
    smoothed = []
    for row in powers:
        mean = window.push(row)
        if mean is not None:
            smoothed.append(db(mean))
    raw_levels = np.asarray(smoothed)
    raw_thresholds = np.maximum(-55., np.percentile(raw_levels, 75, axis=0) - 12.)
    gains = np.asarray(baseline.baseline_db)
    post = raw_levels + gains
    thresholds = raw_thresholds + gains
    # Keep the original immediate lead-activity veto; smoothing may not bridge a pause.
    raw_lead_active = db(powers[4:, 0]) + gains[0] >= baseline.raw_lead_threshold_dbfs
    joint = (post >= thresholds).all(axis=1) & raw_lead_active
    count = int(joint.sum())
    if count < 50:
        raise ValueError(f"500 ms reference has only {count}/50 jointly supported endpoints")
    reference = float(np.median(post[joint, 1] - post[joint, 0]))
    return PairReference(5, baseline.baseline_db, tuple(thresholds.tolist()), reference,
                         baseline.raw_lead_threshold_dbfs, count)


class PairEvidence:
    """Replay observation gates only; never emit a gain command or process PCM.

    500 ms keeps the original immediate RED activity AND accent vetoes in addition
    to matched-window pair checks. This is an explicit conservative candidate.
    Accepted overlapping windows are not independent statistical samples.
    """

    def __init__(self, reference: PairReference, settings: CoordinatorSettings):
        self.reference, self.settings = reference, settings
        self.window = PowerWindow(reference.window_bins)
        self.history = deque()
        self.previous = self.previous_raw_lead = None
        self.unsupported = 0

    def feed(self, index: int, powers) -> dict:
        s, r = self.settings, self.reference
        required = round(s.evidence_seconds * 10)
        limit = round(s.evidence_window_seconds * 10)
        reset = round(s.unsupported_reset_seconds * 10)
        while self.history and self.history[0][0] <= index - limit:
            self.history.popleft()
        row = {"bin_index": index, "time_seconds": (index + 1) / 10,
               "window_bins": r.window_bins, "relative_error_db": None,
               "median_error_db": None, "cut_evidence_ready": False,
               "evidence_sufficient": False, "gain_applied": False}
        if powers is None:
            self.window.clear()
            self.history.clear()
            self.previous = self.previous_raw_lead = None
            self.unsupported = 0
            row.update(reason="gap", evidence_bins=0)
            return row
        raw_lead = float(db(float(powers[0]))) + r.baseline_db[0]
        mean = self.window.push(powers)
        reason = None
        levels = None if mean is None else db(mean) + r.baseline_db
        if levels is None:
            reason = "hold_window_warmup"
        elif raw_lead < r.raw_lead_threshold_dbfs or levels[0] < r.thresholds_dbfs[0]:
            reason = "hold_lead_inactive"
        elif levels[1] < r.thresholds_dbfs[1]:
            reason = "hold_support_inactive"
        elif ((self.previous is not None
               and np.max(np.abs(levels - self.previous)) > s.accent_step_db)
              or (r.window_bins == 5 and self.previous_raw_lead is not None
                  and abs(raw_lead - self.previous_raw_lead) > s.accent_step_db)):
            reason = "hold_accent"
        if levels is not None:
            error = float(levels[1] - levels[0] - r.reference_db)
            row["relative_error_db"] = error
            if reason is None:
                self.unsupported = 0
                self.history.append((index, error))
                median = float(np.median([value for _, value in self.history]))
                row["median_error_db"] = median
                row["evidence_sufficient"] = len(self.history) >= required
                row["cut_evidence_ready"] = (row["evidence_sufficient"] and
                                             median > s.drift_deadband_db and
                                             error > s.drift_deadband_db)
                reason = "eligible" if row["evidence_sufficient"] else "collect_evidence"
        if reason not in ("eligible", "collect_evidence"):
            self.unsupported = min(reset, self.unsupported + 1)
            if self.unsupported >= reset:
                self.history.clear()
        self.previous = None if levels is None else levels.copy()
        self.previous_raw_lead = raw_lead
        row.update(reason=reason, evidence_bins=len(self.history))
        return row


def evaluate(powers, reference, settings, scenario):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown frozen scenario")
    evidence = PairEvidence(reference, settings)
    for index, original in enumerate(powers):
        row = np.asarray(original, dtype=np.float64).copy()
        if ((scenario == "kit_step_plus10" and 200 <= index < 500)
                or (scenario == "kit_single_hit_plus10" and index == 200)):
            row[1] *= 10.
        elif scenario == "lead_pause" and 200 <= index < 250:
            row[0] = 0.
        yield evidence.feed(index, None if scenario == "gap" and index == 200 else row)


def load_pair(report: dict):
    """Read only saved calibration/evaluation RED and KIT, verifying native PCM hashes."""
    import soundfile as sf

    protocol = report["protocol"]
    rate = protocol["sample_rate"]
    cal_start, cal_end = protocol["calibration_source_frames"]
    eval_start, eval_end = protocol["evaluation_source_frames"]
    values = (cal_start, cal_end, eval_start, eval_end)
    if (rate != 48000 or any(type(v) is not int for v in values)
            or cal_end != eval_start or cal_start < rate
            or not 5 * rate <= cal_end - cal_start <= 60 * rate
            or not 10 * rate <= eval_end - eval_start <= 300 * rate
            or any(v % (rate // 10) for v in values)):
        raise ValueError("Unsupported source-frame ranges")
    all_powers, provenance = [], {}
    for track in PAIR:
        saved = report["source_provenance"][track]
        path = Path(saved["path"]).resolve(strict=True)
        before = path.stat()
        hasher = hashlib.sha256()
        rows = []
        with sf.SoundFile(path, "r") as source:
            if (source.samplerate != rate or source.channels != saved["channels"]
                    or source.frames != saved["frames"] or source.frames <= eval_end):
                raise ValueError(f"Source header mismatch: {track}")
            source.seek(cal_start)
            for _ in range((eval_end - cal_start) // (rate // 10)):
                audio = source.read(rate // 10, dtype="float32", always_2d=True)
                if len(audio) != rate // 10 or not np.isfinite(audio).all():
                    raise ValueError(f"Short/nonfinite source: {track}")
                hasher.update(audio.astype("<f4", copy=False).tobytes())
                # Reproduce the original trial's mono-to-stereo measurement reduction.
                if audio.shape[1] == 1:
                    audio = np.repeat(audio, 2, axis=1)
                rows.append(float(np.mean(audio.astype(np.float64) ** 2)))
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Source changed during read: {track}")
        if hasher.hexdigest() != saved["decoded_window_sha256"]:
            raise ValueError(f"Decoded window hash mismatch: {track}")
        provenance[track] = {"decoded_window_sha256": hasher.hexdigest(),
                             "verified_against_saved_trial": True}
        all_powers.append(rows)
    powers = np.asarray(all_powers).T
    split = (cal_end - cal_start) // (rate // 10)
    return powers[:split], powers[split:], provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New experiment directory")
    args = parser.parse_args()
    try:
        report = read_json(args.report)
        settings = CoordinatorSettings(**report["protocol"]["coordinator_settings"])
        if "KIT" not in settings.enabled_support_tracks:
            raise ValueError("The saved trial did not enable KIT")
        baseline = PairReference(1, tuple(settings.baseline_gains_db[t] for t in PAIR),
                                 tuple(settings.activity_thresholds_dbfs[t] for t in PAIR),
                                 settings.reference_support_to_lead_db["KIT"],
                                 settings.activity_thresholds_dbfs["RED"])
        args.output.mkdir(parents=True, exist_ok=False)
        protocol = {"kind": "kit_measurement_only_not_gain_controller", "candidate_window_ms": 500,
                    "update_ms": 100, "raw_RED_activity_and_accent_vetoes_retained": True,
                    "baseline_gains_changed": False, "source_selection_is_development_data": True,
                    "scenarios": list(SCENARIOS),
                    "fault_start_seconds": 20, "fault_end_seconds": 50,
                    "report_sha256": sha256(args.report),
                    "decisions_sha256": sha256(args.decisions),
                    "implementation_sha256": sha256(Path(__file__)),
                    "settings": settings.as_dict(), "host_writes": False, "live_eligible": False,
                    "comparison_has_gain_trajectory": False}
        (args.output / "protocol.json").write_text(json.dumps(protocol, indent=2), encoding="utf-8")
        calibration, evaluation, provenance = load_pair(report)
        legacy = [e for e in read_json(args.decisions) if e.get("type") == "bin"]
        replay = list(evaluate(evaluation, baseline, settings, "original"))
        if len(replay) != len(legacy):
            raise ValueError("Saved trace length differs from source window")
        for observed, saved in zip(replay, legacy):
            expected = ("eligible" if saved["reasons"]["KIT"] in BALANCE_REASONS
                        else saved["reasons"]["KIT"])
            if (observed["reason"] != expected
                    or observed["evidence_bins"] != saved["evidence_bins"]["KIT"]
                    or not math.isclose(observed["time_seconds"], saved["time_seconds"],
                                        abs_tol=1e-9)
                    or not math.isclose(observed["relative_error_db"],
                                        saved["raw_relative_error_db"]["KIT"], abs_tol=1e-8)):
                when = observed["time_seconds"]
                raise ValueError(f"Baseline measurement replay mismatch at {when} s")
        candidate = calibrate_500ms(calibration, baseline)
        summaries = {}
        for name, reference in (("100ms", baseline), ("500ms", candidate)):
            summaries[name] = {"reference": vars(reference), "scenarios": {}}
            for scenario in SCENARIOS:
                counts, maximum, cuts, ready = Counter(), 0, 0, 0
                fault_cuts, first_cut = 0, None
                trace_path = args.output / f"{name}-{scenario}.jsonl"
                with trace_path.open("x", encoding="utf-8") as output:
                    for row in evaluate(evaluation, reference, settings, scenario):
                        counts[row["reason"]] += 1
                        maximum = max(maximum, row["evidence_bins"])
                        cuts += int(row["cut_evidence_ready"])
                        if row["cut_evidence_ready"]:
                            if first_cut is None:
                                first_cut = row["time_seconds"]
                            fault_cuts += int(200 <= row["bin_index"] < 500)
                        ready += int(row["evidence_sufficient"])
                        output.write(json.dumps(row, allow_nan=False) + "\n")
                summaries[name]["scenarios"][scenario] = {
                    "reason_counts": counts, "maximum_evidence_bins": maximum,
                    "evidence_sufficient_bins": ready, "cut_evidence_ready_bins": cuts,
                    "cut_evidence_ready_bins_20_to_50s": fault_cuts,
                    "first_cut_evidence_seconds": first_cut}
        result = {"protocol": protocol, "source_verification": provenance,
                  "baseline_measurement_replay_matches_saved_trace": True,
                  "comparisons": summaries, "gain_policy_changed": False,
                  "perceptual_improvement_established": False,
                  "limitations": ["Overlapping windows are not independent samples.",
                                  "Measurement eligibility is not a gain or quality judgment.",
                                  "Sustained faults and isolated hits are engineering controls."]}
        (args.output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False),
                                                  encoding="utf-8")
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"KIT measurement study stopped: {exc}\n")


if __name__ == "__main__":
    main()
