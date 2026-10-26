"""Focused engineering contracts for a scalar shadow policy, not mix-quality tests.

All inputs are synthetic scalar envelopes. No recording, host, emulator, node,
network or model is needed. The official prospective matrix is kept separately.
"""
from __future__ import annotations

import ast
import copy
import math
from pathlib import Path

import pytest

from wcfi_tools.audio.experimental_audio_gain import ExperimentalAudioGain


ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")
CONTEXT = {"KIT": -4.0, "BASS": -2.0, "KEYS": -6.0, "EGUITAR": -5.0}


def frame(t, ac=0.0, common=0.0, **changes):
    """Independent period-four AC and period-two lead, with stable contexts."""
    red = -30.0 + (1.0 if t % 2 else -1.0)
    acoustic = -30.0 + (-2.0 if (t - 1) % 4 < 2 else 2.0)
    levels = {"RED": red + common, "AC GUITAR": acoustic + ac + common}
    levels.update({role: red + offset + common for role, offset in CONTEXT.items()})
    result = {
        "time_seconds": t,
        "sequence": t,
        "acquired_at": float(t),
        "received_at": float(t),
        "state_observed_at": float(t),
        "source_ids": {role: "source:" + role for role in ROLES},
        "source_map_revision": "mapping:1",
        "routing_revision": "routing:1",
        "state_revision": "state:1",
        "epoch": "epoch:1",
        "alignment_known": True,
        "levels": levels,
    }
    result.update(changes)
    return result


def feed_range(policy, start, stop, transform=None):
    rows = []
    for t in range(start, stop + 1):
        value = frame(t)
        if transform:
            value = transform(t, value)
        if value is not None:
            rows.extend(policy.feed(value))
    return rows


def ac_step(t, value, start=33, stop=96, gain=4.0):
    if start <= t <= stop:
        value["levels"]["AC GUITAR"] += gain
    return value


def changes(rows, direction=None):
    selected = [row for row in rows if row["delta_db"] != 0]
    if direction == "cut":
        selected = [row for row in selected if row["delta_db"] < 0]
    if direction == "release":
        selected = [row for row in selected if row["delta_db"] > 0]
    return selected


def at(rows, t):
    return next(row for row in rows if row["time_seconds"] == t)


def assert_bounded(rows):
    moves = changes(rows)
    for row in rows:
        assert -3.0 <= row["offset_db"] <= 0.0
        assert abs(row["delta_db"]) in (0.0, 0.5)
        if row["delta_db"]:
            assert row["eligible"] is True
        if row["controller"] is not None:
            # A returned synchronous controller mutation must never be hidden.
            assert row["controller"]["delta_db"] == row["delta_db"]
            assert row["controller"]["offset_db"] == row["offset_db"]
    assert all(b["time_seconds"] - a["time_seconds"] >= 10
               for a, b in zip(moves, moves[1:]))


def test_reference_is_unavailable_until_sixteen_frames_and_is_then_frozen():
    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 15)
    assert policy.reference is None
    assert [row["time_seconds"] for row in rows] == list(range(2, 16, 2))
    assert all(row["reference"] is None and row["controller"] is None for row in rows)
    assert all(row["delta_db"] == 0 for row in rows)
    row16 = policy.feed(frame(16))[0]
    reference = copy.deepcopy(policy.reference)
    assert row16["reference"] == reference
    assert reference["calibration_interval"] == [0, 16]
    assert set(reference["context_roles"]) == set(CONTEXT)
    assert reference["theta_db"]["AC GUITAR"] == 0.0
    assert reference["ac_range_db"] == 4.0
    feed_range(policy, 17, 120, lambda t, value: ac_step(t, value))
    assert policy.reference == reference


def test_initial_percentile_uses_linear_n_minus_one_interpolation():
    policy = ExperimentalAudioGain()
    # The two largest samples make p90 halfway between -28 and -27: -27.5.
    for t in range(1, 17):
        value = frame(t)
        if t == 15:
            value["levels"]["AC GUITAR"] = -27.0
        elif t == 16:
            value["levels"]["AC GUITAR"] = -26.0
        policy.feed(value)
    assert policy.reference is not None
    assert policy.reference["thresholds_dbfs"]["AC GUITAR"] == pytest.approx(-45.5)


def test_initial_ratio_and_range_use_only_paired_raw_support():
    policy = ExperimentalAudioGain()
    for t in range(1, 17):
        value = frame(t)
        if t in (1, 2):
            value["levels"]["RED"] = -90.0
            value["levels"]["AC GUITAR"] = -25.0  # Excluded from paired statistics.
        policy.feed(value)
    reference = policy.reference
    assert reference is not None
    assert reference["theta_db"]["AC GUITAR"] == 1.0
    assert reference["ac_range_db"] == 4.0
    assert reference["initial_ac_support"] == list(range(3, 17))


def test_unchanged_relationship_and_common_mode_do_not_need_cues_or_move():
    for common in (0.0, 4.0):
        policy = ExperimentalAudioGain()
        rows = feed_range(policy, 1, 120,
                          lambda t, _: frame(t, common=common if 33 <= t <= 96 else 0))
        assert len(rows) == 60
        assert not changes(rows)
        assert policy.offset_db == 0.0


def test_twenty_second_admission_and_three_votes_are_concurrent_not_serial():
    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 96, ac_step)
    # Eight-sample medians are 1 at34, 2 at36, then3/4. q=36; q+24=60.
    assert at(rows, 34)["raw_error_db"] == pytest.approx(1.0)
    assert at(rows, 36)["raw_error_db"] == pytest.approx(2.0)
    assert changes(rows, "cut")[0]["time_seconds"] == 60
    assert [row["time_seconds"] for row in changes(rows)] == [60, 70, 80, 90]
    assert policy.offset_db == -2.0
    assert_bounded(rows)


@pytest.mark.parametrize("duration", [2, 4])
def test_short_unadmitted_accent_never_moves(duration):
    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 120,
                      lambda t, value: ac_step(t, value, 33, 32 + duration))
    assert not changes(rows)


def test_return_release_requires_five_endpoints_then_three_fresh_votes():
    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 160, ac_step)
    # Raw100=2 but controller median([4,4,2])=4 permits a delayed cut100.
    # Window95..102=[5,7,-3,-1,1,3,-3,-1] has median0. Return starts102;
    # five endpoints end110; third eligible vote114 performs first release.
    assert at(rows, 102)["raw_error_db"] == 0.0
    assert [(row["time_seconds"], row["delta_db"]) for row in changes(rows)
            if 96 < row["time_seconds"] < 114] == [(100, -0.5)]
    assert at(rows, 100)["offset_db"] == -2.5
    releases = changes(rows, "release")
    assert [row["time_seconds"] for row in releases] == [114, 124, 134, 144, 154]
    assert [row["delta_db"] for row in releases] == [0.5] * 5
    assert policy.offset_db == 0.0
    assert_bounded(rows)


def test_fully_released_episode_cannot_authorize_a_later_step():
    def signal(t, value):
        return ac_step(t, ac_step(t, value), start=181, stop=240)

    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 240, signal)
    assert at(rows, 180)["offset_db"] == 0.0
    later_cuts = [row for row in changes(rows, "cut") if row["time_seconds"] > 180]
    assert later_cuts and later_cuts[0]["time_seconds"] >= 205
    assert_bounded(rows)


def test_new_departure_during_unfinished_release_needs_fresh_admission():
    def signal(t, value):
        return ac_step(t, ac_step(t, value), start=121, stop=200)

    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 200, signal)
    assert at(rows, 120)["offset_db"] == -2.0
    later_cuts = [row for row in changes(rows, "cut") if row["time_seconds"] > 120]
    assert later_cuts and later_cuts[0]["time_seconds"] >= 145
    assert not [row for row in changes(rows, "release") if row["time_seconds"] > 120]
    assert_bounded(rows)


def test_overcorrection_without_raw_return_holds_instead_of_releasing():
    def signal(t, value):
        return ac_step(t, value, gain=4.0 if t <= 96 else 1.0, stop=160)

    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 160, signal)
    assert at(rows, 96)["offset_db"] == -2.0
    assert at(rows, 100)["delta_db"] == -0.5
    assert all(row["delta_db"] == 0 for row in rows if row["time_seconds"] > 104)
    assert policy.offset_db == -2.5


@pytest.mark.parametrize("fault", ["context", "absence", "stationary", "duplicate",
                                  "stale_audio", "stale_state"])
def test_every_guard_still_blocks_release(fault):
    def signal(t, value):
        ac_step(t, value)
        if t < 105:
            return value
        if fault == "context":
            value["levels"]["KIT"] += 4.0
        elif fault == "absence":
            value["levels"]["AC GUITAR"] = -90.0
        elif fault == "stationary":
            value["levels"]["AC GUITAR"] = -30.0
        elif fault == "duplicate":
            value["levels"]["AC GUITAR"] = value["levels"]["RED"]
        elif fault == "stale_audio":
            value["received_at"] += 0.5
        elif fault == "stale_state":
            value["state_observed_at"] -= 3.0
        return value

    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 160, signal)
    assert at(rows, 96)["offset_db"] == -2.0
    assert at(rows, 100)["delta_db"] == -0.5
    assert not changes(rows, "release")
    assert policy.offset_db == -2.5
    assert_bounded(rows)


def test_one_bad_raw_frame_clears_episode_without_restarting_watermark_each_window():
    def signal(t, value):
        ac_step(t, value, stop=140)
        if t == 85:
            value["levels"]["AC GUITAR"] = math.nan
        return value

    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 140, signal)
    for t in (86, 88, 90, 92, 94):
        assert at(rows, t)["contiguous_seconds"] == t - 85
    assert all(row["delta_db"] == 0 for row in rows if 84 < row["time_seconds"] < 118)
    assert next(row for row in changes(rows, "cut") if row["time_seconds"] > 84)["time_seconds"] == 118


def test_missing_even_frame_retains_scheduled_row_and_recovers_without_recues():
    def signal(t, value):
        return None if t == 34 else ac_step(t, value, stop=120)

    policy = ExperimentalAudioGain()
    rows = feed_range(policy, 1, 120, signal)
    assert [row["time_seconds"] for row in rows] == list(range(2, 121, 2))
    assert at(rows, 34)["eligible"] is False
    assert at(rows, 42)["contiguous_seconds"] == 8
    assert changes(rows, "cut")
    assert changes(rows, "cut")[0]["time_seconds"] >= 66


@pytest.mark.parametrize("key", ["received_at", "state_observed_at", "acquired_at",
                                "alignment_known", "source_ids", "epoch",
                                "state_revision", "routing_revision", "source_map_revision"])
def test_missing_required_evidence_cannot_default_to_fresh_or_known(key):
    policy = ExperimentalAudioGain()
    feed_range(policy, 1, 32)
    bad = frame(33, ac=4.0)
    del bad[key]
    policy.feed(bad)
    rows = feed_range(policy, 34, 40, lambda t, _: frame(t, ac=4.0))
    assert all(row["eligible"] is False and row["delta_db"] == 0 for row in rows)


@pytest.mark.parametrize("key", ["epoch", "source_map_revision", "routing_revision",
                                "state_revision", "source_ids", "alignment_known"])
def test_odd_frame_identity_fault_latches_and_retains_nonzero_offset(key):
    policy = ExperimentalAudioGain()
    feed_range(policy, 1, 80, ac_step)
    retained = policy.offset_db
    assert retained == -1.5
    bad = frame(81, ac=4.0)
    if key == "source_ids":
        bad[key]["AC GUITAR"] = "different-source"
    elif key == "alignment_known":
        bad[key] = False
    else:
        bad[key] = "changed"
    assert policy.feed(bad) == []
    rows = feed_range(policy, 82, 140)  # Original identity and balance cannot silently reset.
    assert rows and all(row["phase"] == "suspended" for row in rows)
    assert all(row["offset_db"] == retained and row["delta_db"] == 0 for row in rows)
    assert policy.events


@pytest.mark.parametrize("kind", ["duplicate", "out_of_order", "receipt_rollback"])
def test_clock_fault_never_rewinds_or_restores_retained_gain(kind):
    policy = ExperimentalAudioGain()
    feed_range(policy, 1, 80, ac_step)
    retained = policy.offset_db
    if kind == "duplicate":
        bad = frame(80, ac=4.0)
    elif kind == "out_of_order":
        bad = frame(79, ac=4.0)
    else:
        bad = frame(81, ac=4.0, received_at=79.0)
    policy.feed(bad)
    rows = feed_range(policy, 82, 120)
    assert retained < 0
    assert all(row["offset_db"] == retained and row["delta_db"] == 0 for row in rows)
    assert all(row["phase"] == "suspended" for row in rows)


@pytest.mark.parametrize("fault", ["epoch", "receipt_rollback"])
def test_future_fault_is_not_backdated_over_filled_missing_slots(fault):
    policy = ExperimentalAudioGain()
    feed_range(policy, 1, 80, ac_step)
    retained = policy.offset_db
    assert retained < 0
    incoming = frame(100)
    if fault == "epoch":
        incoming["epoch"] = "changed-at100"
    else:
        incoming["received_at"] = 79.0
    rows = policy.feed(incoming)
    without_future_fault = ExperimentalAudioGain()
    feed_range(without_future_fault, 1, 80, ac_step)
    no_fault_rows = without_future_fault.feed(frame(100))
    assert [row["time_seconds"] for row in rows] == list(range(82, 101, 2))
    assert rows[:-1] == no_fault_rows[:-1]
    assert all(row["phase"] != "suspended" for row in rows[:-1])
    assert all(row["eligible"] is False for row in rows)
    assert rows[-1]["phase"] == "suspended"
    assert all(row["offset_db"] == retained and row["delta_db"] == 0 for row in rows)


def test_invalid_initial_reference_does_not_search_later_for_a_better_one():
    policy = ExperimentalAudioGain()
    for t in range(1, 17):
        value = frame(t)
        value["levels"]["AC GUITAR"] = -30.0
        policy.feed(value)
    rows = feed_range(policy, 17, 120, lambda t, _: frame(t, ac=4.0))
    assert policy.reference is None
    assert all(row["phase"] == "calibration_rejected" for row in rows)
    assert not changes(rows)


def test_at_least_two_initial_contexts_and_fixed_roster_cannot_disappear():
    policy = ExperimentalAudioGain()
    for t in range(1, 17):
        value = frame(t)
        for role in ("BASS", "KEYS", "EGUITAR"):
            value["levels"][role] = -90.0
        policy.feed(value)
    assert policy.reference is None

    policy = ExperimentalAudioGain()
    feed_range(policy, 1, 16)
    reference = copy.deepcopy(policy.reference)
    def signal(t, value):
        ac_step(t, value)
        if t >= 33:
            value["levels"]["KEYS"] = -90.0
        return value
    rows = feed_range(policy, 17, 120, signal)
    assert policy.reference == reference
    assert not changes(rows)
    assert all(not row["eligible"] for row in rows if row["time_seconds"] >= 40)


def test_received_frames_reference_and_prior_rows_do_not_alias_policy_state():
    first = ExperimentalAudioGain()
    second = ExperimentalAudioGain()
    for t in range(1, 17):
        value = frame(t)
        first.feed(value)
        second.feed(copy.deepcopy(value))
        value["levels"]["AC GUITAR"] = 1000.0
        value["source_ids"]["AC GUITAR"] = "mutated"
    exposed = first.reference
    exposed["theta_db"]["AC GUITAR"] = 999.0
    exposed["context_roles"].clear()
    rows1 = feed_range(first, 17, 80, ac_step)
    rows2 = feed_range(second, 17, 80, ac_step)
    assert rows1 == rows2
    rows1[-1]["reference"]["theta_db"]["AC GUITAR"] = 999.0
    assert first.reference == second.reference


def test_all_calibration_and_decision_prefixes_equal_full_replay():
    frames = [ac_step(t, frame(t)) for t in range(1, 81)]
    complete = ExperimentalAudioGain()
    expected = [row for value in frames for row in complete.feed(copy.deepcopy(value))]
    for cutoff in (2, 8, 14, 16, 32, 36, 56, 58, 60, 80):
        prefix = ExperimentalAudioGain()
        actual = [row for value in frames[:cutoff] for row in prefix.feed(copy.deepcopy(value))]
        assert actual == [row for row in expected if row["time_seconds"] <= cutoff]


def test_module_has_no_audio_host_network_process_or_gate_capabilities():
    source = Path(__file__).resolve().parents[1] / "src/wcfi_tools/audio/experimental_audio_gain.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = ("socket", "subprocess", "soundfile", "sounddevice", "torch", "numpy",
                 "ctypes", "requests", "http", "wcfi_tools.ableton", "analysis_nodes",
                 "intent_hold_gate", "gain_target_policy", "gain_observations", "gain_replay")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [name.name for name in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]
        else:
            imports = []
        assert not any(part in name for part in forbidden for name in imports)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "__import__"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"rearm", "reset", "start", "play", "connect", "capture"}
