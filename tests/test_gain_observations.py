"""Focused engineering tests: scalar JSON only, no original audio or host access."""

import copy
import json
from pathlib import Path

import pytest

from wcfi_tools.audio import gain_observations
from wcfi_tools.audio.gain_controller import GainController
from wcfi_tools.audio.gain_observations import (
    MAX_INPUT_BYTES,
    ROLES,
    SOURCE_SUFFIX,
    Adapter,
    load_october,
    percentile,
)


def anchor(count=40, level=-20.0):
    return {role: [level] * count for role in ROLES}


def frames(values, changes=None):
    changes = changes or {}
    return [
        {
            "time_seconds": index + 1,
            "levels": {role: values[role][index] + changes.get(role, 0) for role in ROLES},
        }
        for index in range(len(values["RED"]))
    ]


def replay(values, incoming=None):
    adapter = Adapter(values)
    return [result for frame in incoming or frames(values) if (result := adapter.push(frame))]


def feature_file(tmp_path, data=None):
    path = tmp_path.joinpath(*SOURCE_SUFFIX)
    path.parent.mkdir(parents=True)
    if data is None:
        data = {role: {"levels": list(range(530)), "bands": {}} for role in ROLES}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_loader_only_fixed_range(tmp_path):
    result = load_october(feature_file(tmp_path))
    assert tuple(result) == ROLES
    assert all(values == tuple(float(i) for i in range(240, 480)) for values in result.values())


@pytest.mark.parametrize("filename", ["other.json", "2026-01-24-features.json", "take.wav"])
def test_loader_rejects_other_paths_before_read(tmp_path, filename):
    with pytest.raises(ValueError, match="only the frozen"):
        load_october(tmp_path / filename)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "short", "nan", "bool", "bands", "unequal"])
def test_loader_rejects_invalid_metadata(tmp_path, mutation):
    data = {role: {"levels": [-20.0] * 530, "bands": {}} for role in ROLES}
    if mutation == "missing":
        del data["RED"]
    elif mutation == "unknown":
        data["OTHER"] = data["RED"]
    elif mutation == "short":
        data["RED"]["levels"] = [-20] * 479
    elif mutation in {"nan", "bool"}:
        data["RED"]["levels"][240] = float("nan") if mutation == "nan" else True
    elif mutation == "bands":
        data["RED"]["bands"] = []
    elif mutation == "unequal":
        data["RED"]["levels"].append(-20)
    with pytest.raises(ValueError):
        load_october(feature_file(tmp_path, data))


def test_loader_bounded_read_and_invalid_json(tmp_path):
    path = feature_file(tmp_path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid feature JSON"):
        load_october(path)
    with path.open("wb") as handle:
        handle.truncate(MAX_INPUT_BYTES + 1)
    with pytest.raises(ValueError, match="8 MiB"):
        load_october(path)


def test_percentile_matches_linear_interpolation():
    assert percentile([0, 1, 2, 3, 4, 5, 6, 7], 0.9) == pytest.approx(6.3)
    assert percentile([3], 0.9) == 3
    with pytest.raises(ValueError):
        percentile([], 0.9)


def test_warmup_schedule_and_exact_interval():
    logs = replay(anchor(10))
    assert [row["time_seconds"] for row in logs] == [2, 4, 6, 8, 10]
    assert [row["eligible"] for row in logs] == [False, False, False, True, True]
    assert logs[-1]["evidence"]["sample_interval"] == [242, 250]
    assert logs[-1]["evidence"]["shared_support"] == list(range(2, 10))
    assert logs[-1]["raw_error_db"] == 0


def test_ac_drift_and_immutable_candidate_support():
    values = anchor(12)
    baseline = replay(values)[-1]
    current = replay(values, frames(values, {"AC GUITAR": -3}))[-1]
    assert current["eligible"]
    assert current["raw_error_db"] == -3
    for field in ("source_support", "supported_context", "shared_support", "opportunity"):
        assert current["evidence"][field] == baseline["evidence"][field]


def test_same_frame_ratio_not_difference_of_mismatched_medians():
    values = anchor(8)
    values["RED"] = [-20, -20, -20, -20, -50, -50, -50, -50]
    values["AC GUITAR"] = [-20, -20, -20, -20, -20, -20, -20, -20]
    incoming = frames(values, {"AC GUITAR": 2})
    for frame in incoming[4:]:
        frame["levels"]["RED"] -= 20
    result = replay(values, incoming)[-1]
    assert result["evidence"]["shared_support"] == [0, 1, 2, 3]
    assert result["eligible"]
    assert result["raw_error_db"] == 2


@pytest.mark.parametrize("gain", [-3, 3])
def test_common_gain_with_differing_raw_supports(gain):
    values = anchor(8)
    values["KIT"] = [-20, -20, -20, -20, -70, -70, -70, -70]
    values["BASS"] = [-70, -70, -70, -70, -20, -20, -20, -20]
    result = replay(values, frames(values, {role: gain for role in ROLES}))[-1]
    assert result["eligible"]
    assert result["raw_error_db"] == 0
    assert result["evidence"]["common_estimate_db"] == gain
    assert set(result["evidence"]["supported_context"]) == set(ROLES[2:])


@pytest.mark.parametrize("role", ["RED", "KIT", "BASS", "KEYS", "EGUITAR"])
@pytest.mark.parametrize("gain", [-3, 3])
def test_isolated_red_and_non_target_gain_changes_abstain(role, gain):
    values = anchor(8)
    result = replay(values, frames(values, {role: gain}))[-1]
    assert not result["eligible"]
    assert result["opportunity"]
    assert result["evidence"]["runtime_veto"]
    expected = "red_common_gain_disagreement" if role == "RED" else "context_gain_disagreement"
    assert result["reason"] == expected
    assert result["contiguous_seconds"] == 0


@pytest.mark.parametrize("role", ["RED", "KIT"])
def test_window_attribution_disagreement_needs_eight_new_seconds(role):
    values = anchor(24)
    incoming = frames(values)
    for frame in incoming[:8]:
        frame["levels"][role] += 3
    result = {row["time_seconds"]: row for row in replay(values, incoming)}
    assert result[8]["opportunity"]
    assert result[8]["contiguous_seconds"] == 0
    assert all(not result[t]["eligible"] for t in (10, 12, 14))
    assert result[16]["eligible"]
    assert result[16]["contiguous_seconds"] == 8
    assert result[16]["raw_error_db"] == 0


def test_context_source_does_not_disappear_when_attenuated_to_silence():
    values = anchor(8)
    result = replay(values, frames(values, {"KIT": -60}))[-1]
    assert "KIT" in result["evidence"]["supported_context"]
    assert not result["eligible"]
    assert result["reason"] == "current_silence_on_fixed_support"
    assert result["contiguous_seconds"] == 0


def test_insufficient_context_stays_ineligible():
    values = anchor(8)
    for role in ("KIT", "BASS", "KEYS"):
        values[role] = [-70] * 8
    result = replay(values)[-1]
    assert not result["opportunity"]
    assert not result["eligible"]
    assert result["reason"] == "insufficient_raw_support"
    assert result["contiguous_seconds"] == 8  # Quiet raw data are not a timing/data fault.


def test_freshness_and_attribution_boundary_values_are_inclusive_as_specified():
    values = anchor(8)
    incoming = frames(values, {"RED": 0.5, "KIT": 0.5})
    incoming[-1].update(observation_age_seconds=0.499999, state_age_seconds=2.5)
    result = replay(values, incoming)[-1]
    assert result["eligible"]
    assert result["raw_error_db"] == -0.5
    incoming[-1]["state_age_seconds"] = 2.500001
    assert replay(values, incoming)[-1]["reason"] == "stale_state"


def test_raw_floor_is_strict_and_four_shared_frames_required():
    values = anchor(8)
    values["RED"] = [-60] * 4 + [-20] * 4
    result = replay(values)[-1]
    assert result["eligible"]
    assert result["evidence"]["shared_support"] == [4, 5, 6, 7]
    values["RED"][4] = -60
    assert not replay(values)[-1]["opportunity"]


@pytest.mark.parametrize("fault", [
    {"valid": False, "reason": "missing"},
    {"observation_age_seconds": 0.5},
    {"state_age_seconds": 2.50001},
    {"attribution_ambiguous": True},
    {"alignment_unknown": True},
])
def test_faults_clear_history_and_require_full_fresh_window(fault):
    values = anchor(20)
    incoming = frames(values)
    incoming[9].update(fault)
    result = {row["time_seconds"]: row for row in replay(values, incoming)}
    assert result[8]["eligible"]
    assert not result[10]["eligible"]
    assert result[10]["contiguous_seconds"] == 0
    assert all(not result[t]["eligible"] for t in (12, 14, 16))
    assert result[18]["eligible"]


@pytest.mark.parametrize("fault", ["missing_role", "nan", "epoch", "format"])
def test_structural_faults_recover(fault):
    values = anchor(22)
    incoming = frames(values)
    if fault == "missing_role":
        del incoming[9]["levels"]["RED"]
    elif fault == "nan":
        incoming[9]["levels"]["AC GUITAR"] = float("nan")
    else:
        for frame in incoming[9:]:
            frame[fault] = "new"
    result = {row["time_seconds"]: row for row in replay(values, incoming)}
    assert not result[10]["eligible"]
    assert result[18]["eligible"]


def test_actual_gap_clears_history_without_hiding_opportunity():
    values = anchor(20)
    incoming = frames(values)
    del incoming[8]
    result = {row["time_seconds"]: row for row in replay(values, incoming)}
    assert result[10]["reason"] == "timing_gap"
    assert result[10]["opportunity"]
    assert result[18]["eligible"]


def test_missing_scheduled_timestamp_recovers_without_reusing_pre_gap_frames():
    values = anchor(22)
    incoming = frames(values)
    del incoming[9]  # No frame at t=10: first observed recovery frame is odd t=11.
    result = {row["time_seconds"]: row for row in replay(values, incoming)}
    assert 10 not in result
    assert result[12]["reason"] == "fresh_history_required"
    assert result[12]["contiguous_seconds"] == 1
    assert result[12]["opportunity"]
    assert all(not result[t]["eligible"] for t in (12, 14, 16, 18))
    assert result[20]["eligible"]


def test_revision_passes_through_and_explicit_rearm_resets_anchor():
    values = anchor(30)
    adapter = Adapter(values)
    for frame in frames(values)[:10]:
        adapter.push(frame)
    changed = frames(values, {"AC GUITAR": 1})
    changed[10]["revision"] = "manual"
    adapter.push(changed[10])
    changed[11]["revision"] = "manual"
    result = adapter.push(changed[11])
    assert result["revision"] == "manual"
    assert not result["eligible"]
    adapter.reset(12, "manual", "new-anchor", {"AC GUITAR": 1})
    for frame in changed[12:20]:
        frame.update(revision="manual", anchor_revision="new-anchor")
        result = adapter.push(frame)
    assert result["eligible"]
    assert result["raw_error_db"] == 0
    assert result["evidence"]["shared_support"] == list(range(12, 20))


@pytest.mark.parametrize("field", ["revision", "anchor_revision", "completed_revision"])
def test_odd_transient_revision_event_suspends_until_explicit_rearm(field):
    values = anchor(70)
    incoming = frames(values, {"AC GUITAR": 6})
    incoming[12][field] = "transient-manual-or-stale-result"  # t=13, no emission.
    adapter, controller = Adapter(values), GainController()
    logs = {}
    observations = {}
    for frame in incoming[:50]:
        observation = adapter.push(frame)
        if observation is not None:
            observations[frame["time_seconds"]] = observation
            logs[frame["time_seconds"]] = controller.step(observation)
    assert logs[12]["action"] == "propose"
    assert all(logs[t]["suspended"] for t in range(14, 51, 2))
    assert all(logs[t]["delta_db"] == 0 for t in range(14, 51, 2))
    assert observations[14][field] == "transient-manual-or-stale-result"
    assert observations[50][field] == "transient-manual-or-stale-result"
    assert observations[14]["revision_event"]["time_seconds"] == 13
    adapter.reset(50, "accepted-rearm", "accepted-new-anchor")
    controller.rearm(50, "accepted-rearm", "accepted-new-anchor")
    for frame in incoming[50:]:
        frame.update(revision="accepted-rearm", anchor_revision="accepted-new-anchor")
        observation = adapter.push(frame)
        if observation is not None:
            assert observation["revision_event"] is None
            logs[frame["time_seconds"]] = controller.step(observation)
    assert all(logs[t]["delta_db"] == 0 for t in range(52, 62, 2))
    assert logs[62]["action"] == "propose"
    assert logs[62]["delta_db"] == -0.5


@pytest.mark.parametrize("revision,anchor_revision", [
    ("base", "new-anchor"), ("new-revision", "base"), ("base", "base"),
])
def test_adapter_reset_requires_both_new_accepted_identities(revision, anchor_revision):
    adapter = Adapter(anchor())
    with pytest.raises(ValueError, match="new revision and a new anchor"):
        adapter.reset(0, revision, anchor_revision)


def test_prefix_equivalence_and_adversarial_future():
    values = anchor(40)
    for role_index, role in enumerate(ROLES):
        values[role] = [-20 + (index % 5) * (role_index + 1) for index in range(40)]
    incoming = frames(values, {"AC GUITAR": 3})
    full = replay(values, incoming)
    for end in range(2, 41, 2):
        prefix_anchor = {role: values[role][:end] for role in ROLES}
        expected = [row for row in full if row["time_seconds"] <= end]
        assert replay(prefix_anchor, incoming[:end]) == expected
        corrupted_anchor = {role: values[role][:end] + [None] * (40 - end) for role in ROLES}
        corrupted_frames = copy.deepcopy(incoming)
        for frame in corrupted_frames[end:]:
            frame["levels"] = {role: 10000 if role == "RED" else -10000 for role in ROLES}
            frame["valid"] = False
            frame["private_expected_action"] = "arbitrary shuffled label"
        assert replay(corrupted_anchor, corrupted_frames)[:len(expected)] == expected


def test_history_is_bounded_and_inputs_unmodified():
    values = anchor(500)
    incoming = frames(values)
    original = copy.deepcopy(incoming)
    adapter = Adapter(values)
    for frame in incoming:
        adapter.push(frame)
        assert len(adapter.history) <= 9
        assert len(adapter._anchor_validity) <= 8
        assert all(index >= frame["time_seconds"] - 8 for index, _ in adapter._anchor_validity)
    assert incoming == original


def test_future_private_label_is_never_read():
    values = anchor(12)
    incoming = frames(values)
    for frame in incoming:
        frame["private_expected_action"] = object()
    assert replay(values, incoming) == replay(values)


def test_loader_uses_only_october_fixture_when_real_file_available():
    path = Path(__file__).resolve().parents[1].joinpath(*SOURCE_SUFFIX)
    if not path.exists():
        pytest.skip("local October observation file is not distributed")
    data = load_october(path)
    assert all(len(values) == 240 for values in data.values())
    assert len(replay(data)) == 120


@pytest.mark.parametrize("role", ["RED", "EGUITAR"])
@pytest.mark.parametrize("bad_value", [None, True, float("nan"), float("inf")])
def test_causal_anchor_validity_cache_matches_uncached_support(role, bad_value):
    values = anchor(30)
    incoming = frames(values)  # Current input is valid; only the oracle anchor is corrupted.
    values[role][5] = bad_value
    adapter = Adapter(values)
    uncached = Adapter(values)
    for frame in incoming:
        result = adapter.push(frame)
        if result is not None:
            expected_support = uncached._support(frame["time_seconds"])
            # These fields are computed before current-frame eligibility/estimates.
            for field in ("sample_interval", "shared_support", "source_support",
                          "supported_context", "thresholds_dbfs", "opportunity", "support_reason"):
                assert result["evidence"][field] == expected_support[field]
    assert all(count == 0 for count in adapter._anchor_invalid_counts.values())


def test_validity_cache_falls_back_for_gaps_and_direct_support_calls():
    values = anchor(30)
    values["KEYS"][7] = None
    incoming = frames(anchor(30))
    adapter, uncached = Adapter(values), Adapter(values)
    for index in (0, 1, 2, 3, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19):
        adapter.push(incoming[index])
        for end in (8, 12, 16, 20, 24):
            assert adapter._support(end) == uncached._support(end)
    assert len(adapter._anchor_validity) == 8
    adapter.reset(20, "new-revision", "new-anchor")
    assert not adapter._anchor_validity
    assert all(count == 0 for count in adapter._anchor_invalid_counts.values())


def test_anchor_validity_checked_once_per_reached_frame_without_future_reads(monkeypatch):
    values = anchor(20)
    # Unseen bad suffix must neither be validated nor enter an earlier cache.
    for role in ROLES:
        values[role][10:] = [None] * 10
    original = gain_observations._finite
    checked = []

    def counted(value):
        checked.append(value)
        return original(value)

    monkeypatch.setattr(gain_observations, "_finite", counted)
    adapter = Adapter(values)
    assert checked == []
    for end in range(1, 11):
        adapter.push({"time_seconds": end, "valid": False})
        assert len(checked) == len(ROLES) * end
    assert None not in checked
