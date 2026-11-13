"""Controller arithmetic/fault fixtures, not independent musical validation."""
import ast
import json
import math
from itertools import pairwise
from pathlib import Path

import pytest

from wcfi_tools.audio.gain_controller import POLICY, GainController


def observation(t, raw=0.0, **overrides):
    return {
        "time_seconds": t,
        "raw_error_db": raw,
        "eligible": True,
        "reason": "supported_engineering_fixture",
        "observation_age_seconds": 0.0,
        "state_age_seconds": 0.0,
        "contiguous_seconds": min(t, 8.0),
        "revision": "base",
        "anchor_revision": "base",
        **overrides,
    }


def replay(raw_by_time, end=240):
    controller = GainController()
    return [controller.step(observation(t, raw_by_time(t))) for t in range(2, end + 1, 2)]


@pytest.mark.parametrize("onset", [20, 30, 40])
@pytest.mark.parametrize("drift", [2.0, 3.0, 6.0])
def test_nine_supported_episode_direction_deadlines_bounds_and_removal(onset, drift):
    removed = onset + 90
    rows = replay(lambda t: drift if onset <= t < removed else 0.0)
    during = [r for r in rows if onset <= r["time_seconds"] < removed]
    moves = [r for r in rows if r["delta_db"]]
    assert next(r for r in during if r["delta_db"])["delta_db"] == -0.5
    assert all(r["delta_db"] <= 0 for r in during)
    deadline = onset + (70 if drift == 6 else 60)
    settled = next(r for r in rows if r["time_seconds"] == deadline)
    if drift == 6:
        assert settled["offset_db"] == -3
        assert settled["reason"] == "cap_unresolved"
    else:
        assert abs(drift + settled["offset_db"]) <= 0.75
    assert next(r for r in rows if r["time_seconds"] == removed + 70)["offset_db"] == 0
    assert all(-3 <= r["offset_db"] <= 0 for r in rows)
    assert all(abs(r["delta_db"]) == 0.5 for r in moves)
    assert all(b["time_seconds"] - a["time_seconds"] >= 10 for a, b in pairwise(moves))
    for r in rows:
        assert r["effective_gain"] == 10 ** (r["offset_db"] / 20)
        assert r["offset_db"] == r["total_before_db"] + r["delta_db"]


@pytest.mark.parametrize("drift", [-3.0, -0.5, 0.0, 0.5])
def test_no_initial_boost_or_small_drift_proposal(drift):
    assert all(r["offset_db"] == 0 for r in replay(lambda _: drift))


def test_small_jitter_never_moves():
    assert all(r["delta_db"] == 0 for r in replay(lambda t: 0.5 if t % 4 else -0.5))


def test_three_new_estimates_after_move_and_total_gain_applied_once():
    controller = GainController()
    rows = [controller.step(observation(t, 2)) for t in range(8, 34, 2)]
    assert [r["time_seconds"] for r in rows if r["delta_db"]] == [12, 22, 32]
    assert [r["offset_db"] for r in rows if r["delta_db"]] == [-0.5, -1.0, -1.5]
    assert rows[2]["new_estimates_since_move"] == 0
    assert rows[3]["new_estimates_since_move"] == 1
    assert rows[4]["new_estimates_since_move"] == 2
    assert rows[5]["new_estimates_since_move"] == 3
    assert rows[5]["reason"] == "cadence_wait"
    assert rows[-1]["corrected_error_db"] == 1.0  # Before the third move: 2 + (-1).
    assert controller.step(observation(34, 2))["corrected_error_db"] == 0.5


def test_missed_evaluation_cannot_vote_using_old_positive_history():
    controller = GainController()
    controller.step(observation(8, 6))
    controller.step(observation(10, 6))
    row = controller.step(observation(100, 0))
    assert row["timing_status"] == "missed_evaluation_reset"
    assert row["raw_history_count"] == 1
    assert row["corrected_error_db"] == 0
    assert row["offset_db"] == 0
    assert row["delta_db"] == 0
    assert row["direction_count"] == 0


def test_fast_estimates_cannot_collect_votes_or_shift_scheduled_evaluations():
    controller = GainController()
    controller.step(observation(8, 3))
    for t in [8.01, 8.02]:
        row = controller.step(observation(t, 3))
        assert row["action"] == "abstain"
        assert row["reason"] == "evaluation_too_soon"
        assert row["timing_status"] == "too_soon"
        assert row["raw_history_count"] == 0
        assert row["direction_count"] == 0
        assert row["delta_db"] == 0
    rows = [controller.step(observation(t, 3)) for t in [10, 12, 14]]
    assert [r["delta_db"] for r in rows] == [0, 0, -0.5]
    assert all(r["timing_status"] == "scheduled" for r in rows)


def test_gap_restarts_three_votes_without_resetting_last_move_cadence():
    controller = GainController()
    for t in [8, 10, 12]:
        controller.step(observation(t, 6))
    assert controller.offset_db == -0.5
    rows = [controller.step(observation(t, 6)) for t in [16, 18, 20, 22]]
    assert rows[0]["timing_status"] == "missed_evaluation_reset"
    assert [r["new_estimates_since_move"] for r in rows] == [1, 2, 3, 0]
    assert rows[2]["reason"] == "cadence_wait"
    assert [r["delta_db"] for r in rows] == [0, 0, 0, -0.5]


def test_gap_restarts_release_evidence_too():
    controller = GainController()
    for t in [8, 10, 12]:
        controller.step(observation(t, 3))
    for t in [14, 16]:
        controller.step(observation(t, 0))
    rows = [controller.step(observation(t, 0)) for t in [100, 102, 104]]
    assert rows[0]["timing_status"] == "missed_evaluation_reset"
    assert [r["delta_db"] for r in rows] == [0, 0, 0.5]


def test_same_three_raw_return_estimates_satisfy_release_direction():
    controller = GainController()
    for t in [8, 10, 12]:
        controller.step(observation(t, 2))
    for t in [14, 16, 18, 20, 22]:
        controller.step(observation(t, 1))
    assert controller.offset_db == -0.5
    rows = [controller.step(observation(t, 0)) for t in [24, 26, 28]]
    assert [r["direction_count"] for r in rows[:2]] == [1, 2]
    assert [r["delta_db"] for r in rows] == [0, 0, 0.5]
    assert rows[-1]["reason"] == "release_raw_returned"
    assert rows[-1]["offset_db"] == 0


def test_nonconsecutive_raw_return_estimates_do_not_authorize_release():
    controller = GainController()
    for t in [8, 10, 12]:
        controller.step(observation(t, 2))
    for t in [14, 16, 18, 20, 22]:
        controller.step(observation(t, 1))
    rows = [controller.step(observation(t, raw))
            for t, raw in [(24, 0), (26, 0), (28, 1), (30, 0), (32, 0), (34, 0)]]
    assert [r["delta_db"] for r in rows] == [0, 0, 0, 0, 0, 0.5]


def test_deadband_requires_entry_again_after_attenuation_stops():
    controller = GainController()
    for t in range(8, 46, 2):
        controller.step(observation(t, 2))
    assert controller.offset_db == -1.5
    for t in range(46, 76, 2):
        row = controller.step(observation(t, 2.5))
        assert row["delta_db"] == 0  # Corrected +1: above hold threshold, below new entry.
    assert controller.offset_db == -1.5


def test_release_only_undoes_own_attenuation():
    controller = GainController()
    for t in range(8, 80, 2):
        controller.step(observation(t, 6))
    assert controller.offset_db == -3
    rows = [controller.step(observation(t, -3)) for t in range(80, 180, 2)]
    assert all(r["delta_db"] >= 0 for r in rows)
    assert all(r["offset_db"] <= 0 for r in rows)
    assert rows[-1]["offset_db"] == 0
    assert any(r["reason"] == "relieve_overcorrection" for r in rows)


@pytest.mark.parametrize("field,value,reason", [
    ("observation_age_seconds", 0.5, "observation_stale_or_invalid_age"),
    ("observation_age_seconds", -0.1, "observation_stale_or_invalid_age"),
    ("observation_age_seconds", float("nan"), "observation_stale_or_invalid_age"),
    ("state_age_seconds", 2.50001, "state_stale_or_invalid_age"),
    ("state_age_seconds", -1, "state_stale_or_invalid_age"),
    ("contiguous_seconds", 7.999, "fresh_history_required"),
    ("contiguous_seconds", None, "fresh_history_required"),
    ("raw_error_db", float("nan"), "nonfinite_or_missing_drift"),
    ("raw_error_db", float("inf"), "nonfinite_or_missing_drift"),
    ("raw_error_db", None, "nonfinite_or_missing_drift"),
    ("raw_error_db", True, "nonfinite_or_missing_drift"),
    ("eligible", False, "supported_engineering_fixture"),
])
def test_invalid_data_discards_pending_direction_and_recovers(field, value, reason):
    controller = GainController()
    controller.step(observation(8, 6))
    controller.step(observation(10, 6))
    row = controller.step(observation(12, 6, **{field: value}))
    assert row["action"] == "abstain"
    assert row["reason"] == reason
    assert row["raw_history_count"] == 0
    assert row["direction_count"] == 0
    assert row["offset_db"] == 0
    rows = [controller.step(observation(t, 6)) for t in [14, 16, 18]]
    assert [r["delta_db"] for r in rows] == [0, 0, -0.5]
    json.dumps(row, allow_nan=False)


def test_freshness_boundaries_are_exact():
    row = GainController().step(observation(8, 6, observation_age_seconds=0.499999,
                                           state_age_seconds=2.5))
    assert row["eligible"]


def test_recovery_uses_actual_contiguous_window_not_eight_seconds_after_last_veto():
    controller = GainController()
    controller.step(observation(8, 6))
    controller.step(observation(10, 6))
    # Actual invalid frame ended at 12. Overlapping windows remain invalid until 20.
    for t in [12, 14, 16, 18]:
        assert controller.step(observation(t, 6, contiguous_seconds=t - 12))["delta_db"] == 0
    rows = [controller.step(observation(t, 6, contiguous_seconds=t - 12)) for t in [20, 22, 24]]
    assert [r["delta_db"] for r in rows] == [0, 0, -0.5]
    assert rows[0]["eligible"]


def test_transient_invalidity_retains_active_mode_but_not_pending_evidence():
    controller = GainController()
    for t in range(8, 24, 2):
        controller.step(observation(t, 2))
    assert controller.offset_db == -1
    invalid = controller.step(observation(24, 2, eligible=False, reason="gap"))
    assert invalid["raw_history_count"] == 0
    assert invalid["direction_count"] == 0
    # The adapter already provides eight contiguous seconds after the bad frame.
    rows = [controller.step(observation(t, 2)) for t in [32, 34, 36]]
    assert [r["delta_db"] for r in rows] == [0, 0, -0.5]
    assert rows[-1]["offset_db"] == -1.5


@pytest.mark.parametrize("changes,reason", [
    ({"revision": "manual-fader"}, "revision_changed"),
    ({"revision": "routing"}, "revision_changed"),
    ({"revision": "chain"}, "revision_changed"),
    ({"revision": "session"}, "revision_changed"),
    ({"anchor_revision": "other"}, "revision_changed"),
    ({"completed_revision": "previous"}, "inflight_revision_changed"),
])
def test_revision_suspends_until_explicit_new_anchor_rearm(changes, reason):
    controller = GainController()
    for t in [8, 10, 12]:
        controller.step(observation(t, 6))
    assert controller.offset_db == -0.5
    first = controller.step(observation(14, 6, **changes))
    assert first["action"] == "suspended"
    assert first["reason"] == reason
    for t in range(16, 46, 2):
        row = controller.step(observation(t, 6))
        assert row["action"] == "suspended"
        assert row["delta_db"] == 0
        assert row["offset_db"] == -0.5
    rearmed = controller.rearm(44, "accepted-new", "accepted-new-anchor")
    assert rearmed["action"] == "rearmed"
    assert rearmed["offset_db"] == 0
    assert rearmed["total_before_db"] == -0.5
    assert rearmed["delta_db"] == 0  # Reset is virtual; it is not a restore proposal.
    for t in [46, 48, 50]:
        row = controller.step(observation(t, 6, revision="accepted-new",
                                          anchor_revision="accepted-new-anchor"))
        assert not row["eligible"]
    rows = [controller.step(observation(t, 6, revision="accepted-new",
                                       anchor_revision="accepted-new-anchor"))
            for t in [52, 54, 56]]
    assert [r["delta_db"] for r in rows] == [0, 0, -0.5]


@pytest.mark.parametrize("revision,anchor", [("base", "new"), ("new", "base"), ("", "new")])
def test_rearm_cannot_reuse_old_identity(revision, anchor):
    with pytest.raises(ValueError):
        GainController().rearm(20, revision, anchor)


@pytest.mark.parametrize("time", [8, 7, -1, float("nan"), float("inf"), True])
def test_bad_clock_suspends_and_returns_strict_json(time):
    controller = GainController()
    controller.step(observation(8, 6))
    row = controller.step(observation(time, 6))
    assert row["action"] == "suspended"
    assert row["delta_db"] == 0
    assert controller.step(observation(10, 6))["action"] == "suspended"
    json.dumps(row, allow_nan=False)


def test_rearm_cannot_rewind_clock():
    controller = GainController()
    controller.step(observation(8, 6))
    with pytest.raises(ValueError):
        controller.rearm(7, "new", "new")


def test_every_prefix_and_adversarial_future_has_identical_prior_decisions():
    inputs = [observation(t, 6 if 20 <= t < 110 else 0) for t in range(2, 180, 2)]

    def run(sequence):
        controller = GainController()
        return [controller.step(item) for item in sequence]

    complete = run(inputs)
    for length in range(1, len(inputs) + 1):
        assert run(inputs[:length]) == complete[:length]
        future = [observation(t, 1000, eligible=False, reason="future", revision="future")
                  for t in range(inputs[length - 1]["time_seconds"] + 2, 182, 2)]
        assert run(inputs[:length] + future)[:length] == complete[:length]
    assert run(inputs) == run(inputs) == complete


def test_inputs_are_immutable_and_internal_state_is_bounded():
    controller = GainController()
    for t in range(8, 20008, 2):
        source = observation(t, 6)
        before = dict(source)
        row = controller.step(source)
        assert source == before
        assert row["raw_history_count"] <= 3
        assert row["direction_count"] <= 3
        assert row["new_estimates_since_move"] <= 3
    assert len(controller._raw) == 3
    assert controller._release_count <= 3
    assert math.isfinite(row["effective_gain"])


def test_pure_module_has_only_declared_stdlib_imports_and_no_io_calls():
    import wcfi_tools.audio.gain_controller as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "__import__"}
    assert set(imports) == {"__future__", "collections", "math", "statistics"}
    assert POLICY["live_musical_policy"] is False
