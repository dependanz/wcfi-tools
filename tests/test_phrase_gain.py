"""Hand-authored scalar contracts for the offline phrase-gain candidate."""
from dataclasses import FrozenInstanceError
import json
import math

import pytest

from wcfi_tools.audio.phrase_gain import PhraseGain, PhraseSettings


def run(levels, settings=PhraseSettings()):
    policy = PhraseGain(settings)
    return policy, [policy.observe(value, bin_index=i) for i, value in enumerate(levels)]


def test_frozen_default_settings_match_reviewed_candidate():
    settings = PhraseSettings()
    assert (settings.bin_seconds, settings.history_bins, settings.min_active_bins,
            settings.expire_inactive_bins) == (.1, 20, 5, 10)
    assert (settings.trim_db, settings.target_dbfs, settings.max_boost_db,
            settings.max_cut_db, settings.activity_dbfs, settings.slew_db_per_second,
            settings.deadband_db) == (18., -26., 6., 6., -55., 1., 1.)
    with pytest.raises(FrozenInstanceError):
        settings.trim_db = 0


@pytest.mark.parametrize("change", [
    {"trim_db": 25}, {"trim_db": True}, {"target_dbfs": math.nan},
    {"max_boost_db": -1}, {"max_cut_db": math.inf}, {"activity_dbfs": -91},
    {"bin_seconds": 0}, {"slew_db_per_second": 0}, {"deadband_db": -1},
    {"history_bins": True}, {"history_bins": 0}, {"min_active_bins": 21},
    {"expire_inactive_bins": 1.5},
])
def test_invalid_configuration_rejected(change):
    with pytest.raises(ValueError):
        PhraseSettings(**change)


def test_five_completed_active_bins_are_required_and_slew_is_exactly_bounded():
    policy, rows = run([-50.] * 10)
    assert [row["reason"] for row in rows[:4]] == ["hold_insufficient_support"] * 4
    assert rows[4]["end_seconds"] == .5
    assert rows[4]["estimate_dbfs"] == -50
    assert rows[4]["desired_offset_db"] == 6
    assert rows[4]["before_offset_db"] == 0
    assert rows[4]["after_offset_db"] == pytest.approx(.1)
    assert policy.offset_db == pytest.approx(.6)
    assert all(0 <= row["delta_db"] <= .1 + 1e-12 for row in rows)
    assert rows[4]["next_total_gain_db"] == pytest.approx(18.1)
    assert [row["after_offset_db"] for row in rows] == pytest.approx(
        [0, 0, 0, 0, .1, .2, .3, .4, .5, .6])


@pytest.mark.parametrize("level,expected", [(-50., 5.), (-38., -5.)])
def test_sustained_errors_converge_to_symmetric_deadband(level, expected):
    policy, rows = run([level] * 120)
    assert policy.offset_db == pytest.approx(expected)
    assert rows[-1]["reason"] == "hold_deadband"
    assert all(abs(row["delta_db"]) <= .1 + 1e-12 for row in rows)
    assert all(-6 <= row["after_offset_db"] <= 6 for row in rows)


def test_current_evidence_vetoes_a_boost_on_a_single_loud_accent():
    _, rows = run([-50.] * 10 + [-30.] + [-50.])
    accent = rows[10]
    assert accent["estimate_dbfs"] == -50
    assert accent["desired_offset_db"] == 6
    assert accent["reason"] == "hold_recent_disagreement"
    assert accent["delta_db"] == 0
    assert rows[11]["reason"] == "adjust_boost"


def test_new_quiet_bin_prevents_late_cut_from_old_phrase_evidence():
    _, rows = run([-38.] * 10 + [-50.])
    quiet = rows[-1]
    assert quiet["estimate_dbfs"] == -38
    assert quiet["desired_offset_db"] == -6
    assert quiet["reason"] == "hold_recent_disagreement"
    assert quiet["before_offset_db"] == quiet["after_offset_db"]


def test_independently_derived_accent_and_recent_drop_sequences():
    _, rows = run([-50.] * 4 + [-38., -50.])
    assert [row["after_offset_db"] for row in rows] == pytest.approx([0, 0, 0, 0, 0, .1])
    _, rows = run([-38.] * 10 + [-50.] * 11)
    assert [row["after_offset_db"] for row in rows[:10]] == pytest.approx(
        [0, 0, 0, 0, -.1, -.2, -.3, -.4, -.5, -.6])
    assert all(row["reason"] == "hold_recent_disagreement" for row in rows[10:19])
    assert rows[19]["estimate_dbfs"] == -44.
    assert rows[19]["reason"] == "hold_deadband"
    assert [row["after_offset_db"] for row in rows[10:20]] == pytest.approx([-.6] * 10)
    assert rows[20]["after_offset_db"] == pytest.approx(-.5)
    _, rows = run([-50.] * 10 + [-38.] * 2 + [-50.])
    assert [row["after_offset_db"] for row in rows[10:]] == pytest.approx([.6, .6, .7])


@pytest.mark.parametrize("level,expected", [(-45., 0), (-43., 0), (-45.1, .1)])
def test_exact_deadband_boundaries_do_not_accumulate_float_drift(level, expected):
    policy, rows = run([level] * 20)
    assert policy.offset_db == pytest.approx(expected)
    assert all(row["reason"] == "hold_deadband" for row in rows[5:])


def test_at_target_rhythm_and_isolated_accent_do_not_generate_correction():
    policy, rows = run([-45., -43.] * 20 + [-30.] + [-45., -43.] * 10)
    assert policy.offset_db == 0
    assert all(row["delta_db"] == 0 for row in rows)


def test_noise_and_digital_silence_never_create_support_or_increase_gain():
    policy, rows = run([-56., None, -90., -160.] * 30)
    assert policy.offset_db == 0
    assert all(not row["active"] and row["support_bins"] == 0 for row in rows)
    assert all(row["delta_db"] == 0 for row in rows)


def test_threshold_is_inclusive_and_does_not_use_a_shorter_measurement_window():
    _, rows = run([-55.] * 5)
    assert rows[-1]["active"] is True
    assert rows[-1]["reason"] == "adjust_boost"
    assert rows[-1]["support_bins"] == 5


def test_pause_holds_offset_expires_history_and_requires_five_fresh_bins():
    policy, rows = run([-50.] * 10 + [None] * 10 + [-50.] * 5)
    held = rows[9]["after_offset_db"]
    assert all(row["after_offset_db"] == held for row in rows[10:24])
    assert rows[18]["history_bins"] == 19
    assert rows[19]["reason"] == "hold_history_expired"
    assert rows[19]["history_bins"] == 0
    assert [row["support_bins"] for row in rows[20:25]] == [1, 2, 3, 4, 5]
    assert rows[24]["reason"] == "adjust_boost"
    assert policy.offset_db == pytest.approx(held + .1)


def test_support_is_limited_to_the_trailing_twenty_bins():
    # No ten-bin inactive run, but old activity eventually leaves the window.
    _, rows = run([-50.] * 5 + [None] * 9 + [-50.] + [None] * 9 + [-50.])
    assert rows[-1]["history_bins"] == 20
    assert rows[-1]["support_bins"] == 2
    assert rows[-1]["reason"] == "hold_insufficient_support"


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_nonfinite_measurement_clears_evidence_holds_gain_and_is_json_safe(invalid):
    policy, rows = run([-50.] * 10)
    held = policy.offset_db
    row = policy.observe(invalid, bin_index=10)
    assert row["reason"] == "invalid_nonfinite"
    assert row["valid"] is False and row["raw_rms_dbfs"] is None
    assert row["history_bins"] == 0 and row["after_offset_db"] == held
    json.dumps(row, allow_nan=False)
    fresh = [policy.observe(-50., bin_index=i) for i in range(11, 16)]
    assert all(row["delta_db"] == 0 for row in fresh[:4])
    assert fresh[-1]["delta_db"] == pytest.approx(.1)


def test_forward_gap_is_discarded_and_duplicate_does_not_rewind_clock():
    policy, _ = run([-50.] * 10)
    held = policy.offset_db
    gap = policy.observe(-50., bin_index=15)
    assert gap["reason"] == "invalid_sequence" and gap["support_bins"] == 0
    assert gap["after_offset_db"] == held
    duplicate = policy.observe(-50., bin_index=15)
    assert duplicate["reason"] == "invalid_sequence"
    backwards = policy.observe(-50., bin_index=12)
    assert backwards["reason"] == "invalid_sequence"
    fresh = [policy.observe(-50., bin_index=i) for i in range(16, 21)]
    assert fresh[-1]["support_bins"] == 5
    assert fresh[-1]["delta_db"] == pytest.approx(.1)


@pytest.mark.parametrize("raw,index,kind", [
    ("-50", 10, TypeError), (True, 10, TypeError), ([], 10, TypeError),
    (-50., True, TypeError), (-50., 10.0, TypeError), (-50., -1, ValueError),
    (-50., 10 ** 1000, ValueError),
])
def test_invalid_types_or_indexes_raise_without_mutating_state(raw, index, kind):
    policy, _ = run([-50.] * 10)
    with pytest.raises(kind):
        policy.observe(raw, bin_index=index)
    next_row = policy.observe(-50., bin_index=10)
    assert next_row["valid"] is True and next_row["support_bins"] == 11


def test_combined_gain_limit_cannot_be_exceeded_by_nonzero_trim():
    policy, rows = run([-70.] * 100,
                      PhraseSettings(trim_db=24., activity_dbfs=-80.))
    assert policy.offset_db == 0
    assert all(row["next_total_gain_db"] == 24. for row in rows)
    policy, rows = run([-24.] * 100, PhraseSettings(trim_db=-24.))
    assert all(-24. <= row["next_total_gain_db"] <= 24. for row in rows)


def test_returned_rows_cannot_mutate_policy_and_prefixes_are_causal():
    levels = [-50.] * 14 + [-30.] + [None] * 10 + [-38.] * 24
    policy, complete = run(levels)
    for length in (1, 4, 5, 14, 15, 20, 25, 31, len(levels)):
        _, prefix = run(levels[:length])
        assert prefix == complete[:length]
        _, changed_future = run(levels[:length] + [-25.] * 5)
        assert changed_future[:length] == prefix
    before = policy.offset_db
    complete[-1]["after_offset_db"] = 999
    assert policy.offset_db == before


def test_finite_extreme_levels_do_not_create_nonfinite_decisions():
    _, rows = run([1e308] * 8)
    assert all(row["reason"] in {"hold_insufficient_support", "adjust_cut"} for row in rows)
    json.dumps(rows, allow_nan=False)
