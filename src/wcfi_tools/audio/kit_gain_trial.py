"""Offline KIT gain-envelope counterfactual on verified RED/KIT observations.

This is NOT an Ableton controller, renderer, or processor. A completed 100 ms
observation may change only the NEXT bin's virtual gain. No PCM is emitted,
changed, played, or saved. The 500 ms measurement is experimental and does not
certify safe or perceptually preferable mixing.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from .coordinator_diagnostics import BALANCE_REASONS, read_json, sha256
from .kit_observation_trial import (
    PAIR,
    SCENARIOS,
    PairReference,
    calibrate_500ms,
    evaluate,
    load_pair,
)
from .mix_coordinator import CoordinatorSettings


class VirtualKitGain:
    """Bin-causal, gain-only simulator, NOT host or sample-processing code.

    The preceding observation supplies the target for this bin. The target can
    advance at most slew_db_per_second / 10 per complete 100 ms bin. Only after
    this bin ends can its observation select the target for the following bin.
    An invalid/unqualified observation freezes subsequent movement, retaining
    the currently reached gain rather than undoing or applying stale evidence.
    """

    def __init__(self, settings: CoordinatorSettings):
        self.settings = settings
        self.current_db = 0.0
        self.target_db = 0.0
        self.previous_index = -1

    def feed(self, observation: dict) -> dict:
        index = observation["bin_index"]
        if type(index) is not int or index != self.previous_index + 1:
            raise ValueError("Gain simulation requires consecutive zero-based bins")
        self.previous_index = index
        current = self.current_db
        step = self.settings.slew_db_per_second / 10
        # Apply only the target previously decided, not the current observation.
        motion = max(-step, min(step, self.target_db - current))
        applied = current + motion
        if abs(applied) <= 1e-12:
            applied = 0.0
        self.current_db = applied
        self.target_db = applied  # Default hold, including on missing/invalid input.
        chosen = "hold"
        reason = observation["reason"]
        error, median = observation["relative_error_db"], observation["median_error_db"]
        if reason == "eligible" and observation["evidence_sufficient"]:
            if not all(isinstance(v, (float, int)) and math.isfinite(v)
                       for v in (error, median)):
                raise ValueError("Eligible observations require finite drift estimates")
            deadband = self.settings.drift_deadband_db
            recovery_edge = deadband - self.settings.recovery_hysteresis_db
            if median > deadband:
                if error > deadband:
                    desired = -min(self.settings.max_cut_db,
                                   max(0., min(median, error) - deadband))
                    if desired < applied - 1e-12:
                        self.target_db = desired
                        chosen = "attenuate"
                        reason = "positive_sustained_drift"
                    else:
                        reason = "hold_existing_cut"
                else:
                    reason = "instantaneous_cut_veto"
            elif applied < -1e-12 and median <= recovery_edge:
                if error <= recovery_edge:
                    self.target_db = 0.0
                    chosen = "recover"
                    reason = "recovery_supported_virtual_only"
                else:
                    reason = "instantaneous_recovery_veto"
            elif median <= 0:
                reason = "hold_no_positive_drift"
            elif median <= deadband:
                reason = "hold_below_cut_threshold_or_hysteresis"
            else:
                reason = "hold"
        if not -self.settings.max_cut_db - 1e-9 <= self.current_db <= 1e-9:
            raise AssertionError("Virtual gain escaped bounded attenuation")
        if not -self.settings.max_cut_db - 1e-9 <= self.target_db <= 1e-9:
            raise AssertionError("Virtual target escaped bounded attenuation")
        return {
            "bin_index": index,
            "time_end_seconds": observation["time_seconds"],
            "gain_db_at_start": current,
            "gain_db_at_end": self.current_db,
            "next_bin_target_db": self.target_db,
            "gain_delta_db_in_bin": self.current_db - current,
            "action_after_observation": chosen,
            "decision_reason": reason,
            "measurement_reason": observation["reason"],
            "evidence_bins": observation["evidence_bins"],
            "evidence_sufficient": observation["evidence_sufficient"],
            "cut_evidence_ready": observation["cut_evidence_ready"],
            "relative_error_db": error,
            "median_error_db": median,
            "output_is_virtual_only": True,
        }


def simulate(powers, reference: PairReference, settings: CoordinatorSettings, scenario: str):
    gain = VirtualKitGain(settings)
    for observation in evaluate(powers, reference, settings, scenario):
        yield gain.feed(observation)


def summarize(rows, settings: CoordinatorSettings) -> dict:
    actions, reasons = Counter(), Counter()
    maximum_cut, first_command, first_change, first_recovery = 0., None, None, None
    in_fault_nonzero = outside_fault_nonzero = bins_nonzero = 0
    min_gain, max_gain, max_step, final = 0., 0., 0., 0.
    sample_count = 0
    for row in rows:
        sample_count += 1
        delta = row["gain_delta_db_in_bin"]
        if abs(delta) > settings.slew_db_per_second / 10 + 1e-9:
            raise ValueError("Slew bound violated")
        end = row["gain_db_at_end"]
        if not -settings.max_cut_db - 1e-9 <= end <= 1e-9:
            raise ValueError("Gain bound violated")
        actions[row["action_after_observation"]] += 1
        reasons[row["decision_reason"]] += 1
        maximum_cut = min(maximum_cut, end)
        min_gain, max_gain = min(min_gain, end), max(max_gain, end)
        max_step = max(max_step, abs(delta))
        final = end
        if row["action_after_observation"] == "attenuate" and first_command is None:
            first_command = row["time_end_seconds"]
        if delta < -1e-12 and first_change is None:
            first_change = row["time_end_seconds"]
        if row["action_after_observation"] == "recover" and first_recovery is None:
            first_recovery = row["time_end_seconds"]
        if end < -1e-12:
            bins_nonzero += 1
            if 200 <= row["bin_index"] < 500:
                in_fault_nonzero += 1
            else:
                outside_fault_nonzero += 1
    return {
        "bin_count": sample_count,
        "action_counts": dict(actions),
        "reason_counts": dict(reasons),
        "minimum_applied_gain_db": min_gain,
        "maximum_applied_gain_db": max_gain,
        "maximum_abs_bin_gain_change_db": max_step,
        "nonzero_gain_bins": bins_nonzero,
        "nonzero_gain_bins_inside_fault_interval": in_fault_nonzero,
        "nonzero_gain_bins_outside_fault_interval": outside_fault_nonzero,
        "first_cut_command_seconds": first_command,
        "first_applied_attenuation_bin_end_seconds": first_change,
        "first_recovery_command_seconds": first_recovery,
        "final_gain_db": final,
        "gain_bounds_ok": min_gain >= -settings.max_cut_db - 1e-9 and max_gain <= 1e-9,
        "slew_bound_ok": max_step <= settings.slew_db_per_second / 10 + 1e-9,
    }


def verify_saved_100ms(evaluation, saved_decisions, reference, settings):
    """Fail closed if this run's 100 ms measurements disagree with frozen evidence."""
    prior = read_json(saved_decisions)
    if not isinstance(prior, list):
        raise ValueError("Saved decisions must be a JSON array")  # noqa: TRY004
    legacy = [item for item in prior if item.get("type") == "bin"]
    replay = list(evaluate(evaluation, reference, settings, "original"))
    if len(legacy) != len(replay):
        raise ValueError("Saved 100 ms decision count disagrees with the recording")
    for observed, saved in zip(replay, legacy):
        reported = saved["reasons"]["KIT"]
        expected = "eligible" if reported in BALANCE_REASONS else reported
        both = (observed["reason"] == expected
                and observed["evidence_bins"] == saved["evidence_bins"]["KIT"]
                and math.isclose(observed["time_seconds"], saved["time_seconds"], abs_tol=1e-9))
        old_error, new_error = saved["raw_relative_error_db"]["KIT"], observed["relative_error_db"]
        both = both and old_error is not None and new_error is not None and math.isclose(
            old_error, new_error, abs_tol=1e-8)
        if not both:
            raise ValueError(f"Saved baseline mismatch at {observed['time_seconds']} seconds")
    # The original 100 ms KIT gain never moved. Require exact parity for this passage.
    if any(abs(row["current_correction_db"]["KIT"]) > 1e-10 for row in legacy):
        raise ValueError("This specific baseline requires a zero KIT gain trace")
    replayed = list(simulate(evaluation, reference, settings, "original"))
    if any(abs(row["gain_db_at_end"]) > 1e-10 for row in replayed):
        raise ValueError("Virtual 100 ms KIT gain disagrees with the saved baseline")
    return len(replay)


def run_study(report_path: Path, decisions_path: Path, output_dir: Path) -> dict:
    """Write only evidence to a fresh directory; preserve all recorded sources."""
    if output_dir.exists():
        raise ValueError("Use a new directory; existing output must not be overwritten")
    report = read_json(report_path)
    settings = CoordinatorSettings(**report["protocol"]["coordinator_settings"])
    if "KIT" not in settings.enabled_support_tracks:
        raise ValueError("This experiment requires a calibrated, enabled KIT")
    baseline = PairReference(1, tuple(settings.baseline_gains_db[t] for t in PAIR),
                             tuple(settings.activity_thresholds_dbfs[t] for t in PAIR),
                             settings.reference_support_to_lead_db["KIT"],
                             settings.activity_thresholds_dbfs["RED"])
    # Verify all inputs before producing an output directory.
    calibration, evaluation, source_verification = load_pair(report)
    verified_count = verify_saved_100ms(evaluation, decisions_path, baseline, settings)
    candidate = calibrate_500ms(calibration, baseline)
    protocol = {
        "kind": "kit_virtual_gain_envelope_no_host_or_audio_output",
        "source_selection": "previously_examined_development_recording",
        "sample_rate_hz": report["protocol"]["sample_rate"],
        "observation_bin_ms": 100,
        "candidate_measurement_window_ms": 500,
        "scenarios": list(SCENARIOS),
        "fault_interval_seconds": [20, 50],
        "source_verification": source_verification,
        "saved_100ms_trace_replay_verified_bins": verified_count,
        "baseline_reference": vars(baseline),
        "candidate_reference": vars(candidate),
        "settings": settings.as_dict(),
        "report_sha256": sha256(report_path),
        "decisions_sha256": sha256(decisions_path),
        "code_sha256": sha256(Path(__file__)),
        "gain_applied_to_audio": False,
        "host_writes": 0,
        "live_eligible": False,
        "perceptual_improvement_established": False,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "protocol.json").write_text(json.dumps(protocol, indent=2, allow_nan=False), encoding="utf-8")
    comparisons = {}
    for name, reference in (("100ms", baseline), ("500ms", candidate)):
        comparisons[name] = {}
        for scenario in SCENARIOS:
            trace_file = output_dir / f"{name}-{scenario}-gain.jsonl"
            def rows(trace_file=trace_file, reference=reference, scenario=scenario):
                with trace_file.open("x", encoding="utf-8", newline="\n") as destination:
                    for row in simulate(evaluation, reference, settings, scenario):
                        destination.write(json.dumps(row, allow_nan=False) + "\n")
                        yield row
            comparisons[name][scenario] = summarize(rows(), settings)
    controls = {
        "100ms_replay_zero_gain": comparisons["100ms"]["original"]["nonzero_gain_bins"] == 0,
        "500ms_original_no_gain": comparisons["500ms"]["original"]["nonzero_gain_bins"] == 0,
        "500ms_sustained_fault_attenuated": comparisons["500ms"]["kit_step_plus10"]["minimum_applied_gain_db"] < -0.05,
        "500ms_isolated_hit_no_gain": comparisons["500ms"]["kit_single_hit_plus10"]["nonzero_gain_bins"] == 0,
        "500ms_red_pause_no_gain": comparisons["500ms"]["lead_pause"]["nonzero_gain_bins"] == 0,
        "500ms_gap_no_gain": comparisons["500ms"]["gap"]["nonzero_gain_bins"] == 0,
    }
    result = {
        "protocol": protocol,
        "comparisons": comparisons,
        "engineering_controls": controls,
        "engineering_controls_pass": all(controls.values()),
        "gain_policy_applied_to_host": False,
        "audio_rendered": False,
        "perceptual_improvement_established": False,
        "limitations": [
            "Virtual envelopes, not an actual mixed stereo waveform, audition or loudness evaluation.",
            "No full-mix peak reconstruction or host headroom guarantee; recovery is virtual only.",
            "Overlapping 500 ms observations are dependent; bin counts are not independent evidence.",
            "The +10 dB step and other disturbances are synthetic engineering controls.",
            "The recorded passage has been inspected previously and is not held out.",
            "All dynamic decisions are restricted to KIT; other tracks' baseline gains are frozen.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New, nonexistent directory")
    args = parser.parse_args()
    try:
        result = run_study(args.report, args.decisions, args.output)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"KIT offline gain study stopped: {exc}\n")


if __name__ == "__main__":
    main()
