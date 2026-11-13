"""Add explanations to coordinator telemetry without changing its gain policy.

The original `reasons` field remains intact. `diagnostics` distinguishes signed
balance error, evidence sufficiency, permitted action, and accent attribution.
No audio device, host, network, or playback code is imported here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path

from .mix_coordinator import SUPPORT_TRACKS, TRACKS, CoordinatorSettings, MixCoordinator

DIAGNOSTICS_VERSION = "coordinator_diagnostics_v1"
BALANCE_REASONS = frozenset({
    "hold_deadband", "hold_current_cut_veto", "hold_cut", "attenuate",
    "hold_current_recovery_veto", "hold_master_headroom", "recover",
})


def explain_bin(event: dict, settings: CoordinatorSettings,
                previous_levels: dict | None = None) -> dict:
    """Explain a completed bin. Never mutate the event, settings, or controller.

    Previous levels must be from the immediately preceding valid bin in the
    same stream. A gap or invalid bin makes accent attribution unavailable.
    These are engineering conditions, not source-intent or mix-quality labels.
    """
    if event.get("type") != "bin":
        raise ValueError("Expected a coordinator bin event")
    required = round(settings.evidence_seconds * 10)
    levels = event["levels_after_baseline_dbfs"]
    result = {}
    for track in TRACKS:
        legacy = event["reasons"][track]
        median = event["rolling_median_error_db"].get(track)
        evidence = event["evidence_bins"][track]
        current = event["current_correction_db"][track]
        target = event["target_correction_db"][track]
        qualified = median is not None and not event["invalid"]
        decision_reached = legacy in BALANCE_REASONS
        reason = legacy
        if legacy == "hold_deadband":
            if median is None or evidence < required:
                raise ValueError("hold_deadband requires sufficient qualified evidence")
            if abs(current) <= 1e-12:
                reason = ("hold_no_positive_drift" if median <= 0
                          else "hold_below_cut_threshold")
            else:
                reason = "hold_recovery_hysteresis"
        deltas = None
        accent_sources = None
        if track in SUPPORT_TRACKS and previous_levels is not None and not event["invalid"]:
            deltas = {name: levels[name] - previous_levels[name] for name in ("RED", track)}
            accent_sources = [name for name, delta in deltas.items()
                              if abs(delta) > settings.accent_step_db]
        action = ("attenuate" if legacy == "attenuate" else
                  "recover" if legacy == "recover" else
                  "manual" if legacy == "manual" else "hold")
        result[track] = {
            "reason": reason, "legacy_reason": legacy, "action": action,
            "observation_qualified": qualified,
            "evidence_bins_available": evidence, "evidence_bins_required": required,
            "evidence_sufficient_now": qualified and evidence >= required,
            "balance_decision_reached": decision_reached,
            "relative_error_db": event["raw_relative_error_db"].get(track),
            "rolling_median_error_db": median,
            "lead_activity_margin_db": levels["RED"] - settings.activity_thresholds_dbfs["RED"],
            "support_activity_margin_db": (levels[track] - settings.activity_thresholds_dbfs[track]
                                           if track in SUPPORT_TRACKS else None),
            "level_step_db": deltas, "accent_sources": accent_sources,
            "current_correction_db": current, "target_correction_db": target,
            "automatic_boost_above_baseline_allowed": False,
        }
    return result


class DiagnosticMixCoordinator(MixCoordinator):
    """Opt-in instrumentation; inherited sample processing and decisions are unchanged."""

    def _finish_bin(self):
        previous = (dict(zip(TRACKS, self._previous_levels.tolist()))
                    if self._previous_levels is not None else None)
        super()._finish_bin()
        event = self.events[-1]
        event["diagnostics_version"] = DIAGNOSTICS_VERSION
        event["diagnostics"] = explain_bin(event, self.settings, previous)


def annotate_events(events: Iterable[dict], settings: CoordinatorSettings) -> Iterable[dict]:
    """Annotate saved events; gaps, invalid bins and missing frames break continuity."""
    previous, last_frame, last_time = None, None, None
    for original in events:
        event = deepcopy(original)
        if event.get("type") != "bin":
            if event.get("type") == "gap":
                previous, last_frame, last_time = None, None, None
            yield event
            continue
        frame, time = event["frame"], event["time_seconds"]
        if last_frame is not None:
            if frame <= last_frame or time <= last_time:
                raise ValueError("Bin frames and times must increase")
            if not math.isclose(time - last_time, .1, abs_tol=1e-9):
                previous = None
        event["diagnostics_version"] = DIAGNOSTICS_VERSION
        event["diagnostics"] = explain_bin(event, settings, previous)
        previous = None if event["invalid"] else event["levels_after_baseline_dbfs"]
        last_frame, last_time = frame, time
        yield event


def summarize_events(events: Iterable[dict], settings: CoordinatorSettings) -> dict:
    """Bounded counters; no audio or per-sample data is retained."""
    result = {track: {"bins": 0, "qualified_observations": 0,
                     "balance_decisions": 0, "maximum_evidence_bins": 0,
                     "correction_min_db": None, "correction_max_db": None,
                     "reason_counts": Counter(), "accent_source_counts": Counter()}
              for track in TRACKS}
    for event in annotate_events(events, settings):
        if event.get("type") != "bin":
            continue
        for track, detail in event["diagnostics"].items():
            row = result[track]
            row["bins"] += 1
            row["qualified_observations"] += int(detail["observation_qualified"])
            row["balance_decisions"] += int(detail["balance_decision_reached"])
            row["maximum_evidence_bins"] = max(row["maximum_evidence_bins"],
                                                detail["evidence_bins_available"])
            gain = detail["current_correction_db"]
            for name, fn in (("correction_min_db", min), ("correction_max_db", max)):
                row[name] = gain if row[name] is None else fn(row[name], gain)
            row["reason_counts"][detail["reason"]] += 1
            if detail["legacy_reason"] == "hold_accent":
                sources = detail["accent_sources"]
                label = "unknown" if sources is None else "+".join(sources)
                row["accent_source_counts"][label] += 1
    return {"version": DIAGNOSTICS_VERSION, "gain_policy_changed": False,
            "live_eligible": False, "perceptual_improvement_established": False,
            "reason_counts_are_first_reported_conditions": True, "tracks": result}


def read_json(path: Path, max_bytes: int = 64 * 1024 * 1024):
    if path.stat().st_size > max_bytes:
        raise ValueError(f"JSON file exceeds {max_bytes} bytes: {path}")
    def reject_nonfinite(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_nonfinite)


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("--report", type=Path, required=True, help="The same trial's report.json")
    parser.add_argument("--output", type=Path, required=True,
                        help="New JSON file; never overwritten")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Output already exists")
        report = read_json(args.report)
        settings = CoordinatorSettings(**report["protocol"]["coordinator_settings"])
        events = read_json(args.decisions)
        if not isinstance(events, list):
            raise ValueError("Expected a JSON array of coordinator events")  # noqa: TRY004
        result = summarize_events(events, settings)
        result["inputs"] = {"decisions_sha256": sha256(args.decisions),
                            "report_sha256": sha256(args.report)}
        result["diagnostic_module_sha256"] = sha256(Path(__file__))
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(result, output, indent=2, allow_nan=False)
            output.write("\n")
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Diagnostic review failed: {exc}\n")


if __name__ == "__main__":
    main()
