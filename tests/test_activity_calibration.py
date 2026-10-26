"""Causal evidence, expiry and immutability contracts for fixed-gain calibration."""
from dataclasses import FrozenInstanceError
import json
import math

import pytest

from wcfi_tools.audio.activity_calibration import ActivityCalibration, CalibrationSettings


def run(levels, settings=CalibrationSettings()):
    calibrator = ActivityCalibration(settings)
    return calibrator, [calibrator.observe(level, bin_index=i) for i, level in enumerate(levels)]


def test_default_contract_and_immutable_settings():
    settings = CalibrationSettings()
    assert (settings.bin_seconds, settings.window_bins, settings.required_active_bins,
            settings.activity_dbfs, settings.target_dbfs,
            settings.max_boost_db, settings.max_cut_db) == (.1, 300, 50, -55., -26., 6., 6.)
    with pytest.raises(FrozenInstanceError):
        settings.target_dbfs = -24
    with pytest.raises(TypeError):
        ActivityCalibration({})


def test_long_initial_quiet_does_not_calibrate_then_five_active_seconds_do():
    calibrator, rows = run([None, -80.] * 400 + [-30.] * 50)
    assert all(not row["ready"] and row["gain_db"] is None for row in rows[:-1])
    assert max(row["window_bins"] for row in rows) == 300
    assert rows[-1]["newly_ready"]
    assert rows[-1]["support_bins"] == 50
    assert rows[-1]["median_dbfs"] == -30
    assert rows[-1]["end_seconds"] == rows[-1]["ready_end_seconds"] == 85.
    assert calibrator.ready and calibrator.ready_bin_index == 849 and calibrator.gain_db == 4.


def test_support_accumulates_across_phrase_pauses_within_window():
    _, rows = run([-30.] * 25 + [None] * 100 + [-28.] * 25)
    assert not any(row["ready"] for row in rows[:-1])
    assert rows[-1]["ready"] and rows[-1]["ready_bin_index"] == 149
    assert rows[-1]["median_dbfs"] == -29 and rows[-1]["gain_db"] == 3


def test_old_evidence_expires_on_exact_window_boundary():
    settings = CalibrationSettings(window_bins=5, required_active_bins=3)
    calibrator, rows = run([-30., -30., None, None, None, -20., -20., -20.], settings)
    assert [row["support_bins"] for row in rows] == [1, 2, 2, 2, 2, 2, 2, 3]
    assert not rows[6]["ready"]
    assert calibrator.ready_bin_index == 7 and rows[-1]["median_dbfs"] == -20
    assert calibrator.gain_db == -6


@pytest.mark.parametrize("levels,median,gain", [
    ([-55.] * 50, -55., 6.),
    ([-15.] * 50, -15., -6.),
    ([-29.] * 25 + [-27.] * 25, -28., 2.),
    ([-26.] * 50, -26., 0.),
])
def test_threshold_inclusive_median_and_clamping(levels, median, gain):
    calibrator, rows = run([-55.0001] * 50 + levels)
    assert not any(row["active"] for row in rows[:50])
    assert rows[-1]["support_bins"] == 50 and rows[-1]["median_dbfs"] == median
    assert calibrator.ready_bin_index == 99 and calibrator.gain_db == gain


def test_odd_median_and_asymmetric_bounds():
    settings = CalibrationSettings(required_active_bins=3, max_boost_db=2., max_cut_db=4.)
    assert run([-30., -27., -29.], settings)[0].gain_db == 2.
    assert run([-20., -21., -19.], settings)[0].gain_db == -4.


def test_result_freezes_at_first_quorum_and_future_audio_cannot_change_it():
    prefix = [-33.] * 25 + [-25.] * 25
    first, a = run(prefix + [-10.] * 1000)
    second, b = run(prefix + [None] * 1000)
    assert a[:50] == b[:50]
    assert first.gain_db == second.gain_db == 3.
    assert first.ready_bin_index == second.ready_bin_index == 49
    assert sum(row["newly_ready"] for row in a) == sum(row["newly_ready"] for row in b) == 1
    assert all(row["support_bins"] == 50 for row in a[49:] + b[49:])
    assert a[-1]["reason"] == b[-1]["reason"] == "hold_ready"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 10 ** 500])
def test_nonfinite_input_clears_partial_evidence_and_is_json_safe(bad):
    settings = CalibrationSettings(required_active_bins=3)
    calibrator, rows = run([-30., -30., bad, -28., -28., -28.], settings)
    assert rows[2]["reason"] == "invalid_nonfinite"
    assert not rows[2]["valid"] and rows[2]["support_bins"] == 0
    assert rows[2]["raw_rms_dbfs"] is None
    assert not rows[4]["ready"]
    assert calibrator.ready_bin_index == 5 and calibrator.gain_db == 2.
    json.dumps(rows, allow_nan=False)


@pytest.mark.parametrize("indexes", [[0, 1, 4, 5, 6, 7], [0, 1, 1, 2, 3, 4], [0, 1, 0, 2, 3, 4]])
def test_discontinuous_evidence_discarded_before_rebuilding_support(indexes):
    calibrator = ActivityCalibration(CalibrationSettings(required_active_bins=3))
    rows = [calibrator.observe(-30., bin_index=i) for i in indexes]
    assert rows[2]["reason"] == "invalid_sequence" and rows[2]["support_bins"] == 0
    assert not rows[4]["ready"]
    assert rows[5]["newly_ready"] and calibrator.ready_bin_index == indexes[-1]


def test_invalid_after_readiness_preserves_result_and_monotonic_clock():
    calibrator, _ = run([-30.] * 50)
    nan_row = calibrator.observe(math.nan, bin_index=50)
    old_row = calibrator.observe(-10., bin_index=20)
    gap_row = calibrator.observe(-10., bin_index=55)
    resumed = calibrator.observe(-10., bin_index=56)
    assert all(row["ready"] and not row["newly_ready"] and row["gain_db"] == 4.
               and row["ready_bin_index"] == 49 for row in (nan_row, old_row, gap_row, resumed))
    assert [row["reason"] for row in (nan_row, old_row, gap_row, resumed)] == [
        "invalid_nonfinite", "invalid_sequence", "invalid_sequence", "hold_ready"]
    assert not old_row["valid"] and resumed["valid"]


@pytest.mark.parametrize("bad_value,bad_index,error", [
    (True, 0, TypeError), ("-30", 0, TypeError), (object(), 0, TypeError),
    (-30., True, TypeError), (-30., 1., TypeError), (-30., -1, ValueError),
    (-30., 10 ** 500, ValueError),
])
def test_type_and_range_errors_do_not_mutate_clock(bad_value, bad_index, error):
    calibrator = ActivityCalibration(CalibrationSettings(required_active_bins=1))
    with pytest.raises(error):
        calibrator.observe(bad_value, bin_index=bad_index)
    assert not calibrator.ready
    assert calibrator.observe(-30., bin_index=0)["newly_ready"]


@pytest.mark.parametrize("change", [
    {"bin_seconds": 0}, {"bin_seconds": True}, {"bin_seconds": math.nan},
    {"window_bins": 0}, {"window_bins": 10001}, {"window_bins": True},
    {"required_active_bins": 301}, {"required_active_bins": 2.5},
    {"activity_dbfs": -91.}, {"activity_dbfs": math.inf},
    {"target_dbfs": -11.}, {"target_dbfs": "-26"},
    {"max_boost_db": -1.}, {"max_cut_db": 25.}, {"max_boost_db": 10 ** 500},
])
def test_invalid_settings_rejected(change):
    with pytest.raises(ValueError):
        CalibrationSettings(**change)


def test_returned_record_cannot_mutate_frozen_state():
    calibrator, rows = run([-30.] * 50)
    rows[-1]["gain_db"] = 99
    rows[-1]["ready_bin_index"] = 900
    result = calibrator.observe(None, bin_index=50)
    assert result["gain_db"] == 4. and result["ready_bin_index"] == 49
