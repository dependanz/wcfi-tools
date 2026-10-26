"""Independent scalar v2 contracts; no host, audio or former-suite execution.

Fixtures are hand-authored envelopes. Behavioral expectations are prospective,
not snapshots copied from implementation output or musical-quality labels.
"""
from __future__ import annotations

import ast
import copy
import math
from pathlib import Path

import pytest

from wcfi_tools.audio.experimental_audio_gain_v2 import ExperimentalAudioGainV2


ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")
CONTEXT = {"KIT": -4.0, "BASS": -2.0, "KEYS": -6.0, "EGUITAR": -5.0}


def frame(t, ac=0.0, common=0.0, cycle=(-2.0, -2.0, 2.0, 2.0), rotation=0,
          **changes):
    source_index = t - 1 + rotation
    red = -30.0 + (1.0 if source_index % 2 == 0 else -1.0)
    acoustic = -30.0 + cycle[source_index % len(cycle)]
    levels = {"RED": red + common, "AC GUITAR": acoustic + ac + common}
    levels.update({role: red + offset + common for role, offset in CONTEXT.items()})
    value = {
        "time_seconds": t, "sequence": t,
        "acquired_at": float(t), "received_at": float(t),
        "state_observed_at": float(t),
        "source_ids": {role: "source:" + role for role in ROLES},
        "source_map_revision": "mapping:1", "routing_revision": "routing:1",
        "state_revision": "state:1", "epoch": "epoch:1",
        "alignment_known": True, "levels": levels,
    }
    value.update(changes)
    return value


def collect(policy, start=1, stop=120, make=None):
    rows = []
    for t in range(start, stop + 1):
        value = make(t) if make else frame(t)
        if value is not None:
            rows.extend(policy.feed(value))
    return rows


def step(t, return_at=97, **kwargs):
    return frame(t, ac=4.0 if 33 <= t < return_at else 0.0, **kwargs)


def row_at(rows, t):
    return next(row for row in rows if row["time_seconds"] == t)


def moves(rows, direction=None):
    result = [row for row in rows if row["delta_db"] != 0]
    if direction == "cut":
        result = [row for row in result if row["delta_db"] < 0]
    elif direction == "release":
        result = [row for row in result if row["delta_db"] > 0]
    return result


def assert_exact_mutation_reporting(rows):
    for row in rows:
        controller = row["controller"]
        assert -3 <= row["offset_db"] <= 0
        assert abs(row["delta_db"]) in (0.0, 0.5)
        assert row["effective_gain"] == 10 ** (row["offset_db"] / 20)
        if controller is not None:
            assert row["delta_db"] == controller["delta_db"]
            assert row["offset_db"] == controller["offset_db"]
            assert row["effective_gain"] == controller["effective_gain"]
        if row["delta_db"]:
            assert row["eligible"] is True
        assert row["musical_approval_valid"] is False
        assert row["live_eligible"] is False
    changed = moves(rows)
    assert all(b["time_seconds"] - a["time_seconds"] >= 10
               for a, b in zip(changed, changed[1:]))


def test_reference_and_new_statistics_are_unavailable_before_frame16():
    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=15)
    assert policy.reference is None
    assert [r["time_seconds"] for r in rows] == list(range(2, 16, 2))
    assert all(r["reference"] is None and r["controller"] is None for r in rows)
    assert all(r["delta_db"] == 0 for r in rows)
    row16 = policy.feed(frame(16))[0]
    assert row16["reference"] == policy.reference
    assert policy.reference["calibration_interval"] == [0, 16]


def test_reference_is_immutable_across_departure_return_and_copy_mutation():
    first = ExperimentalAudioGainV2()
    second = ExperimentalAudioGainV2()
    for t in range(1, 17):
        value = frame(t)
        first.feed(value)
        second.feed(copy.deepcopy(value))
        value["levels"]["AC GUITAR"] = 1000.0
        value["source_ids"]["AC GUITAR"] = "mutated"
    frozen = copy.deepcopy(first.reference)
    exposed = first.reference
    exposed["theta_db"]["AC GUITAR"] = 999.0
    exposed["context_roles"].clear()
    rows1 = collect(first, 17, 160, step)
    rows2 = collect(second, 17, 160, step)
    assert rows1 == rows2
    assert first.reference == frozen == second.reference
    rows1[-1]["reference"]["theta_db"]["AC GUITAR"] = 999.0
    assert first.reference == frozen


def test_every_returned_move_is_the_actual_synchronous_controller_mutation():
    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=180, make=step)
    assert len(rows) == 90
    assert moves(rows, "cut") and moves(rows, "release")
    assert_exact_mutation_reporting(rows)


@pytest.mark.parametrize("fault", ["context", "absence", "stationary", "duplicate",
                                  "stale_audio", "stale_state", "missing_level"])
def test_existing_guards_apply_through_return_release(fault):
    def signal(t):
        value = step(t)
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
        else:
            del value["levels"]["KEYS"]
        return value

    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=160, make=signal)
    retained = row_at(rows, 96)["offset_db"]
    assert retained < 0
    assert not moves(rows, "release")
    assert policy.offset_db == retained
    assert_exact_mutation_reporting(rows)


@pytest.mark.parametrize("fault", ["context", "absence", "stationary", "duplicate",
                                  "stale_audio", "stale_state", "missing_level", "volatile"])
def test_existing_guards_also_end_an_admitted_attenuation_episode(fault):
    def signal(t):
        value = frame(t, ac=4.0 if t >= 33 else 0.0)
        if t < 81:
            return value
        if fault == "context":
            value["levels"]["KIT"] += 4.0
        elif fault == "absence":
            value["levels"]["AC GUITAR"] = -90.0
        elif fault == "stationary":
            value["levels"]["AC GUITAR"] = -26.0
        elif fault == "duplicate":
            value["levels"]["AC GUITAR"] = value["levels"]["RED"] + 4.0
        elif fault == "stale_audio":
            value["received_at"] += 0.5
        elif fault == "stale_state":
            value["state_observed_at"] -= 3.0
        elif fault == "missing_level":
            del value["levels"]["KEYS"]
        else:
            value["levels"]["AC GUITAR"] = 10.0 if t % 2 else -60.0
        return value

    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=120, make=signal)
    retained = row_at(rows, 80)["offset_db"]
    assert retained < 0
    assert not [r for r in moves(rows) if r["time_seconds"] > 80]
    assert policy.offset_db == retained
    assert_exact_mutation_reporting(rows)


@pytest.mark.parametrize("field", ["epoch", "source_map_revision", "routing_revision",
                                  "state_revision", "source_ids", "alignment_known"])
def test_identity_fault_at_odd_endpoint_retains_negative_offset_and_latches(field):
    policy = ExperimentalAudioGainV2()
    collect(policy, stop=80, make=step)
    retained = policy.offset_db
    assert retained < 0
    bad = step(81)
    if field == "source_ids":
        bad[field]["AC GUITAR"] = "different-source"
    elif field == "alignment_known":
        bad[field] = False
    else:
        bad[field] = "changed"
    assert policy.feed(bad) == []
    rows = collect(policy, 82, 160)
    assert rows and all(r["phase"] == "suspended" for r in rows)
    assert all(r["offset_db"] == retained and r["delta_db"] == 0 for r in rows)
    assert policy.events


@pytest.mark.parametrize("fault", ["duplicate", "out_of_order", "receipt_rollback"])
def test_clock_faults_cannot_rewind_or_restore_attenuated_gain(fault):
    policy = ExperimentalAudioGainV2()
    collect(policy, stop=80, make=step)
    retained = policy.offset_db
    assert retained < 0
    bad = step(80 if fault == "duplicate" else 79 if fault == "out_of_order" else 81)
    if fault == "receipt_rollback":
        bad["received_at"] = 79.0
    policy.feed(bad)
    rows = collect(policy, 82, 140)
    assert all(r["phase"] == "suspended" for r in rows)
    assert all(r["offset_db"] == retained and r["delta_db"] == 0 for r in rows)


@pytest.mark.parametrize("fault", ["epoch", "receipt_rollback"])
def test_sparse_gap_rows_do_not_anticipate_later_received_fault(fault):
    policy, control = ExperimentalAudioGainV2(), ExperimentalAudioGainV2()
    collect(policy, stop=80, make=step)
    collect(control, stop=80, make=step)
    retained = policy.offset_db
    assert retained < 0
    incoming = frame(100)
    if fault == "epoch":
        incoming["epoch"] = "changed"
    else:
        incoming["received_at"] = 79.0
    rows = policy.feed(incoming)
    expected_prefix = control.feed(frame(100))
    assert [r["time_seconds"] for r in rows] == list(range(82, 101, 2))
    assert rows[:-1] == expected_prefix[:-1]
    assert all(r["phase"] != "suspended" for r in rows[:-1])
    assert rows[-1]["phase"] == "suspended"
    assert all(r["offset_db"] == retained and r["delta_db"] == 0 for r in rows)


@pytest.mark.parametrize("key", ["received_at", "state_observed_at", "acquired_at",
                                "alignment_known", "source_ids", "epoch",
                                "state_revision", "routing_revision", "source_map_revision"])
def test_missing_evidence_cannot_be_fabricated_as_valid(key):
    policy = ExperimentalAudioGainV2()
    collect(policy, stop=32)
    bad = step(33)
    del bad[key]
    policy.feed(bad)
    rows = collect(policy, 34, 40, step)
    assert all(r["eligible"] is False and r["delta_db"] == 0 for r in rows)


def test_bad_raw_frame_watermark_tracks_actual_frame_not_overlapping_windows():
    def signal(t):
        value = frame(t, ac=4.0 if t >= 33 else 0.0)
        if t == 85:
            value["levels"]["AC GUITAR"] = math.nan
        return value

    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=140, make=signal)
    for t in (86, 88, 90, 92, 94):
        assert row_at(rows, t)["contiguous_seconds"] == t - 85
    assert not [r for r in moves(rows) if 84 < r["time_seconds"] < 118]
    assert any(r["delta_db"] < 0 for r in rows if r["time_seconds"] >= 118)


def test_initial_rejection_does_not_search_for_a_favorable_later_reference():
    policy = ExperimentalAudioGainV2()
    for t in range(1, 17):
        value = frame(t)
        value["levels"]["AC GUITAR"] = -30.0
        policy.feed(value)
    rows = collect(policy, 17, 120, lambda t: frame(t, ac=4.0))
    assert policy.reference is None
    assert all(r["phase"] == "calibration_rejected" for r in rows)
    assert not moves(rows)


def test_calibration_and_decision_prefixes_equal_complete_history():
    frames = [step(t) for t in range(1, 121)]
    complete = ExperimentalAudioGainV2()
    expected = [r for value in frames for r in complete.feed(copy.deepcopy(value))]
    for cutoff in (2, 6, 8, 10, 12, 14, 16, 32, 36, 56, 58, 60, 96, 98, 100, 120):
        prefix = ExperimentalAudioGainV2()
        rows = [r for value in frames[:cutoff] for r in prefix.feed(copy.deepcopy(value))]
        assert rows == [r for r in expected if r["time_seconds"] <= cutoff]


def official_frame(t, variant="step", rotation=0, cycle=(-2., -2., 2., 2.),
                   return_at=97):
    """Literal independently frozen public rhythm, distinct from common fixture."""
    index = t - 1 + rotation
    red = -24.0 + (-1.0 if index % 2 == 0 else 1.0)
    ac = -28.0 + cycle[index % len(cycle)]
    onset = 49 if variant == "absence" else 38 if variant == "uncertain" else 33
    if variant != "unchanged" and onset <= t < return_at:
        ac += 4.0
    if variant == "absence" and 33 <= t <= 48:
        ac = -90.0
    value = frame(t)
    value["levels"] = {"RED": red, "AC GUITAR": ac, "KIT": red + 2.0,
                       "BASS": red - 2.0, "KEYS": red - 6.0, "EGUITAR": red - 8.0}
    if variant == "uncertain":
        if t == 33:
            return None
        if t == 35:
            value["levels"]["AC GUITAR"] = math.nan
        elif t == 37:
            value["received_at"] += 0.75
    return value


@pytest.mark.parametrize("cycle,expected", [
    ((-2., -2., 2., 2.), [0., 0., 0., 0., 0., 0.]),
    ((-2., -2., 0., 2., 2., 0.), [1., -2., 1., 1., -2., 1.]),
    ((-2., -2., -2., -2., 2., 2., 2., 2.), [2., 2., -2., -2., 2., 2.]),
])
def test_recent_calibration_statistic_is_exact_causal_and_frozen(cycle, expected):
    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=15,
                   make=lambda t: official_frame(t, variant="unchanged", cycle=cycle))
    assert policy.reference is None
    assert all(r["reference"] is None for r in rows)
    policy.feed(official_frame(16, variant="unchanged", cycle=cycle))
    reference = copy.deepcopy(policy.reference)
    assert reference["recent_calibration_supported"] is True
    assert reference["recent_calibration_deltas_db"] == expected
    assert reference["minimum_recent_change_db"] == min([0., *expected])
    collect(policy, 17, 120, lambda t: official_frame(t, cycle=cycle))
    assert policy.reference == reference


@pytest.mark.parametrize("variant,cut_times", [
    ("step", [60, 70, 80, 90]),
    ("absence", [78, 88]),
    ("uncertain", [70, 80, 90]),
])
def test_known_fault_removal_has_useful_correction_and_no_delayed_cut(variant, cut_times):
    policy = ExperimentalAudioGainV2()
    rows = collect(policy, make=lambda t: official_frame(t, variant=variant))
    assert [r["time_seconds"] for r in moves(rows, "cut")] == cut_times
    assert row_at(rows, 96)["offset_db"] <= -1.0
    assert not [r for r in moves(rows, "cut") if r["time_seconds"] >= 97]
    assert moves(rows, "release")[0]["time_seconds"] == 114
    recent = row_at(rows, 98)["evidence"]["recent"]
    assert recent["recent_error_db"] == 2.0
    assert recent["previous_recent_error_db"] == 4.0
    assert recent["recent_change_db"] == -2.0
    assert recent["reference_minimum_recent_change_db"] == 0.0
    assert recent["recent_veto_reason"] is not None
    assert row_at(rows, 98)["eligible"] is False
    assert row_at(rows, 98)["controller"]["raw_history_count"] == 0
    assert_exact_mutation_reporting(rows)


@pytest.mark.parametrize("rotation", [1, 2, 3])
def test_rotated_absence_recovery_does_not_alias_a_recent_return(rotation):
    policy = ExperimentalAudioGainV2()
    rows = collect(policy, make=lambda t: official_frame(t, variant="absence", rotation=rotation))
    assert [r["time_seconds"] for r in moves(rows, "cut")] == [78, 88]
    assert row_at(rows, 96)["offset_db"] == -1.0
    assert not [r for r in moves(rows, "cut") if r["time_seconds"] >= 97]
    if rotation == 1:
        # Former e4-e8 check saw3-3=0 and missed this known counterexample.
        recent = row_at(rows, 98)["evidence"]["recent"]
        assert recent["recent_error_db"] == 3.0
        assert recent["previous_recent_error_db"] == 4.0
        assert recent["recent_change_db"] == -1.0
        assert row_at(rows, 98)["raw_error_db"] == 3.0
        assert row_at(rows, 98)["eligible"] is False
    assert_exact_mutation_reporting(rows)


def test_return_shifted_two_seconds_is_not_a_fitted_removal_timestamp():
    policy = ExperimentalAudioGainV2()
    rows = collect(policy, make=lambda t: official_frame(t, return_at=99))
    assert [r["time_seconds"] for r in moves(rows, "cut")] == [60, 70, 80, 90]
    assert not [r for r in moves(rows, "cut") if r["time_seconds"] >= 99]
    assert [r["time_seconds"] for r in moves(rows, "release")] == [118]
    assert_exact_mutation_reporting(rows)


@pytest.mark.parametrize("cycle,cut_times,release_times", [
    ((-2., -2., 0., 2., 2., 0.), [60, 70, 80, 90], []),
    # Old high29..32 equals new low33..36: range0 veto36; q38 -> cuts62/72.
    ((-2., -2., -2., -2., 2., 2., 2., 2.), [62, 72], [114]),
])
def test_longer_rhythms_are_not_permanent_hold_or_treated_as_gain_faults(cycle, cut_times, release_times):
    quiet, raised = ExperimentalAudioGainV2(), ExperimentalAudioGainV2()
    quiet_rows = collect(quiet, make=lambda t: official_frame(t, variant="unchanged", cycle=cycle))
    raised_rows = collect(raised, make=lambda t: official_frame(t, cycle=cycle))
    assert not moves(quiet_rows)
    assert [r["time_seconds"] for r in moves(raised_rows, "cut")] == cut_times
    assert [r["time_seconds"] for r in moves(raised_rows, "release")] == release_times
    assert row_at(raised_rows, 96)["offset_db"] <= -1.0
    assert not [r for r in moves(raised_rows, "cut") if r["time_seconds"] >= 97]
    if len(cycle) == 8:
        # Previous-window evidence must exist even when its controller call was vetoed.
        assert row_at(raised_rows, 76)["eligible"] is False
        recent78 = row_at(raised_rows, 78)["evidence"]["recent"]
        assert recent78["previous_recent_error_db"] == 2.0
        assert recent78["recent_error_db"] == 4.0
        assert recent78["recent_change_db"] == 2.0
    assert_exact_mutation_reporting(raised_rows)


def test_incomplete_recent_calibration_cannot_choose_a_supported_subset_or_fallback():
    policy = ExperimentalAudioGainV2()
    for t in range(1, 17):
        value = frame(t)
        if t == 1:
            value["levels"]["RED"] = -90.0
        policy.feed(value)
    # v1 still has15 shared frames: recent-difference qualification is stricter.
    assert policy.reference is not None
    assert policy.reference["recent_calibration_supported"] is False
    assert policy.reference["minimum_recent_change_db"] is None
    assert policy.reference["recent_calibration_deltas_db"] is None
    rows = collect(policy, 17, 160, lambda t: frame(t, ac=4.0))
    assert not moves(rows)
    assert policy.offset_db == 0.0
    assert policy.reference["minimum_recent_change_db"] is None


def test_recent_pair_support_veto_is_stricter_without_changing_activity_mask():
    def signal(t):
        value = frame(t, ac=4.0 if t >= 33 else 0.0)
        if t == 57:
            value["levels"]["AC GUITAR"] = -90.0
        return value

    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=120, make=signal)
    row58 = row_at(rows, 58)
    # Seven of eight paired frames still satisfy v1's >=6 support guard.
    assert len(row58["evidence"]["pair_support"]["AC GUITAR"]) == 7
    assert row58["evidence"]["recent"]["recent_veto_reason"] is not None
    assert row58["eligible"] is False
    assert row58["controller"]["raw_history_count"] == 0
    assert not [r for r in moves(rows, "cut") if r["time_seconds"] < 88]
    assert moves(rows, "cut")


def test_new_departure_during_v2_release_requires_fresh_twenty_second_admission():
    def signal(t):
        value = official_frame(t)
        if 121 <= t <= 180:
            value["levels"]["AC GUITAR"] += 4.0
        return value

    policy = ExperimentalAudioGainV2()
    rows = collect(policy, stop=200, make=signal)
    assert row_at(rows, 120)["offset_db"] < 0
    later = [r for r in moves(rows, "cut") if r["time_seconds"] >= 121]
    assert [r["time_seconds"] for r in later] == [148, 158, 168]
    assert not [r for r in moves(rows, "cut") if r["time_seconds"] >= 181]
    assert_exact_mutation_reporting(rows)


def test_v2_module_has_no_io_host_gate_or_automatic_rearm_capability():
    source = Path(__file__).resolve().parents[1] / "src/wcfi_tools/audio/experimental_audio_gain_v2.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = ("socket", "subprocess", "soundfile", "sounddevice", "torch", "numpy",
                 "ctypes", "requests", "http", "wcfi_tools.ableton", "analysis_nodes",
                 "intent_hold_gate", "gain_target_policy", "gain_observations", "gain_replay")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [entry.name for entry in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]
        else:
            imports = []
        assert not any(part in name for part in forbidden for name in imports)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "__import__"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"rearm", "reset", "start", "play", "connect", "capture"}
