"""Fake scalar contracts only. No capture, host, filesystem output, or audio."""
from copy import deepcopy
import math

import pytest

from wcfi_tools.audio.experimental_audio_gain import ROLES
from wcfi_tools.audio.experimental_live_input import PublicationClockGainV2


EXPECTED = {role: {"node_id": "node:" + role, "pid": 321, "epoch": 4} for role in ROLES}
OFFSETS = {"KIT": -4., "BASS": -2., "KEYS": -6., "EGUITAR": -5.}


def group(index, acoustic_gain=0.):
    red = -30 + (1 if index % 2 else -1)
    acoustic = -30 + (-2 if (index - 1) % 4 < 2 else 2) + acoustic_gain
    levels = {"RED": red, "AC GUITAR": acoustic, **{r: red + v for r, v in OFFSETS.items()}}
    windows = {}
    for ordinal, role in enumerate(ROLES):
        end = index * 48000
        callback = round((1000 + index + ordinal * .001 + .005) * 1e9)
        windows[role] = {"source": role, **EXPECTED[role], "stage": "after_effects_before_fader",
            "sample_rate": 48000, "channels": 2, "complete": True, "measurement_valid": True,
            "window_start_local_frame": end - 48000, "window_end_local_frame": end,
            "observed_frames": 48000, "missing_frames": 0, "nonfinite_samples": [0, 0],
            "read_failures": 0, "stale_reads": 0, "rms_dbfs": [levels[role], levels[role]],
            "capture_age_min_ms": 1., "capture_age_max_ms": 10., "callback_last_ns": callback,
            "last_status": {"active": True, "flags": 0, "written_frame": end + 240,
                            "callback_ns": callback, "host_sample": 0, "host_system_ns": 0}}
    return {"time_seconds": index, "sequence": index, "windows": windows,
            "received_at_qpc": 1000 + index + .1, "state_query_started_at_qpc": 1000 + index - 1,
            "state_observed_at_qpc": 1000 + index, "source_map_revision": "map:1",
            "routing_revision": "routing:1", "state_revision": "state:1", "host_sample_alignment": False}


def run(policy, stop, change=None, start=1):
    rows = []
    for index in range(start, stop + 1):
        value = group(index, 4. if index >= 41 else 0.)
        if change:
            change(index, value)
        rows.extend(policy.feed(value))
    return rows


def test_fixed_eight_second_timing_warmup_precedes_fixed_sixteen_second_reference():
    policy = PublicationClockGainV2(EXPECTED)
    for index in range(1, 9):
        assert policy.feed(group(index)) == []
        assert policy.input_status["policy_started"] is False
        assert policy.reference is None
    assert policy.feed(group(9)) == []  # First internal policy endpoint.
    assert policy.input_status["policy_endpoint"] == 1
    run(policy, 23, start=10)
    assert policy.reference is None
    row = policy.feed(group(24))[0]
    assert row["time_seconds"] == 16
    assert row["observation_group_index"] == 24
    assert policy.reference is not None
    frozen = deepcopy(policy.reference)
    run(policy, 100, start=25)
    assert policy.reference == frozen


def test_zero_host_flags_never_become_shared_sample_alignment():
    policy = PublicationClockGainV2(EXPECTED)
    rows = run(policy, 100)
    assert any(row["delta_db"] < 0 for row in rows)
    assert all(row["host_sample_alignment"] is False for row in rows)
    assert all(row["live_eligible"] is False for row in rows)
    assert all(row["input_contract"] == "experimental_publication_clock_v1" for row in rows)
    assert policy.input_status["acoustic_skew_bound_seconds"] is None
    assert "alignment_known" not in policy.input_status


@pytest.mark.parametrize("fault", ["gap", "nonfinite", "stale", "publication_age", "skew", "advance", "missing"])
def test_fixed_timing_startup_failure_never_searches_for_later_reference(fault):
    policy = PublicationClockGainV2(EXPECTED)
    def change(index, value):
        if index != 4:
            return
        row = value["windows"]["AC GUITAR"]
        if fault == "gap":
            row["missing_frames"] = 1
        elif fault == "nonfinite":
            row["rms_dbfs"][0] = math.nan
        elif fault == "stale":
            row["capture_age_max_ms"] = 250.
        elif fault == "publication_age":
            value["received_at_qpc"] += .2
        elif fault in ("skew", "advance"):
            shift = 60_000_000 if fault == "skew" else 12_000_000
            row["callback_last_ns"] += shift
            row["last_status"]["callback_ns"] += shift
        else:
            del value["windows"]["KIT"]
    assert run(policy, 100, change) == []
    assert policy.reference is None
    assert policy.offset_db == 0
    assert policy.input_status["reason"].startswith("startup_no_go:")


@pytest.mark.parametrize("field,value", [("pid", 322), ("epoch", 5), ("node_id", "other"), ("sample_rate", 44100)])
def test_identity_or_format_changes_suspend_retaining_actual_offset(field, value):
    policy = PublicationClockGainV2(EXPECTED)
    run(policy, 100)
    before = policy.offset_db
    assert before < 0
    changed = group(101, 4)
    changed["windows"]["AC GUITAR"][field] = value
    policy.feed(changed)
    run(policy, 120, start=102)
    assert policy.phase == "suspended"
    assert policy.offset_db == before


def test_transient_quality_failure_holds_and_requalifies_without_replacing_reference():
    policy = PublicationClockGainV2(EXPECTED)
    run(policy, 100)
    frozen, offset = deepcopy(policy.reference), policy.offset_db
    bad = group(101, 4)
    bad["windows"]["KIT"]["stale_reads"] = 1
    policy.feed(bad)
    assert policy.offset_db == offset
    assert policy.input_status["publication_windows_qualified"] is False
    run(policy, 110, start=102)
    assert policy.input_status["publication_windows_qualified"] is True
    assert policy.reference == frozen


def test_state_age_uses_request_start_not_newer_response_receipt():
    policy = PublicationClockGainV2(EXPECTED)
    value = group(1)
    value["state_query_started_at_qpc"] = value["received_at_qpc"] - 2.51
    value["state_observed_at_qpc"] = value["received_at_qpc"]
    policy.feed(value)
    assert "stale_state" in policy.input_status["reason"]


@pytest.mark.parametrize("key", ["source_map_revision", "routing_revision", "state_revision"])
def test_state_revision_change_is_a_latched_hard_fault(key):
    policy = PublicationClockGainV2(EXPECTED)
    run(policy, 100)
    before = policy.offset_db
    value = group(101, 4)
    value[key] = "changed"
    policy.feed(value)
    run(policy, 120, start=102)
    assert policy.phase == "suspended"
    assert policy.offset_db == before


def test_forged_shared_alignment_claim_is_rejected():
    policy = PublicationClockGainV2(EXPECTED)
    value = group(1)
    value["alignment_known"] = True
    policy.feed(value)
    assert "unsupported_alignment_claim" in policy.input_status["reason"]


def test_missing_policy_group_becomes_invalid_slot_without_reindexing_time():
    policy = PublicationClockGainV2(EXPECTED)
    run(policy, 30)
    policy.feed(group(32))
    assert policy.input_status["policy_endpoint"] == 24
    assert any(slot["endpoint"] == 23 and slot["reason"] == "missing_frame" for slot in policy._history)


def test_input_copies_cannot_mutate_expected_identity_or_reference():
    expected = deepcopy(EXPECTED)
    policy = PublicationClockGainV2(expected)
    expected["RED"]["node_id"] = "changed externally"
    run(policy, 24)
    assert policy.reference is not None
    status = policy.input_status
    status["host_sample_alignment"] = True
    assert policy.input_status["host_sample_alignment"] is False


def test_stereo_levels_are_power_averaged():
    policy = PublicationClockGainV2(EXPECTED)
    for index in range(1, 10):
        value = group(index)
        value["windows"]["AC GUITAR"]["rms_dbfs"] = [-30., -20.]
        policy.feed(value)
    level = policy._calibration[0]["levels"]["AC GUITAR"]
    assert level == pytest.approx(10 * math.log10((10 ** -3 + 10 ** -2) / 2))


def test_verified_silent_context_is_absent_without_invalidating_other_sources():
    policy = PublicationClockGainV2(EXPECTED)
    for index in range(1, 25):
        value = group(index)
        value["windows"]["BASS"].update(rms_dbfs=[None, None], zero_energy_channels=[True, True])
        policy.feed(value)
    assert policy.reference is not None
    assert "BASS" not in policy.reference["context_roles"]
    assert policy._calibration[0]["levels"]["BASS"] == -120.
    assert policy.input_status["zero_energy_channels"]["BASS"] == [True, True]


def test_single_silent_channel_contributes_zero_power_not_a_missing_measurement():
    policy = PublicationClockGainV2(EXPECTED)
    for index in range(1, 10):
        value = group(index)
        value["windows"]["AC GUITAR"].update(rms_dbfs=[None, -20.], zero_energy_channels=[True, False])
        policy.feed(value)
    assert policy._calibration[0]["levels"]["AC GUITAR"] == pytest.approx(-20 + 10 * math.log10(.5))


def test_unflagged_missing_rms_is_not_silence():
    policy = PublicationClockGainV2(EXPECTED)
    value = group(1)
    value["windows"]["BASS"]["rms_dbfs"] = [None, None]
    policy.feed(value)
    assert "nonfinite_source_rms" in policy.input_status["reason"]


def test_drift_limit_covers_eight_seconds_not_only_adjacent_windows():
    policy = PublicationClockGainV2(EXPECTED)
    for index in range(1, 10):
        value = group(index)
        for row in value["windows"].values():
            row["callback_last_ns"] += index * 1_500_000
            row["last_status"]["callback_ns"] += index * 1_500_000
        policy.feed(value)
    assert "counter_qpc_advance_residual_exceeded" in policy.input_status["reason"]
    assert policy.input_status["max_counter_qpc_residual_seconds"] == pytest.approx(.012)
    assert policy.reference is None


@pytest.mark.parametrize("fault", ["receipt", "publication", "counter", "duplicate_group"])
def test_clock_and_counter_rollbacks_suspend_without_restoring_gain(fault):
    policy = PublicationClockGainV2(EXPECTED)
    run(policy, 100)
    before = policy.offset_db
    value = group(101, 4)
    if fault == "receipt":
        value["received_at_qpc"] = 1099.
        value["state_observed_at_qpc"] = 1099.
        value["state_query_started_at_qpc"] = 1098.
    elif fault == "publication":
        row = value["windows"]["AC GUITAR"]
        row["callback_last_ns"] -= 2_000_000_000
        row["last_status"]["callback_ns"] -= 2_000_000_000
    elif fault == "counter":
        row = value["windows"]["AC GUITAR"]
        row["window_start_local_frame"] -= 48000
        row["window_end_local_frame"] -= 48000
        row["last_status"]["written_frame"] -= 48000
    else:
        value = group(100, 4)
    policy.feed(value)
    assert policy.phase == "suspended"
    assert policy.offset_db == before
