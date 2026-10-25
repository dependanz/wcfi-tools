"""Development checks for the past-target contract; no audio or host access.

These scalar values and timings are separate from the prospective evaluation
fixtures. Human-looking identifiers below are explicitly synthetic test records,
never assertions that somebody approved an audible mix.
"""
from __future__ import annotations

import copy
import json

import pytest

from wcfi_tools.audio.gain_target_policy import TargetPolicy


ROLES = ("AC GUITAR", "RED", "KIT", "BASS", "KEYS", "EGUITAR")
CONTEXTS = ROLES[2:]
LEVELS = {"AC GUITAR": -31.0, "RED": -27.0, "KIT": -34.0,
          "BASS": -36.0, "KEYS": -29.0, "EGUITAR": -33.0}


def metadata(suffix="one", *, diagnostic=False):
    return {
        "target_revision": "target-" + suffix,
        "scope_revision": "scope-" + suffix,
        "state_revision": "state-" + suffix,
        "authorization_epoch": "epoch-" + suffix,
        "source_ids": {role: "dev-source-" + role for role in ROLES},
        "measurement_locations": {role: "dev-location-" + role for role in ROLES},
        "measurement_domain": "raw_input" if diagnostic else "post_fx_pre_fader",
        "baseline_faders_db": None if diagnostic else dict.fromkeys(ROLES, 0.0),
        "scope": {"performer_assignment": "development-performers",
                  "lead_reference": "development-lead",
                  "section": "development-section", "arrangement": "development-band",
                  "dynamic_intent": "development-steady"},
        "state_snapshot": {"chain": "dev-chain", "routing": "dev-routing",
                           "sends": "dev-sends"},
    }


def frame(t, meta=None, *, changes=None, levels=None, **fields):
    meta = metadata() if meta is None else meta
    values = dict(LEVELS if levels is None else levels)
    for role, delta in (changes or {}).items():
        values[role] += delta
    return {
        "time_seconds": t, "levels": values,
        "source_ids": copy.deepcopy(meta["source_ids"]),
        "measurement_locations": copy.deepcopy(meta["measurement_locations"]),
        "scope_revision": meta["scope_revision"],
        "state_revision": meta["state_revision"], "state_matches": True,
        "observation_age_seconds": 0.0, "state_age_seconds": 0.0, **fields,
    }


def acceptance(candidate, *, at=9):
    meta = candidate["metadata"]
    return {"operator_id": "development-synthetic-operator",
            "namespace": "synthetic_test_only", "accepted_at": at, "received_at": at,
            "scope_revision": meta["scope_revision"],
            "target_revision": meta["target_revision"],
            "state_revision": meta["state_revision"],
            "calibration_id": candidate["calibration_id"]}


def declaration(meta=None, *, identifier="initial", receipt=9, start=10, stop=40,
                mode="track_accepted_ratio"):
    meta = metadata() if meta is None else meta
    return {
        "declaration_id": "development-" + identifier,
        "issuer": "development-synthetic-operator",
        "provenance": "independent-development-clock-script",
        "issued_at": receipt, "received_at": receipt,
        "effective_from": start, "expires_at": stop,
        "scope_revision": meta["scope_revision"],
        "target_revision": meta["target_revision"],
        "state_revision": meta["state_revision"],
        "authorization_epoch": meta["authorization_epoch"],
        "operator_mode": mode,
        "applicability": {
            "same_sources": True, "same_lead": True, "comparable_section": True,
            "comparable_arrangement": True, "comparable_dynamics": True,
            "expected_activity": dict.fromkeys(ROLES, True),
            "bleed_suitable": dict.fromkeys(ROLES, True),
        },
    }


def candidate(policy=None, meta=None, *, frames=None, end=8):
    meta = metadata() if meta is None else meta
    policy = TargetPolicy(namespace="synthetic_test_only") if policy is None else policy
    supplied = frames if frames is not None else [frame(t, meta) for t in range(end - 7, end + 1)]
    result = policy.calibrate(supplied, meta, time_seconds=end)
    assert result is not None
    return policy, result


def armed(*, meta=None, calibration_frames=None, initial_declaration=None):
    meta = metadata() if meta is None else meta
    policy, target = candidate(meta=meta, frames=calibration_frames)
    policy.accept(acceptance(target), time_seconds=9)
    policy.declare(initial_declaration or declaration(meta), time_seconds=9)
    policy.arm(10)
    return policy


def advance(policy, stop, *, start=11, meta=None, changes=None, mutate=None):
    results = []
    for t in range(start, stop + 1):
        current = frame(t, meta, changes=changes)
        if mutate is not None:
            mutate(current)
        policy.feed(current)
        if t % 2 == 0:
            results.append(policy.evaluate(t))
    return results


def scoped_flags(result):
    assert result["musical_approval_valid"] is False
    assert result["live_eligible"] is False
    assert result["namespace"] in {"synthetic_test_only", "input_domain_diagnostic"}


def no_move(result):
    assert result["gain_proposal"] is None
    if result["controller_decision"] is not None:
        assert result["controller_decision"]["delta_db"] == 0


def test_constructor_never_arms_or_creates_a_controller():
    for namespace in ("synthetic_test_only", "input_domain_diagnostic"):
        policy = TargetPolicy(namespace=namespace)
        assert not policy.controller_created
        assert policy.controller_calls == 0
    with pytest.raises(ValueError):
        TargetPolicy(namespace="live")


def test_constant_calibration_arithmetic_and_fixed_floors():
    policy, record = candidate()
    assert record["theta_db"] == -4.0
    assert record["phi_db"] == {"KIT": 7.0, "BASS": 9.0, "KEYS": 2.0, "EGUITAR": 6.0}
    assert record["thresholds_dbfs"] == {role: max(-60.0, value - 24)
                                         for role, value in LEVELS.items()}
    assert record["calibration_interval"] == [0, 8]
    assert not policy.controller_created


def test_known_faders_are_applied_once_to_target_not_to_raw_support_floors():
    meta = metadata()
    meta["baseline_faders_db"].update({"AC GUITAR": -1.0, "RED": 2.0, "KIT": 1.0,
                                      "KEYS": -3.0, "EGUITAR": 2.0})
    policy = armed(meta=meta)
    result = advance(policy, 18, meta=meta, changes={"AC GUITAR": 1.75})[-1]
    assert policy.target["theta_db"] == -7.0
    assert result["raw_error_db"] == pytest.approx(1.75, abs=1e-9)
    assert result["context_errors_db"] == dict.fromkeys(CONTEXTS, 0.0)
    assert policy.target["thresholds_dbfs"]["AC GUITAR"] == -55.0


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), -float("inf")])
def test_every_calibration_frame_must_be_finite(bad):
    policy = TargetPolicy(namespace="synthetic_test_only")
    supplied = [frame(t) for t in range(1, 9)]
    supplied[2]["levels"]["EGUITAR"] = bad
    assert policy.calibrate(supplied, metadata(), time_seconds=8) is None
    assert policy.calibration_error
    assert not policy.controller_created


@pytest.mark.parametrize("variant", ["short", "gap", "future", "unknown_source", "wrong_location"])
def test_invalid_calibration_cannot_produce_a_target(variant):
    supplied = [frame(t) for t in range(1, 9)]
    if variant == "short":
        supplied.pop()
    elif variant == "gap":
        supplied[3]["time_seconds"] = 99
    elif variant == "future":
        supplied[-1]["time_seconds"] = 9
    elif variant == "unknown_source":
        supplied[2]["source_ids"]["RED"] = "different-lead"
    else:
        supplied[2]["measurement_locations"]["RED"] = "different-location"
    policy = TargetPolicy(namespace="synthetic_test_only")
    assert policy.calibrate(supplied, metadata(), time_seconds=8) is None
    assert policy.calibration_error


def test_post_fx_metadata_cannot_silently_assume_zero_faders():
    meta = metadata()
    meta["baseline_faders_db"] = None
    policy = TargetPolicy(namespace="synthetic_test_only")
    assert policy.calibrate([frame(t, meta) for t in range(1, 9)], meta,
                            time_seconds=8) is None


@pytest.mark.parametrize("fields", [
    {"state_matches": False}, {"scope_revision": "other-scope"},
    {"state_revision": "other-state"}, {"observation_age_seconds": 0.5},
    {"state_age_seconds": 2.50001}, {"observation_age_seconds": -0.1},
    {"state_age_seconds": float("nan")},
])
def test_synthetic_calibration_requires_valid_current_state_and_age(fields):
    policy = TargetPolicy(namespace="synthetic_test_only")
    supplied = [frame(t) for t in range(1, 9)]
    supplied[3].update(fields)
    assert policy.calibrate(supplied, metadata(), time_seconds=8) is None
    assert policy.calibration_error
    assert not policy.controller_created


def test_calibration_needs_four_frames_in_every_required_pair():
    policy = TargetPolicy(namespace="synthetic_test_only")
    supplied = [frame(t) for t in range(1, 9)]
    for current in supplied[:5]:
        current["levels"]["KIT"] = -90.0
    assert policy.calibrate(supplied, metadata(), time_seconds=8) is None


def test_unequal_support_uses_paired_differences_not_separate_medians():
    supplied = []
    for index in range(8):
        values = dict.fromkeys(ROLES, -25.0)
        values["AC GUITAR"] = -30.0 - index
        values["RED"] = -20.0 if index < 4 else -70.0
        supplied.append(frame(index + 1, levels=values))
    policy = armed(calibration_frames=supplied)
    assert policy.target["theta_db"] == -11.5
    for index in range(8):
        values = dict(supplied[index]["levels"])
        values["AC GUITAR"] += 2.0
        if index >= 4:
            values["AC GUITAR"] = -90.0
        policy.feed(frame(index + 11, levels=values))
    result = policy.evaluate(18)
    assert result["raw_error_db"] == 2.0
    assert result["paired_support"]["AC GUITAR"] == [11, 12, 13, 14]
    assert result["synthetic_controller_eligible"] is True


@pytest.mark.parametrize("equal_floor_count,valid", [(4, True), (5, False)])
def test_monitoring_floor_is_strict_and_does_not_reestimate(equal_floor_count, valid):
    policy = armed()
    original = copy.deepcopy(policy.target["thresholds_dbfs"])
    for index in range(8):
        values = dict(LEVELS)
        if index < equal_floor_count:
            values["AC GUITAR"] = -55.0
        policy.feed(frame(index + 11, levels=values))
    result = policy.evaluate(18)
    assert result["numerical_diagnostic_valid"] is valid
    assert len(result["paired_support"]["AC GUITAR"]) == 8 - equal_floor_count
    assert policy.target["thresholds_dbfs"] == original
    if not valid:
        assert result["reason"] == "insufficient_support"


def test_acceptance_and_declaration_are_both_required_before_arm():
    policy, record = candidate()
    with pytest.raises(ValueError):
        policy.arm(9)
    assert not policy.controller_created
    policy.accept(acceptance(record, at=10), time_seconds=10)
    with pytest.raises(ValueError):
        policy.arm(11)
    assert not policy.controller_created
    assert policy.controller_calls == 0


@pytest.mark.parametrize("field,value", [("calibration_id", "wrong"),
                                        ("target_revision", "wrong"),
                                        ("state_revision", "wrong"),
                                        ("namespace", "real_musical_approval")])
def test_acceptance_cannot_accept_another_target_or_real_namespace(field, value):
    policy, record = candidate()
    approved = acceptance(record)
    approved[field] = value
    with pytest.raises(ValueError):
        policy.accept(approved, time_seconds=9)
    assert not policy.controller_created


def test_acceptance_receipt_cannot_be_future_or_backdated():
    for receipt in (8.5, 10):
        policy, record = candidate()
        approved = acceptance(record)
        approved["received_at"] = receipt
        with pytest.raises(ValueError):
            policy.accept(approved, time_seconds=9)


def test_acceptance_cannot_predate_the_creation_of_its_candidate():
    policy = TargetPolicy(namespace="synthetic_test_only")
    record = policy.calibrate([frame(t) for t in range(1, 9)], metadata(), time_seconds=10)
    assert record is not None
    approved = acceptance(record, at=11)
    approved["accepted_at"] = 9
    with pytest.raises(ValueError):
        policy.accept(approved, time_seconds=11)
    assert not policy.controller_created


@pytest.mark.parametrize("change", [
    {"expires_at": 40.001}, {"expires_at": 10}, {"effective_from": 8},
    {"received_at": 10}, {"received_at": 8}, {"issued_at": 10},
    {"operator_mode": "automatic_intent_detection"}, {"declaration_id": ""},
])
def test_declaration_rejects_unbounded_or_invalid_time_contract(change):
    policy, record = candidate()
    policy.accept(acceptance(record), time_seconds=9)
    permission = declaration()
    permission.update(change)
    with pytest.raises(ValueError):
        policy.declare(permission, time_seconds=9)
    assert not policy.controller_created


@pytest.mark.parametrize("location", ["declaration", "applicability"])
def test_private_truth_fields_are_not_a_public_authorization_contract(location):
    policy = armed()
    advance(policy, 20, changes={"AC GUITAR": 2.0})
    permission = declaration(identifier="bad-truth", receipt=21, start=22, stop=39)
    selected = permission if location == "declaration" else permission["applicability"]
    selected["injected_gain_fault"] = True
    with pytest.raises(ValueError):
        policy.declare(permission, time_seconds=21)
    results = advance(policy, 26, start=21, changes={"AC GUITAR": 2.0})
    assert all(row["lifecycle"] == "suspended" and row["gain_proposal"] is None
               for row in results)


def test_initial_hold_or_unknown_applicability_cannot_arm():
    for variant in ("hold", "unknown_activity", "unknown_bleed", "unknown_dynamics"):
        policy, record = candidate()
        policy.accept(acceptance(record), time_seconds=9)
        permission = declaration(mode="hold" if variant == "hold" else "track_accepted_ratio")
        if variant == "unknown_activity":
            permission["applicability"]["expected_activity"]["RED"] = None
        elif variant == "unknown_bleed":
            permission["applicability"]["bleed_suitable"]["AC GUITAR"] = None
        elif variant == "unknown_dynamics":
            permission["applicability"]["comparable_dynamics"] = None
        policy.declare(permission, time_seconds=9)
        with pytest.raises(ValueError):
            policy.arm(10)
        assert not policy.controller_created


def test_initial_arm_needs_eight_new_authorized_seconds():
    policy = armed()
    results = advance(policy, 22, changes={"AC GUITAR": 2.0})
    assert all(not row["synthetic_controller_eligible"] for row in results
               if row["time_seconds"] < 18)
    assert next(row for row in results if row["time_seconds"] == 18)["synthetic_controller_eligible"]
    assert all(row["gain_proposal"] is None for row in results if row["time_seconds"] < 22)
    assert results[-1]["controller_decision"]["delta_db"] == -0.5
    for result in results:
        scoped_flags(result)


@pytest.mark.parametrize("changes,error,context_ok", [
    ({}, 0.0, True), ({"AC GUITAR": 1.75}, 1.75, True),
    ({"AC GUITAR": -1.75}, -1.75, True),
    (dict.fromkeys(ROLES, 1.25), 0.0, True),
    ({"RED": -1.25}, 1.25, False),
    ({"KIT": 1.25}, 0.0, False),
])
def test_target_error_and_reference_attribution_are_distinct(changes, error, context_ok):
    policy = armed()
    result = advance(policy, 18, changes=changes)[-1]
    assert result["numerical_diagnostic_valid"] is True
    assert result["raw_error_db"] == pytest.approx(error, abs=1e-9)
    assert result["context_consistent"] is context_ok
    assert result["synthetic_controller_eligible"] is context_ok
    if not context_ok:
        assert result["reason"] == "context_disagreement"
        assert result["controller_observation"]["eligible"] is False
        assert result["controller_observation"]["raw_error_db"] is None
        no_move(result)
    scoped_flags(result)


@pytest.mark.parametrize("changes", [
    {}, {"AC GUITAR": -1.75}, dict.fromkeys(ROLES, 1.25),
    {"RED": -1.25}, {"KEYS": 1.25},
])
def test_null_quiet_common_and_ambiguous_paths_never_move(changes):
    policy = armed()
    results = advance(policy, 36, changes=changes)
    for result in results:
        no_move(result)
        assert result["controller_decision"]["offset_db"] == 0
        scoped_flags(result)


def test_virtual_gain_is_applied_once_and_proposals_obey_controller_cadence():
    policy = armed()
    results = advance(policy, 36, changes={"AC GUITAR": 2.0})
    decisions = [row["controller_decision"] for row in results]
    proposals = [row for row in decisions if row["delta_db"]]
    assert [(row["time_seconds"], row["offset_db"]) for row in proposals] == [(22, -0.5), (32, -1.0)]
    for decision in decisions:
        assert -3 <= decision["offset_db"] <= 0
        assert decision["offset_db"] == decision["total_before_db"] + decision["delta_db"]
        assert decision["effective_gain"] == 10 ** (decision["offset_db"] / 20)
    assert all(row["raw_error_db"] == 2.0 for row in results
               if row["numerical_diagnostic_valid"])


@pytest.mark.parametrize("role", CONTEXTS)
def test_no_required_context_can_be_dropped_to_hide_silence(role):
    policy = armed()
    result = advance(policy, 18, changes={role: -100})[-1]
    assert result["paired_support"][role] == []
    assert not result["synthetic_controller_eligible"]
    assert result["controller_observation"]["raw_error_db"] is None
    no_move(result)


@pytest.mark.parametrize("fields", [
    {"observation_age_seconds": 0.5}, {"state_age_seconds": 2.50001},
    {"observation_age_seconds": -1}, {"state_age_seconds": None},
])
def test_stale_or_invalid_data_never_becomes_a_controller_estimate(fields):
    policy = armed()
    def invalidate(current):
        if current["time_seconds"] == 20:
            current.update(fields)
    results = advance(policy, 32, changes={"AC GUITAR": 2.0}, mutate=invalidate)
    assert all(not row["synthetic_controller_eligible"] for row in results
               if 20 <= row["time_seconds"] < 28)
    assert all(row["gain_proposal"] is None for row in results
               if 20 <= row["time_seconds"] < 32)
    assert next(row for row in results if row["time_seconds"] == 28)["synthetic_controller_eligible"]
    for row in results:
        if not row["synthetic_controller_eligible"]:
            assert row["controller_observation"]["raw_error_db"] is None


def test_same_scope_timely_renewal_preserves_revision_and_controller_evidence():
    policy = armed()
    before = advance(policy, 28, changes={"AC GUITAR": 2.0})
    revision = before[-1]["controller_observation"]["revision"]
    policy.declare(declaration(identifier="renewal", receipt=29, start=40, stop=70),
                   time_seconds=29)
    after = advance(policy, 46, start=29, changes={"AC GUITAR": 2.0})
    assert all(row["authorization_valid"] for row in after)
    assert all(row["controller_observation"]["revision"] == revision for row in after)
    assert next(row for row in after if row["time_seconds"] == 40)["synthetic_controller_eligible"]
    assert all(row["lifecycle"] != "suspended" for row in after)


def test_expiry_is_half_open_and_latched_after_late_renewal():
    policy = armed()
    results = advance(policy, 40, changes={"AC GUITAR": 2.0})
    expired = results[-1]
    assert not expired["authorization_valid"]
    assert not expired["synthetic_controller_eligible"]
    assert expired["lifecycle"] == "suspended"
    assert expired["reason"] == "suspended:authorization_expired"
    no_move(expired)
    policy.declare(declaration(identifier="too-late", receipt=40, start=40, stop=70),
                   time_seconds=40)
    subsequent = advance(policy, 46, start=41, changes={"AC GUITAR": 2.0})
    assert all(row["lifecycle"] == "suspended" for row in subsequent)
    assert all(row["gain_proposal"] is None for row in subsequent)


def test_expiry_between_evaluations_is_observed_before_any_move():
    permission = declaration(stop=39)
    policy = armed(initial_declaration=permission)
    results = advance(policy, 40, changes={"AC GUITAR": 2.0})
    assert results[-1]["lifecycle"] == "suspended"
    assert results[-1]["loss_event"]["time_seconds"] == 39
    no_move(results[-1])


def test_reading_defensive_target_copies_does_not_extend_authorization():
    policy = armed()
    for _ in range(5):
        target = policy.target
        target["theta_db"] = 200
        target["metadata"]["authorization_epoch"] = "altered-copy"
    result = advance(policy, 40)[-1]
    assert policy.target["theta_db"] == -4.0
    assert result["lifecycle"] == "suspended"
    no_move(result)


def test_conflicting_overlap_blocks_tracking_after_arming():
    policy = armed()
    advance(policy, 20, changes={"AC GUITAR": 2.0})
    conflicting = declaration(identifier="hold-command", receipt=21, start=22, stop=39,
                              mode="hold")
    policy.declare(conflicting, time_seconds=21)
    results = advance(policy, 28, start=21, changes={"AC GUITAR": 2.0})
    assert results[0]["lifecycle"] == "suspended"
    assert all(row["gain_proposal"] is None for row in results)


def test_received_future_hold_cannot_rewrite_earlier_permission():
    policy = armed()
    advance(policy, 18, changes={"AC GUITAR": 2.0})
    policy.declare(declaration(identifier="future-hold", receipt=19, start=24, stop=39,
                               mode="hold"), time_seconds=19)
    results = advance(policy, 28, start=19, changes={"AC GUITAR": 2.0})
    assert all(row["authorization_valid"] and row["synthetic_controller_eligible"]
               for row in results if row["time_seconds"] < 24)
    assert all(row["lifecycle"] == "suspended" and row["gain_proposal"] is None
               for row in results if row["time_seconds"] >= 24)
    assert results[-1]["loss_event"]["time_seconds"] == 24


def test_polling_a_declaration_does_not_create_a_new_receipt_or_lifetime():
    policy, record = candidate()
    policy.accept(acceptance(record), time_seconds=9)
    permission = declaration()
    policy.declare(permission, time_seconds=9)
    policy.declare(copy.deepcopy(permission), time_seconds=9)
    policy.arm(10)
    assert advance(policy, 40)[-1]["lifecycle"] == "suspended"


@pytest.mark.parametrize("change", ["source", "location", "scope", "state", "manual", "revoke"])
def test_change_then_revert_latches_suspension(change):
    policy = armed()
    def alter(current):
        if current["time_seconds"] != 21:
            return
        if change == "source":
            current["source_ids"]["RED"] = "other-lead"
        elif change == "location":
            current["measurement_locations"]["AC GUITAR"] = "other-stage"
        elif change == "scope":
            current["scope_revision"] = "other-section"
        elif change == "state":
            current["state_revision"] = "other-chain"
        elif change == "manual":
            current["takeover"] = "manual-fader"
        else:
            current["revoked"] = True
    results = advance(policy, 30, changes={"AC GUITAR": 2.0}, mutate=alter)
    assert all(row["lifecycle"] == "suspended" and row["gain_proposal"] is None
               for row in results if row["time_seconds"] >= 22)


def test_rearm_requires_new_calibration_acceptance_revisions_and_fresh_history():
    policy = armed()
    advance(policy, 24, changes={"AC GUITAR": 2.0})
    policy.revoke(25)
    advance(policy, 30, start=25, changes={"AC GUITAR": 2.0})
    with pytest.raises(ValueError):
        policy.rearm(30)
    meta = metadata("two")
    new = policy.calibrate([frame(t, meta) for t in range(51, 59)], meta, time_seconds=58)
    assert new is not None
    policy.accept(acceptance(new, at=59), time_seconds=59)
    policy.declare(declaration(meta, identifier="rearm", receipt=59, start=60, stop=90),
                   time_seconds=59)
    policy.rearm(60)
    results = advance(policy, 72, start=61, meta=meta, changes={"AC GUITAR": 2.0})
    assert all(not row["synthetic_controller_eligible"] for row in results
               if row["time_seconds"] < 68)
    assert all(row["gain_proposal"] is None for row in results if row["time_seconds"] < 72)
    assert results[-1]["controller_decision"]["delta_db"] == -0.5
    assert results[-1]["controller_decision"]["offset_db"] == -0.5
    assert results[-1]["controller_observation"]["anchor_revision"] == "target-two"


@pytest.mark.parametrize("new_epoch_conflict", [False, True])
def test_rearm_before_old_lease_expiry_partitions_epochs_without_hiding_new_conflicts(new_epoch_conflict):
    policy = armed()  # Original lease is still present and covers [10,40).
    advance(policy, 18, changes={"AC GUITAR": 2.0})
    policy.feed(frame(19, changes={"AC GUITAR": 2.0}, takeover="manual-fader"))
    meta = metadata()
    meta.update(target_revision="replacement-target", state_revision="replacement-state",
                authorization_epoch="replacement-epoch")
    new = policy.calibrate([frame(t, meta) for t in range(20, 28)], meta, time_seconds=27)
    assert new is not None
    policy.accept(acceptance(new, at=27), time_seconds=27)
    policy.declare(declaration(meta, identifier="replacement", receipt=27, start=27, stop=57),
                   time_seconds=27)
    policy.rearm(27)

    results = advance(policy, 36, start=28, meta=meta, changes={"AC GUITAR": 2.0})
    assert all(not row["synthetic_controller_eligible"] for row in results
               if row["time_seconds"] < 36)
    assert all(row["gain_proposal"] is None for row in results)
    assert results[-1]["authorization_valid"] is True
    assert results[-1]["synthetic_controller_eligible"] is True
    assert results[-1]["controller_observation"]["anchor_revision"] == "replacement-target"

    if new_epoch_conflict:
        policy.declare(declaration(meta, identifier="replacement-hold", receipt=37,
                                   start=38, stop=50, mode="hold"), time_seconds=37)
        subsequent = advance(policy, 42, start=37, meta=meta, changes={"AC GUITAR": 2.0})
        assert all(row["lifecycle"] == "suspended" and row["gain_proposal"] is None
                   for row in subsequent)
        assert subsequent[-1]["loss_event"]["time_seconds"] == 38
    else:
        subsequent = advance(policy, 40, start=37, meta=meta, changes={"AC GUITAR": 2.0})
        assert subsequent[-1]["controller_decision"]["delta_db"] == -0.5
        assert subsequent[-1]["controller_decision"]["offset_db"] == -0.5


def test_receiving_a_new_epoch_without_explicit_rearm_cannot_retire_active_permission():
    policy = armed()
    advance(policy, 18, changes={"AC GUITAR": 2.0})
    meta = metadata()
    meta.update(target_revision="unaccepted-target", state_revision="unaccepted-state",
                authorization_epoch="unaccepted-epoch")
    policy.declare(declaration(meta, identifier="unaccepted-replacement", receipt=19,
                               start=20, stop=40), time_seconds=19)

    results = advance(policy, 28, start=19, changes={"AC GUITAR": 2.0})
    assert all(row["lifecycle"] == "suspended" for row in results)
    assert all(not row["synthetic_controller_eligible"] for row in results)
    assert all(row["gain_proposal"] is None for row in results)
    assert results[-1]["loss_event"]["reason"] == "conflicting_declaration"
    assert results[-1]["loss_event"]["time_seconds"] == 20


def test_raw_input_diagnostic_never_instantiates_or_calls_the_controller():
    meta = metadata(diagnostic=True)
    policy = TargetPolicy(namespace="input_domain_diagnostic")
    _, record = candidate(policy, meta)
    assert record["theta_db"] == -4.0
    results = advance(policy, 30, start=9, meta=meta, changes={"AC GUITAR": 8.0})
    assert any(row["numerical_diagnostic_valid"] for row in results)
    for row in results:
        assert row["synthetic_controller_eligible"] is False
        assert row["controller_observation"] is None
        assert row["controller_decision"] is None
        assert row["gain_proposal"] is None
        scoped_flags(row)
    assert not policy.controller_created
    assert policy.controller_calls == 0


def test_inputs_and_target_exports_are_defensively_copied():
    meta = metadata()
    supplied = [frame(t, meta) for t in range(1, 9)]
    original_meta, original_frames = copy.deepcopy(meta), copy.deepcopy(supplied)
    policy, target = candidate(meta=meta, frames=supplied)
    assert meta == original_meta and supplied == original_frames
    policy.accept(acceptance(target), time_seconds=9)
    target["theta_db"] = 100
    supplied[0]["levels"]["AC GUITAR"] = 100
    meta["baseline_faders_db"]["AC GUITAR"] = 100
    assert policy.target["theta_db"] == -4.0
    permission = declaration()
    policy.declare(permission, time_seconds=9)
    permission["expires_at"] = 1000
    policy.arm(10)
    results = advance(policy, 40)
    assert results[-1]["lifecycle"] == "suspended"


def test_every_prefix_and_hidden_intention_label_are_observationally_identical():
    def replay(stop, label, *, corrupt_future=False, missing=False):
        # Private intention belongs only to this test's scorer. It is never a
        # frame, declaration or policy input, and cannot branch their creation.
        assert isinstance(label, str) and label
        policy = armed() if not missing else candidate()[0]
        incoming = [frame(t, changes={"AC GUITAR": 2.0})
                    for t in range(11, 33)]
        if corrupt_future:
            for current in incoming:
                if current["time_seconds"] > stop:
                    current["levels"] = dict.fromkeys(ROLES, None)
        results = []
        for current in incoming:
            if current["time_seconds"] > (32 if corrupt_future else stop):
                break
            policy.feed(current)
            if current["time_seconds"] % 2 == 0:
                results.append(policy.evaluate(current["time_seconds"]))
        return [row for row in results if row["time_seconds"] <= stop]

    full = replay(32, "unwanted-gain")
    identical_accent = replay(32, "intentional-accent")
    assert full == identical_accent
    # An identical intentional accent can be attenuated: this is an unresolved
    # musical ambiguity, even though identical engineering outputs are correct.
    assert any(row["gain_proposal"] is not None for row in identical_accent)
    for stop in range(12, 33, 2):
        expected = [row for row in full if row["time_seconds"] <= stop]
        assert replay(stop, "different-hidden-truth") == expected
        assert replay(stop, "different-hidden-truth", corrupt_future=True) == expected
    absent_gain = replay(32, "unwanted-gain", missing=True)
    absent_accent = replay(32, "intentional-accent", missing=True)
    assert absent_gain == absent_accent
    assert all(row["controller_observation"] is None and row["gain_proposal"] is None
               for row in absent_gain)
    json.dumps(full, allow_nan=False)


def test_identical_missing_declarations_and_private_label_permutations_stay_unarmed():
    public_frames = [frame(t, changes={"AC GUITAR": 2.0}) for t in range(11, 33)]
    original_frames = copy.deepcopy(public_frames)
    private_associations = [
        {"stream-a": "unwanted-gain", "stream-b": "intentional-accent"},
        {"stream-a": "intentional-accent", "stream-b": "unwanted-gain"},
    ]
    observed = []
    for private_key in private_associations:
        for identifier in sorted(private_key):
            policy, record = candidate()
            policy.accept(acceptance(record), time_seconds=9)
            # Both streams intentionally have the SAME absent public declaration.
            # The private interpretation is retained by the scorer only.
            with pytest.raises(ValueError):
                policy.arm(10)
            results = []
            for current in copy.deepcopy(public_frames):
                policy.feed(current)
                if current["time_seconds"] % 2 == 0:
                    results.append(policy.evaluate(current["time_seconds"]))
            assert not policy.controller_created
            assert policy.controller_calls == 0
            assert any(row["numerical_diagnostic_valid"] for row in results)
            for row in results:
                assert not row["synthetic_controller_eligible"]
                assert row["controller_observation"] is None
                assert row["controller_decision"] is None
                assert row["gain_proposal"] is None
                assert row["lifecycle"] != "suspended"
                scoped_flags(row)
            observed.append({"private_intention": private_key[identifier],
                             "public_outputs": results})
    assert public_frames == original_frames
    assert {row["private_intention"] for row in observed} == {"unwanted-gain", "intentional-accent"}
    first = json.dumps(observed[0]["public_outputs"], sort_keys=True, allow_nan=False)
    assert all(json.dumps(row["public_outputs"], sort_keys=True, allow_nan=False) == first
               for row in observed)
