"""Integration and adversarial scoring tests; saved scalars only, no host/audio."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wcfi_tools.audio.gain_observations import ROLES
from wcfi_tools.audio.gain_replay import (
    DURATION,
    TransitionTimings,
    cases,
    check_causality,
    make_frames,
    opportunities,
    replay,
    scheduled_frames,
    score,
)


def anchor():
    return {role: tuple([-24. - index] * DURATION) for index, role in enumerate(ROLES)}


def test_all_transition_histogram_is_bounded_and_conservative():
    timing = TransitionTimings()
    for value in [.001] * 95 + [250.] * 5:
        timing.append(value)
    assert timing.describe()["transition_count"] == 100
    assert timing.describe()["p95_upper_bound_ms"] == .01
    assert timing.describe()["max_ms"] == 250
    assert len(timing.bins) == 5001


def test_frozen_case_counts_and_no_truth_in_public_frames():
    fixtures = cases()
    assert len(fixtures) == 77
    assert sum(s["kind"] == "positive" for s in fixtures.values()) == 9
    assert sum(s["kind"] == "fault" for s in fixtures.values()) == 32
    assert sum(s["kind"] == "takeover" for s in fixtures.values()) == 20
    for spec in fixtures.values():
        for frame in make_frames(anchor(), spec):
            assert not {"kind", "expected", "gain", "onset", "episode"}.intersection(frame)


@pytest.mark.parametrize("identifier,spec", list(cases().items()))
def test_synthetic_frozen_cases(identifier, spec):
    data = anchor()
    report = score(spec, replay(data, make_frames(data, spec), until=240), opportunities(data))
    assert report["passed"], (identifier, report)


def test_actual_missing_scheduled_frame_logged_and_counted():
    data = anchor()
    frames = make_frames(data, {"kind": "positive", "gain": 6, "onset": 0, "removal": 90})
    frames = [frame for frame in frames if frame["time_seconds"] != 10]
    logs = replay(data, frames, until=240)
    assert [row["decision"]["time_seconds"] for row in logs] == list(range(2, 241, 2))
    missing = next(row for row in logs if row["decision"]["time_seconds"] == 10)
    assert missing["observation"]["opportunity"] is True
    assert missing["decision"]["action"] == "abstain"
    assert missing["decision"]["raw_history_count"] == 0
    assert all(row["decision"]["action"] != "propose" for row in logs
               if 10 <= row["decision"]["time_seconds"] < 22)


def test_trailing_missing_slot_uses_explicit_event_clock():
    data = anchor()
    frames = make_frames(data, {"kind": "null", "variant": "unchanged"})[:9]
    logs = replay(data, frames, until=10)
    assert logs[-1]["decision"]["time_seconds"] == 10
    assert logs[-1]["decision"]["action"] == "abstain"
    assert logs[-1]["observation"]["opportunity"] is True


def test_schedule_rejects_duplicate_endpoints():
    with pytest.raises(ValueError, match="increase"):
        list(scheduled_frames([{"time_seconds": 2}, {"time_seconds": 2}]))


def test_bad_deadline_support_remains_failure():
    data = anchor()
    spec = {"kind": "positive", "gain": 2, "onset": 20, "removal": 110}
    support = opportunities(data)
    support[40] = False
    result = score(spec, replay(data, make_frames(data, spec)), support)
    assert not result["coverage"]["passed"]
    assert result["coverage"]["missing_deadline_windows"] == [40]
    assert result["coverage"]["denominator_retained"] is True
    assert result["correction"]["classification"] == "insufficient_deadline_support"


def test_scorer_rejects_wrong_direction_even_if_offsets_eventually_recover():
    data = anchor()
    spec = {"kind": "positive", "gain": 3, "onset": 20, "removal": 110}
    logs = replay(data, make_frames(data, spec))
    broken = copy.deepcopy(logs)
    move = next(row["decision"] for row in broken if row["decision"]["action"] == "propose")
    move["delta_db"] = .5
    assert not score(spec, broken, opportunities(data))["direction"]["passed"]


def test_scorer_rejects_transient_crossing_then_overshoot():
    data = anchor()
    truth = {"kind": "positive", "gain": 2, "onset": 20, "removal": 110}
    erroneous = dict(truth, gain=6)
    logs = replay(data, make_frames(data, erroneous))
    report = score(truth, logs, opportunities(data))
    assert not report["correction"]["passed"]
    assert not report["passed"]


def test_scorer_rejects_loss_of_cap_after_deadline_and_late_reversion():
    data = anchor()
    spec = {"kind": "positive", "gain": 6, "onset": 20, "removal": 110}
    logs = replay(data, make_frames(data, spec))
    broken = copy.deepcopy(logs)
    next(row for row in broken if row["decision"]["time_seconds"] == 100)["decision"]["offset_db"] = -2.5
    assert not score(spec, broken, opportunities(data))["correction"]["passed"]
    broken = copy.deepcopy(logs)
    next(row for row in broken if row["decision"]["time_seconds"] == 200)["decision"]["offset_db"] = -.5
    assert not score(spec, broken, opportunities(data))["removal"]["passed"]


def test_entire_prefix_logs_equal_and_future_suffix_cannot_change_them():
    data = anchor()
    spec = {"kind": "takeover", "event": "chain", "at": 60, "rearm": 90, "episode": True}
    frames = make_frames(data, spec)
    report = check_causality(data, frames, replay(data, frames))
    assert report["passed"]
    assert report["checked"] == 120


def test_three_identical_replays_are_identical():
    data = anchor()
    frames = make_frames(data, {"kind": "fault", "fault": "stale", "at": 120, "episode": True})
    assert replay(data, frames) == replay(data, frames) == replay(data, frames)


def test_stream_does_not_read_a_future_frame_before_current_transition(monkeypatch):
    from wcfi_tools.audio import gain_replay
    data = anchor()
    frames = make_frames(data, {"kind": "null", "variant": "unchanged"})
    received = 0
    consumed = 0
    original = gain_replay.Adapter

    class CheckedAdapter(original):
        def push(self, frame):
            nonlocal received
            assert consumed == frame["time_seconds"]
            received = consumed
            return super().push(frame)

    def strict_stream():
        nonlocal consumed
        for frame in frames:
            assert consumed == received, "Future frame read before current decision"
            consumed += 1
            yield frame
    monkeypatch.setattr(gain_replay, "Adapter", CheckedAdapter)
    replay(data, strict_stream(), until=40)
    assert consumed == 40


def test_odd_manual_event_reversion_remains_suspended_in_runner():
    data = anchor()
    frames = make_frames(data, {"kind": "positive", "gain": 6, "onset": 0, "removal": 90})
    frames[12]["revision"] = "manual-fader"
    logs = replay(data, frames)
    assert all(row["decision"]["action"] == "suspended" for row in logs
               if row["decision"]["time_seconds"] >= 14)


def test_rearm_anchor_comes_from_observed_frames_not_private_key():
    data = anchor()
    spec = {"kind": "takeover", "event": "manual_fader", "at": 60, "rearm": 90,
            "episode": True}
    frames = make_frames(data, spec)
    for frame in frames[82:90]:
        frame["levels"]["AC GUITAR"] += 1.25
    events = [row for row in replay(data, frames) if row["decision"]["action"] == "rearmed"]
    assert events[0]["accepted_observed_anchor_offsets_db"]["AC GUITAR"] == 7.25


def test_pure_modules_import_only_scalar_dependencies():
    base = Path(__file__).resolve().parents[1] / "src/wcfi_tools/audio"
    allowed = {"__future__", "math", "collections", "statistics", "typing", "json", "pathlib"}
    for name in ("gain_controller.py", "gain_observations.py"):
        tree = ast.parse((base / name).read_text(encoding="utf-8"))
        modules = {node.module.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        modules |= {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        assert modules <= allowed


def test_cli_help_and_failed_gate_exit(monkeypatch, tmp_path):
    from wcfi_tools.audio import gain_replay
    from wcfi_tools.commands import role_analysis
    from wcfi_tools.commands.gain_shadow import app
    monkeypatch.setattr(role_analysis, "limit_resources", lambda: None)
    monkeypatch.setattr(gain_replay, "run", lambda output: {"status": "failed", "output": str(output)})
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["--output", str(tmp_path / "new")])
    assert result.exit_code == 2
    assert '"status": "failed"' in result.output


def test_forced_prefix_timeout_cannot_label_partial_case_passed(monkeypatch, tmp_path):
    from wcfi_tools.audio import gain_replay as module
    root = tmp_path / "fixture-repo"
    for relative in (module.PLAN, module.REVIEW, Path(*module.SOURCE_SUFFIX)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unit fixture", encoding="utf-8")
    monkeypatch.setattr(module, "PLAN_SHA256", module.file_digest(root / module.PLAN))
    monkeypatch.setattr(module, "OWN_FILES", ())
    monkeypatch.setattr(module, "load_october", lambda _path: anchor())
    monkeypatch.setattr(module, "cases", lambda: {
        "case-009": {"kind": "positive", "gain": 6, "onset": 40, "removal": 130}})
    monkeypatch.setattr(module, "memory", lambda: {
        "working_mib": 30., "peak_mib": 35., "cpu_count": 1, "idle_priority": True})

    def forced(*_args, **_kwargs):
        raise TimeoutError("forced prefix timeout")
    monkeypatch.setattr(module, "check_causality", forced)
    output = tmp_path / "partial"
    result = module.run(output, repo=root)
    saved = json.loads((output / "results.json").read_text(encoding="utf-8"))
    partial = saved["families"]["synthetic"]["cases"]["case-009"]
    assert result["status"] == "failed"
    assert partial["primary_gates_passed"] is True
    assert partial["passed"] is False
    assert partial["causality"] == {"passed": False, "status": "not_completed"}
    assert saved["freeze_integrity"]["passed"] is True
    assert saved["resources"]["post_warmup_growth_mib"] == 5.
    assert (output / "synthetic-case-009.jsonl").exists()
