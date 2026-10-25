"""Independent scalar contract checks; no host, audio, fixtures or model calls.

Authored after the public timeline freeze. Development levels intentionally
differ from the official matrix. Private interpretation stays in the scorer.
"""
from __future__ import annotations

import copy
import json

import pytest

from wcfi_tools.audio.intent_hold_gate import IntentHoldGate, pair_key, translation_batch


ROLES = ("AC GUITAR", "RED", "KIT", "BASS", "KEYS", "EGUITAR")
LEVELS = dict(zip(ROLES, (-33.0, -25.0, -30.0, -34.0, -35.0, -32.0)))
POSITIVE = "unwanted_level_departure"
PROTECT = "preserve_intended_change"


def metadata(suffix="original", *, ac_fader=0.0):
    faders = dict.fromkeys(ROLES, 0.0)
    faders["AC GUITAR"] = ac_fader
    return {
        "target_revision": "target-" + suffix,
        "scope_revision": "scope-" + suffix,
        "state_revision": "state-" + suffix,
        "authorization_epoch": "epoch-" + suffix,
        "source_ids": {role: "dev-source-" + role for role in ROLES},
        "measurement_locations": {role: "post-fx-" + role for role in ROLES},
        "measurement_domain": "post_fx_pre_fader",
        "baseline_faders_db": faders,
        "scope": {
            "performer_assignment": "development-band",
            "lead_reference": "RED",
            "section": "development-section",
            "arrangement": "six-sources",
            "dynamic_intent": "steady",
        },
        "state_snapshot": {"chain": "fixed", "routing": "fixed", "sends": "fixed"},
    }


def frame(t, meta=None, *, changes=None, **fields):
    meta = metadata() if meta is None else meta
    levels = {role: value + (changes or {}).get(role, 0.0) for role, value in LEVELS.items()}
    return {
        "time_seconds": t,
        "levels": levels,
        "source_ids": copy.deepcopy(meta["source_ids"]),
        "measurement_locations": copy.deepcopy(meta["measurement_locations"]),
        "scope_revision": meta["scope_revision"],
        "state_revision": meta["state_revision"],
        "state_matches": True,
        "observation_age_seconds": 0.0,
        "state_age_seconds": 0.0,
        **fields,
    }


def binding(candidate):
    meta = candidate["metadata"]
    return {
        "controlled_source_id": meta["source_ids"]["AC GUITAR"],
        "lead_source_id": meta["source_ids"]["RED"],
        "calibration_id": candidate["calibration_id"],
        **{key: meta[key] for key in (
            "target_revision", "scope_revision", "state_revision", "authorization_epoch"
        )},
    }


def acceptance(candidate, at=9):
    return {
        "operator_id": "synthetic-development-operator",
        "accepted_at": at,
        "received_at": at,
        "namespace": "synthetic_test_only",
        "calibration_id": candidate["calibration_id"],
        **{key: candidate["metadata"][key] for key in (
            "target_revision", "scope_revision", "state_revision"
        )},
    }


def scope(bind, *, identifier="scope0", received=9, start=10, stop=39):
    declaration = {
        "declaration_id": identifier,
        "issuer": "synthetic-development-operator",
        "provenance": "independent-public-scope",
        "issued_at": received,
        "received_at": received,
        "effective_from": start,
        "expires_at": stop,
        "operator_mode": "track_accepted_ratio",
        **{key: bind[key] for key in (
            "target_revision", "scope_revision", "state_revision", "authorization_epoch"
        )},
        "applicability": {
            "same_sources": True,
            "same_lead": True,
            "comparable_section": True,
            "comparable_arrangement": True,
            "comparable_dynamics": True,
            "expected_activity": dict.fromkeys(ROLES, True),
            "bleed_suitable": dict.fromkeys(ROLES, True),
        },
    }
    return {"binding": copy.deepcopy(bind), "declaration": declaration}


def cue(bind, *, identifier="cue0", received=9, start=10, stop=39, assertion=POSITIVE):
    return {
        "cue_id": identifier,
        "issuer": "synthetic-development-operator",
        "provenance": "independent-public-intent-instruction",
        "binding": copy.deepcopy(bind),
        "issued_at": received,
        "received_at": received,
        "effective_from": start,
        "expires_at": stop,
        "assertion": assertion,
    }


def configured(*, assertion=POSITIVE, cue_stop=39):
    gate = IntentHoldGate()
    candidate = gate.calibrate([frame(t) for t in range(1, 9)], metadata(), time_seconds=8)
    assert candidate is not None
    assert candidate["theta_db"] == -8.0
    bind = binding(candidate)
    gate.accept(acceptance(candidate), time_seconds=9)
    gate.scope(scope(bind), time_seconds=9)
    if assertion is not None:
        gate.cue(cue(bind, assertion=assertion, stop=cue_stop), time_seconds=9)
    return gate, candidate, bind


def armed():
    gate, candidate, bind = configured()
    gate.arm(10)
    return gate, candidate, bind


def advance(gate, stop, *, start=11, meta=None, changes=None, events=None):
    results = []
    for t in range(start, stop + 1):
        if t in (events or {}):
            events[t]()
        gate.feed(frame(t, meta, changes=changes))
        if t % 2 == 0:
            result = gate.evaluate(t)
            assert result["musical_approval_valid"] is False
            assert result["live_eligible"] is False
            results.append(result)
    return results


def moves(results):
    return [(row["time_seconds"], row["gain_proposal"]["offset_db"])
            for row in results if row["gain_proposal"] is not None]


def policy_calls(gate, method=None):
    return [event for event in gate.events if event["event"] == "policy_call"
            and (method is None or event["method"] == method)]


def assert_held(results, offset=0.0):
    for row in results:
        assert row["gate_phase"] == "held"
        assert not row["synthetic_controller_eligible"]
        assert row["gain_proposal"] is None
        assert row["retained_offset_db"] == offset
        if row["controller_decision"] is not None:
            assert row["controller_decision"]["delta_db"] == 0.0


@pytest.mark.parametrize("assertion", [None, "unknown", PROTECT])
def test_initial_missing_or_nonpositive_intent_preserves_acceptance_but_cannot_arm(assertion):
    gate, candidate, bind = configured(assertion=assertion, cue_stop=18)
    with pytest.raises(ValueError):
        gate.arm(10)
    rows = advance(gate, 18, changes={"AC GUITAR": 2.0})
    assert all(row["gate_phase"] == "initial_blocked" for row in rows)
    assert not gate.controller_created
    assert gate.controller_calls == 0
    assert not policy_calls(gate, "revoke")
    assert gate.target["calibration_id"] == candidate["calibration_id"]

    gate.cue(cue(bind, identifier="late-initial", received=18, start=18, stop=48),
             time_seconds=18)
    assert not gate.controller_created  # Receipt alone cannot arm.
    gate.arm(18)
    rows = advance(gate, 30, start=19, changes={"AC GUITAR": 2.0})
    assert all(not row["synthetic_controller_eligible"] for row in rows
               if row["time_seconds"] < 26)
    assert moves(rows) == [(30, -0.5)]
    assert gate.target["calibration_id"] == candidate["calibration_id"]


def test_initial_protect_interval_must_end_before_explicit_initial_arm():
    gate, _, bind = configured(assertion=PROTECT, cue_stop=20)
    gate.cue(cue(bind, identifier="positive", received=12, start=12, stop=39), time_seconds=12)
    with pytest.raises(ValueError):
        gate.arm(12)
    assert not gate.controller_created
    gate.arm(20)
    assert gate.controller_created
    assert not policy_calls(gate, "revoke")


def test_initial_state_change_invalidates_acceptance_reuse():
    gate, _, bind = configured(assertion=None)
    gate.feed(frame(11, state_revision="unaccepted-state"))
    gate.cue(cue(bind, identifier="now-positive", received=12, start=12, stop=39), time_seconds=12)
    with pytest.raises(ValueError):
        gate.arm(12)
    assert not gate.controller_created


def test_explicit_permission_still_needs_fresh_evidence_and_controller_persistence():
    gate, _, _ = armed()
    rows = advance(gate, 36, changes={"AC GUITAR": 2.0})
    assert next(row["time_seconds"] for row in rows if row["synthetic_controller_eligible"]) == 18
    assert moves(rows) == [(22, -0.5), (32, -1.0)]


@pytest.mark.parametrize("changes", [{}, {role: 2.0 for role in ROLES}, {"RED": -2.0}])
def test_a_positive_cue_is_not_itself_an_order_to_cut(changes):
    gate, _, _ = armed()
    assert moves(advance(gate, 36, changes=changes)) == []


def test_future_protection_is_causal_and_its_end_cannot_resume_tracking():
    gate, _, bind = armed()
    earlier = advance(gate, 18, changes={"AC GUITAR": 2.0})
    snapshot = copy.deepcopy(earlier)
    gate.cue(cue(bind, identifier="future-protect", received=18, start=21, stop=25,
                 assertion=PROTECT), time_seconds=18)
    assert not policy_calls(gate, "revoke")
    rows = advance(gate, 36, start=19, changes={"AC GUITAR": 2.0})
    assert rows[0]["synthetic_controller_eligible"] is True  # t20 remains authorized.
    assert_held(rows[1:])
    assert [event["time_seconds"] for event in policy_calls(gate, "revoke")] == [21]
    assert earlier == snapshot


def test_reversed_positive_and_protect_receipt_order_has_identical_future_conflict_hold():
    outcomes = []
    for reverse in (False, True):
        gate, _, bind = armed()
        advance(gate, 18, changes={"AC GUITAR": 2.0})
        messages = [
            cue(bind, identifier="batch-positive", received=18, start=21, stop=30),
            cue(bind, identifier="batch-protect", received=18, start=21, stop=30,
                assertion=PROTECT),
        ]
        for message in reversed(messages) if reverse else messages:
            gate.cue(message, time_seconds=18)
        assert not policy_calls(gate, "revoke")
        rows = advance(gate, 36, start=19, changes={"AC GUITAR": 2.0})
        assert rows[0]["synthetic_controller_eligible"] is True
        assert_held(rows[1:])
        assert all(row["intent_reason"] == "conflicting_intent" for row in rows[1:])
        assert all(row["gate_loss_time"] == 21 for row in rows[1:])
        assert [event["time_seconds"] for event in policy_calls(gate, "revoke")] == [21]
        outcomes.append(rows)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("before_evaluation", [True, False])
def test_same_clock_hold_receipt_order_does_not_rewrite_prior_virtual_moves(before_evaluation):
    gate, _, bind = armed()
    advance(gate, 20, changes={"AC GUITAR": 2.0})
    gate.feed(frame(21, changes={"AC GUITAR": 2.0}))
    protective = cue(bind, identifier="same-clock", received=22, start=22, stop=30,
                     assertion=PROTECT)
    if before_evaluation:
        gate.cue(protective, time_seconds=22)
    gate.feed(frame(22, changes={"AC GUITAR": 2.0}))
    result = gate.evaluate(22)
    immutable_result = copy.deepcopy(result)
    if not before_evaluation:
        gate.cue(protective, time_seconds=22)
        assert moves([result]) == [(22, -0.5)]
    else:
        assert result["gain_proposal"] is None
    expected = 0.0 if before_evaluation else -0.5
    assert_held(advance(gate, 36, start=23), expected)
    assert result == immutable_result


def test_hold_retains_offset_and_blocks_release_when_raw_levels_return():
    gate, _, bind = armed()
    assert moves(advance(gate, 22, changes={"AC GUITAR": 2.0})) == [(22, -0.5)]
    gate.cue(cue(bind, identifier="protect", received=23, start=23, stop=27,
                 assertion=PROTECT), time_seconds=23)
    gate.cue(cue(bind, identifier="positive-again", received=24, start=24, stop=39),
             time_seconds=24)
    with pytest.raises(ValueError):
        gate.arm(24)  # A post-arm loss cannot use the less restrictive initial path.
    with pytest.raises(ValueError):
        gate.rearm(24)
    assert_held(advance(gate, 38, start=24), -0.5)


@pytest.mark.parametrize("operation", ["scope", "accept", "calibrate"])
def test_every_clock_advancing_call_drains_older_hold_before_payload_validation(operation):
    gate, _, bind = armed()
    advance(gate, 18)
    gate.cue(cue(bind, identifier="pending", received=18, start=19, stop=25,
                 assertion=PROTECT), time_seconds=18)
    try:
        if operation == "scope":
            gate.scope(scope(bind, identifier="next-scope", received=20, start=20, stop=45),
                       time_seconds=20)
        elif operation == "accept":
            gate.accept({}, time_seconds=20)
        else:
            gate.calibrate([], metadata(), time_seconds=20)
    except ValueError as exc:
        assert "monotonic" not in str(exc).lower()
    calls = policy_calls(gate)
    boundary = next(index for index, event in enumerate(calls)
                    if event["method"] == "revoke" and event["time_seconds"] == 19)
    assert all(index > boundary for index, event in enumerate(calls)
               if event["time_seconds"] == 20)
    assert_held([gate.evaluate(20)])


def test_all_overlapping_pairs_are_emitted_once_with_independent_provenance():
    gate, _, bind = armed()
    original_scope = scope(bind)
    new_scope = scope(bind, identifier="scope1", received=13, start=15, stop=39)
    new_cue = cue(bind, identifier="cue1", received=13, start=17, stop=32)
    original_inputs = copy.deepcopy((new_scope, new_cue))
    gate.scope(new_scope, time_seconds=13)
    gate.cue(new_cue, time_seconds=13)
    entries = gate.declarations
    declarations = [entry["declaration"] for entry in entries]
    assert len(entries) == 4
    assert [(row["effective_from"], row["expires_at"]) for row in declarations] == [
        (10, 39), (15, 39), (17, 32), (17, 32),
    ]
    provenance = [entry["provenance"] for entry in entries]
    assert [(row["scope_id"], row["cue_id"]) for row in provenance] == [
        ("scope0", "cue0"), ("scope1", "cue0"), ("scope0", "cue1"), ("scope1", "cue1"),
    ]
    assert len({row["declaration_id"] for row in declarations}) == 4
    for row, origin in zip(declarations[1:], provenance[1:]):
        assert row["issued_at"] == row["received_at"] == 13
        assert origin["binding"] == bind
        assert origin["gate_generated"] is True
        assert row["applicability"] == original_scope["declaration"]["applicability"]
        assert origin["scope_received_at"] == (9 if origin["scope_id"] == "scope0" else 13)
        assert origin["cue_received_at"] == (9 if origin["cue_id"] == "cue0" else 13)
    gate.scope(copy.deepcopy(new_scope), time_seconds=14)
    gate.cue(copy.deepcopy(new_cue), time_seconds=14)
    assert gate.declarations == entries
    assert (new_scope, new_cue) == original_inputs


def test_translation_batch_rejects_delayed_pair_atomically_and_cannot_mutate_inputs():
    _, candidate, bind = configured()
    scopes = [scope(bind), scope(bind, identifier="scope1", received=13, start=15, stop=39)]
    cues = [cue(bind), cue(bind, identifier="cue1", received=13, start=17, stop=32)]
    emitted = {pair_key(bind, "scope0", "cue0")}
    snapshot = copy.deepcopy((bind, scopes, cues, emitted, candidate))
    with pytest.raises(ValueError):
        translation_batch(bind, scopes, cues, emitted, 19, new_cue_id="cue1")
    assert (bind, scopes, cues, emitted, candidate) == snapshot


def test_translation_ignores_expired_and_nonoverlapping_historical_positive_records():
    _, _, bind = configured()
    scopes = [scope(bind),
              scope(bind, identifier="future", received=38, start=50, stop=65),
              scope(bind, identifier="overlap", received=38, start=40, stop=68)]
    incoming = cue(bind, identifier="new", received=40, start=40, stop=45)
    translated = translation_batch(bind, scopes, [incoming], set(), 40, new_cue_id="new")
    assert [(entry["declaration"]["effective_from"], entry["declaration"]["expires_at"])
            for entry in translated] == [(40, 45)]
    assert translated[0]["provenance"]["scope_id"] == "overlap"


@pytest.mark.parametrize("value", [False, None])
def test_denying_scope_cannot_be_upgraded_by_a_positive_cue_or_existing_coverage(value):
    gate, _, bind = armed()
    record = scope(bind, identifier="denying", received=13, start=15, stop=39)
    record["declaration"]["applicability"]["comparable_dynamics"] = value
    before = copy.deepcopy(record)
    gate.scope(record, time_seconds=13)
    assert len(gate.declarations) == 1
    assert not policy_calls(gate, "revoke")
    assert_held([gate.evaluate(16)])
    assert policy_calls(gate, "revoke")[0]["time_seconds"] == 15
    assert record == before


def test_asynchronous_renewals_use_coverage_union_and_preserve_initial_arm_evidence():
    gate, candidate, bind = configured(assertion=None)
    rows = []
    for t in range(11, 57):
        if t == 12:
            gate.cue(cue(bind, identifier="unknown", received=t, start=t, stop=18,
                         assertion="unknown"), time_seconds=t)
        if t in (18, 38):
            gate.cue(cue(bind, identifier=f"cue-{t}", received=t, start=t, stop=t + 30),
                     time_seconds=t)
        if t in (28, 48):
            gate.scope(scope(bind, identifier=f"scope-{t}", received=t, start=t, stop=t + 30),
                       time_seconds=t)
        if t == 18:
            assert not gate.controller_created
            gate.arm(t)
        gate.feed(frame(t, changes={"AC GUITAR": 2.0}))
        if t % 2 == 0:
            rows.append(gate.evaluate(t))
    assert moves(rows) == [(30, -0.5), (40, -1.0), (50, -1.5)]
    assert not policy_calls(gate, "revoke")
    assert gate.target["calibration_id"] == candidate["calibration_id"]
    assert all(row["synthetic_controller_eligible"] for row in rows
               if row["time_seconds"] >= 26)


def test_true_coverage_gap_at_receipt_boundary_cannot_be_retroactively_renewed():
    gate, _, bind = configured(cue_stop=29)
    gate.arm(10)
    advance(gate, 28, changes={"AC GUITAR": 2.0})
    gate.cue(cue(bind, identifier="too-late", received=29, start=29, stop=59), time_seconds=29)
    assert_held(advance(gate, 36, start=29, changes={"AC GUITAR": 2.0}), -0.5)
    assert policy_calls(gate, "revoke")[0]["time_seconds"] == 29


@pytest.mark.parametrize("assertion", [PROTECT, "unknown", POSITIVE])
def test_late_cue_cannot_rewrite_the_interval_before_actual_receipt(assertion):
    gate, _, bind = armed()
    earlier = advance(gate, 22, changes={"AC GUITAR": 2.0})
    frozen_earlier = copy.deepcopy(earlier)
    message = cue(bind, identifier="late", received=23, start=21, stop=30,
                  assertion=assertion)
    message["issued_at"] = 21
    if assertion == POSITIVE:
        with pytest.raises(ValueError):
            gate.cue(message, time_seconds=23)
    else:
        gate.cue(message, time_seconds=23)
    assert policy_calls(gate, "revoke")[0]["time_seconds"] == 23
    assert_held(advance(gate, 36, start=23), -0.5)
    assert earlier == frozen_earlier


@pytest.mark.parametrize("kind", ["cue_none", "cue_list", "cue_id_list", "cue_id_dict",
                                 "scope_none", "scope_list", "declaration_none", "scope_id_list"])
def test_malformed_message_shapes_fail_closed_with_value_error(kind):
    gate, _, bind = armed()
    if kind.startswith("cue"):
        message = cue(bind, identifier="bad", received=12, start=12, stop=30)
        if kind == "cue_none":
            message = None
        elif kind == "cue_list":
            message = []
        else:
            message["cue_id"] = [] if kind == "cue_id_list" else {}
        method = gate.cue
    else:
        message = scope(bind, identifier="bad", received=12, start=12, stop=30)
        if kind == "scope_none":
            message = None
        elif kind == "scope_list":
            message = []
        elif kind == "declaration_none":
            message["declaration"] = None
        else:
            message["declaration"]["declaration_id"] = []
        method = gate.scope
    with pytest.raises(ValueError):
        method(message, time_seconds=12)
    assert_held([gate.evaluate(12)])
    assert policy_calls(gate, "revoke")[0]["time_seconds"] == 12


@pytest.mark.parametrize("where", ["cue", "binding", "scope", "declaration", "applicability"])
def test_private_truth_is_rejected_from_public_intent_and_scope_contracts(where):
    gate, _, bind = armed()
    if where in ("cue", "binding"):
        record = cue(bind, identifier="extra", received=12, start=12, stop=30)
        destination = record if where == "cue" else record["binding"]
        method = gate.cue
    else:
        record = scope(bind, identifier="extra", received=12, start=12, stop=30)
        destination = (record if where == "scope" else record["declaration"]
                       if where == "declaration" else record["declaration"]["applicability"])
        method = gate.scope
    destination["private_fault_truth"] = "this field is not an operator instruction"
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        method(record, time_seconds=12)
    assert record == before
    assert_held([gate.evaluate(12)])


@pytest.mark.parametrize("field,value", [
    ("expires_at", None), ("expires_at", float("nan")), ("expires_at", float("inf")),
    ("expires_at", True), ("expires_at", 43), ("received_at", 11), ("received_at", 13),
    ("issued_at", 13), ("effective_from", 30), ("assertion", "track_accepted_ratio"),
])
def test_cue_timing_and_assertion_boundaries_cannot_be_coerced(field, value):
    gate, _, bind = armed()
    record = cue(bind, identifier="boundary", received=12, start=12, stop=30)
    record[field] = value
    with pytest.raises(ValueError):
        gate.cue(record, time_seconds=12)
    assert_held([gate.evaluate(12)])


def test_thirty_second_cue_bound_starts_at_issue_not_only_effective_time():
    gate, _, bind = armed()
    record = cue(bind, identifier="old-assertion", received=12, start=12, stop=38)
    record["issued_at"] = 7  # Interval length26, but assertion lifetime31.
    with pytest.raises(ValueError):
        gate.cue(record, time_seconds=12)
    assert_held([gate.evaluate(12)])


def test_duplicate_poll_preserves_receipt_but_changed_id_content_latches_hold():
    gate, _, bind = armed()
    original = cue(bind)
    declarations = gate.declarations
    gate.cue(copy.deepcopy(original), time_seconds=12)
    assert gate.declarations == declarations
    assert not policy_calls(gate, "revoke")
    changed = copy.deepcopy(original)
    changed["expires_at"] = 38
    with pytest.raises(ValueError):
        gate.cue(changed, time_seconds=13)
    assert gate.declarations == declarations
    assert_held([gate.evaluate(14)])


@pytest.mark.parametrize("method", ["cue", "scope", "accept", "calibrate"])
def test_unrecognized_recovery_context_cannot_bypass_active_fail_closed_behavior(method):
    gate, _, bind = armed()
    with pytest.raises(ValueError):
        if method == "cue":
            gate.cue(cue(bind, identifier="fake-recovery", received=12, start=12, stop=30),
                     time_seconds=12, context="recovery")
        elif method == "scope":
            gate.scope(scope(bind, identifier="fake-recovery", received=12, start=12, stop=30),
                       time_seconds=12, context="recovery")
        elif method == "accept":
            gate.accept({}, time_seconds=12, context="recovery")
        else:
            gate.calibrate([], metadata(), time_seconds=12, context="recovery")
    assert_held([gate.evaluate(12)])


@pytest.mark.parametrize("flag", ["takeover", "revoked"])
def test_prearm_takeover_or_revoke_invalidates_unchanged_identity_acceptance(flag):
    gate, _, bind = configured(assertion=None)
    gate.feed(frame(11, **{flag: True}))
    gate.cue(cue(bind, identifier="positive", received=12, start=12, stop=30), time_seconds=12)
    with pytest.raises(ValueError):
        gate.arm(12)
    assert not gate.controller_created


def test_repeated_initial_continuity_failure_preserves_first_loss_for_new_calibration():
    gate, _, _ = configured(assertion=None)
    changed = metadata("changed-before-first-arm")
    for t in range(11, 21):
        gate.feed(frame(t, changed))
    acquired = [frame(t, changed) for t in range(12, 20)]
    candidate = gate.calibrate(acquired, changed, time_seconds=20)
    assert candidate is not None
    gate.accept(acceptance(candidate, at=20), time_seconds=20)
    bind = binding(candidate)
    gate.scope(scope(bind, identifier="new-state", received=20, start=20, stop=50), time_seconds=20)
    gate.cue(cue(bind, identifier="new-state", received=20, start=20, stop=50), time_seconds=20)
    gate.arm(20)
    assert gate.controller_created
    assert moves(advance(gate, 32, start=21, meta=changed, changes={"AC GUITAR": 2.0})) == [(32, -0.5)]


def test_fresh_initial_arm_after_continuity_loss_is_not_blocked_by_obsolete_positive_lease():
    gate, _, old_binding = configured()  # Positive derived lease is [10,39).
    original_audit = gate.declarations
    assert len(original_audit) == 1
    assert original_audit[0]["declaration"]["effective_from"] == 10
    assert original_audit[0]["declaration"]["expires_at"] == 39
    assert not gate.controller_created
    changed = metadata("fresh-initial-after-positive")
    gate.feed(frame(11, changed))
    acquired = [frame(t, changed) for t in range(12, 20)]
    for current in acquired:
        gate.feed(current)
    candidate = gate.calibrate(acquired, changed, time_seconds=20)
    assert candidate is not None
    gate.accept(acceptance(candidate, at=20), time_seconds=20)
    new_binding = binding(candidate)
    assert new_binding != old_binding
    gate.scope(scope(new_binding, identifier="fresh-initial-scope", received=20,
                     start=20, stop=50), time_seconds=20)
    gate.cue(cue(new_binding, identifier="fresh-initial-cue", received=20,
                 start=20, stop=50), time_seconds=20)
    assert gate.declarations[0] == original_audit[0]
    gate.arm(20)
    assert gate.controller_created
    rows = advance(gate, 42, start=21, meta=changed, changes={"AC GUITAR": 2.0})
    assert all(not row["synthetic_controller_eligible"] for row in rows
               if row["time_seconds"] < 28)
    assert rows[3]["time_seconds"] == 28 and rows[3]["synthetic_controller_eligible"]
    assert moves(rows) == [(32, -0.5), (42, -1.0)]
    assert all(row["gate_phase"] == "monitoring" for row in rows)
    assert gate.declarations[0] == original_audit[0]


@pytest.mark.parametrize("field,value", [
    ("same_sources", False), ("same_lead", None), ("comparable_section", False),
    ("comparable_arrangement", None), ("comparable_dynamics", None),
])
def test_declared_scope_continuity_loss_before_first_arm_cannot_reuse_old_acceptance(field, value):
    gate, _, bind = configured(assertion=None)
    changed = scope(bind, identifier="scope-loss", received=11, start=12, stop=14)
    changed["declaration"]["applicability"][field] = value
    gate.scope(changed, time_seconds=11)
    assert not policy_calls(gate, "revoke")
    advance(gate, 16, start=11)
    gate.cue(cue(bind, identifier="positive-after-loss", received=16, start=16, stop=39),
             time_seconds=16)
    with pytest.raises(ValueError):
        gate.arm(16)
    assert not gate.controller_created
    assert policy_calls(gate, "revoke")[0]["time_seconds"] == 12


def test_native_first_suspension_and_old_cue_expiry_cannot_erase_staged_acceptance():
    gate, _, old_binding = armed()
    advance(gate, 16)
    gate.feed(frame(17, state_matches=False))
    assert not policy_calls(gate, "revoke")  # The native state-loss path suspended first.
    new_meta = metadata("staged-native-first")
    gate.prepare_recovery(new_meta, time_seconds=18)
    acquired = []
    for t in range(19, 27):
        current = frame(t, new_meta)
        acquired.append(current)
        gate.feed(current)
        if t % 2 == 0:
            assert_held([gate.evaluate(t)])
    new = gate.calibrate(acquired, new_meta, time_seconds=26, context="recovery")
    assert new is not None
    gate.accept(acceptance(new, at=26), time_seconds=26, context="recovery")
    new_binding = binding(new)
    assert gate.binding == old_binding
    assert gate.staged_binding == new_binding
    gate.scope(scope(new_binding, identifier="new-scope", received=27, start=41, stop=56),
               time_seconds=27, context="recovery")
    gate.cue(cue(new_binding, identifier="new-cue", received=27, start=41, stop=56),
             time_seconds=27, context="recovery")
    events = {29: lambda: gate.cue(cue(old_binding, identifier="old-protect", received=29,
                                      start=30, stop=35, assertion=PROTECT), time_seconds=29)}
    assert_held(advance(gate, 40, start=27, meta=new_meta, events=events))
    assert not policy_calls(gate, "revoke")
    assert gate.binding == old_binding
    gate.rearm(41)
    assert gate.binding == new_binding
    rows = advance(gate, 50, start=42, meta=new_meta)
    assert rows[-1]["synthetic_controller_eligible"] is True
    assert moves(rows) == []


def test_rearm_before_old_lease_expiry_rebases_retained_offset_without_restoring_gain():
    gate, _, old_binding = armed()
    advance(gate, 22, changes={"AC GUITAR": 2.0})
    gate.cue(cue(old_binding, identifier="hold", received=23, start=23, stop=28,
                 assertion=PROTECT), time_seconds=23)
    new_meta = metadata("retained-baseline", ac_fader=-0.5)
    gate.prepare_recovery(new_meta, time_seconds=24)
    acquired = [frame(t, new_meta) for t in range(25, 33)]
    for current in acquired:
        gate.feed(current)
        if current["time_seconds"] % 2 == 0:
            assert_held([gate.evaluate(current["time_seconds"])], -0.5)
    new = gate.calibrate(acquired, new_meta, time_seconds=32, context="recovery")
    assert new is not None and new["theta_db"] == -8.5
    gate.accept(acceptance(new, at=32), time_seconds=32, context="recovery")
    new_binding = binding(new)
    gate.scope(scope(new_binding, identifier="new-scope", received=33, start=33, stop=63),
               time_seconds=33, context="recovery")
    gate.cue(cue(new_binding, identifier="new-cue", received=33, start=33, stop=63),
             time_seconds=33, context="recovery")
    declared_before = gate.declarations
    assert gate.binding == old_binding
    result = gate.rearm(33)
    assert result["gain_proposal"] is None
    assert gate.declarations == declared_before  # Promotion cannot re-emit pairs.
    assert gate.binding == new_binding
    rows = advance(gate, 44, start=34, meta=new_meta)
    assert all(not row["synthetic_controller_eligible"] for row in rows
               if row["time_seconds"] < 42)
    assert rows[-1]["synthetic_controller_eligible"] is True
    assert moves(rows) == []
    assert all(row["retained_offset_db"] == 0.0 for row in rows)
    assert gate.target["metadata"]["baseline_faders_db"]["AC GUITAR"] == -0.5


@pytest.mark.parametrize("bad_rebase", ["restored_ac", "changed_bass"])
def test_recovery_cannot_hide_restoration_or_change_other_baseline_faders(bad_rebase):
    gate, _, bind = armed()
    advance(gate, 22, changes={"AC GUITAR": 2.0})
    gate.cue(cue(bind, identifier="hold", received=23, start=23, stop=28,
                 assertion=PROTECT), time_seconds=23)
    new_meta = metadata("bad-rebase", ac_fader=-0.5)
    if bad_rebase == "restored_ac":
        new_meta["baseline_faders_db"]["AC GUITAR"] = 0.0
    else:
        new_meta["baseline_faders_db"]["BASS"] = -0.5
    with pytest.raises(ValueError):
        gate.prepare_recovery(new_meta, time_seconds=24)
    assert_held([gate.evaluate(24)], -0.5)
    assert gate.binding == bind


@pytest.mark.parametrize("flag", ["takeover", "revoked"])
def test_staged_acquisition_takeover_invalidates_preparation_even_when_ids_match(flag):
    gate, _, bind = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("acquisition-loss")
    gate.prepare_recovery(new_meta, time_seconds=18)
    supplied = []
    for t in range(19, 27):
        current = frame(t, new_meta, **({flag: True} if t == 21 else {}))
        supplied.append(current)
        gate.feed(current)
    with pytest.raises(ValueError):
        gate.calibrate(supplied, new_meta, time_seconds=26, context="recovery")
    with pytest.raises(ValueError):
        gate.rearm(26)
    assert gate.binding == bind
    assert_held([gate.evaluate(26)])


def test_recovery_calibration_requires_actual_new_state_acquisition_not_renamed_rows():
    gate, _, _ = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("renamed")
    gate.prepare_recovery(new_meta, time_seconds=18)
    for t in range(19, 27):
        gate.feed(frame(t))  # These actual frames still identify the old state.
    renamed = [frame(t, new_meta) for t in range(19, 27)]
    with pytest.raises(ValueError):
        gate.calibrate(renamed, new_meta, time_seconds=26, context="recovery")
    assert_held([gate.evaluate(26)])


def test_recovery_requires_all_eight_frames_to_follow_preparation():
    gate, _, _ = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("early-frame")
    gate.prepare_recovery(new_meta, time_seconds=19)
    for t in range(20, 27):
        gate.feed(frame(t, new_meta))
    # Endpoint19 describes [18,19), before preparation, and was never acquired.
    with pytest.raises(ValueError):
        gate.calibrate([frame(t, new_meta) for t in range(19, 27)], new_meta,
                       time_seconds=26, context="recovery")
    assert_held([gate.evaluate(26)])


def test_staged_denial_blocks_rearm_and_does_not_retire_the_active_binding():
    gate, _, old_binding = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("denied-recovery")
    gate.prepare_recovery(new_meta, time_seconds=18)
    frames = [frame(t, new_meta) for t in range(19, 27)]
    for current in frames:
        gate.feed(current)
    new = gate.calibrate(frames, new_meta, time_seconds=26, context="recovery")
    gate.accept(acceptance(new, at=26), time_seconds=26, context="recovery")
    bind = binding(new)
    gate.scope(scope(bind, identifier="new-scope", received=27, start=28, stop=57),
               time_seconds=27, context="recovery")
    gate.cue(cue(bind, identifier="new-positive", received=27, start=28, stop=57),
             time_seconds=27, context="recovery")
    gate.cue(cue(bind, identifier="new-protect", received=27, start=28, stop=35,
                 assertion=PROTECT), time_seconds=27, context="recovery")
    with pytest.raises(ValueError):
        gate.rearm(28)
    assert gate.binding == old_binding
    assert_held([gate.evaluate(28)])


def test_invalid_replacement_preparation_cannot_leave_previous_staged_acceptance_usable():
    gate, _, old_binding = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("previously-valid-preparation")
    gate.prepare_recovery(new_meta, time_seconds=18)
    acquired = [frame(t, new_meta) for t in range(19, 27)]
    for current in acquired:
        gate.feed(current)
    new = gate.calibrate(acquired, new_meta, time_seconds=26, context="recovery")
    gate.accept(acceptance(new, at=26), time_seconds=26, context="recovery")
    bind = binding(new)
    gate.scope(scope(bind, identifier="new-scope", received=27, start=28, stop=57),
               time_seconds=27, context="recovery")
    gate.cue(cue(bind, identifier="new-cue", received=27, start=28, stop=57),
             time_seconds=27, context="recovery")
    invalid_replacement = metadata("invalid-replacement")
    invalid_replacement["baseline_faders_db"] = None
    with pytest.raises(ValueError):
        gate.prepare_recovery(invalid_replacement, time_seconds=27)
    with pytest.raises(ValueError):
        gate.rearm(28)
    assert gate.binding == old_binding
    assert_held([gate.evaluate(28)])


@pytest.mark.parametrize("message", [
    "staged_protect", "staged_unknown", "staged_scope_denial",
    "unknown_binding", "malformed",
])
def test_rejected_default_context_message_cannot_leave_staged_acceptance_usable(message):
    gate, _, old_binding = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("misrouted-recovery")
    gate.prepare_recovery(new_meta, time_seconds=18)
    acquired = [frame(t, new_meta) for t in range(19, 27)]
    for current in acquired:
        gate.feed(current)
    new = gate.calibrate(acquired, new_meta, time_seconds=26, context="recovery")
    gate.accept(acceptance(new, at=26), time_seconds=26, context="recovery")
    bind = binding(new)
    gate.scope(scope(bind, identifier="new-scope", received=27, start=28, stop=57),
               time_seconds=27, context="recovery")
    gate.cue(cue(bind, identifier="new-positive", received=27, start=28, stop=57),
             time_seconds=27, context="recovery")
    if message == "staged_scope_denial":
        rejected = scope(bind, identifier="misrouted-denial", received=27, start=28, stop=35)
        rejected["declaration"]["applicability"]["comparable_dynamics"] = False
        with pytest.raises(ValueError):
            gate.scope(rejected, time_seconds=27)  # Default context is deliberately wrong.
    else:
        rejected = cue(bind, identifier="misrouted-cue", received=27, start=28, stop=35,
                       assertion="unknown" if message == "staged_unknown" else PROTECT)
        if message == "unknown_binding":
            rejected["binding"]["authorization_epoch"] = "neither-active-nor-staged"
        elif message == "malformed":
            rejected = None
        with pytest.raises(ValueError):
            gate.cue(rejected, time_seconds=27)
    with pytest.raises(ValueError):
        gate.rearm(28)
    assert gate.binding == old_binding
    assert_held([gate.evaluate(28)])


def test_explicit_revoke_invalidates_an_accepted_pending_recovery_stage():
    gate, _, old_binding = armed()
    advance(gate, 16)
    gate.revoke(17)
    new_meta = metadata("explicitly-revoked-stage")
    gate.prepare_recovery(new_meta, time_seconds=18)
    acquired = [frame(t, new_meta) for t in range(19, 27)]
    for current in acquired:
        gate.feed(current)
    new = gate.calibrate(acquired, new_meta, time_seconds=26, context="recovery")
    gate.accept(acceptance(new, at=26), time_seconds=26, context="recovery")
    bind = binding(new)
    gate.scope(scope(bind, identifier="new-scope", received=27, start=28, stop=57),
               time_seconds=27, context="recovery")
    gate.cue(cue(bind, identifier="new-cue", received=27, start=28, stop=57),
             time_seconds=27, context="recovery")
    gate.revoke(27, reason="operator_cancelled_recovery")
    with pytest.raises(ValueError):
        gate.rearm(28)
    assert gate.binding == old_binding
    assert_held([gate.evaluate(28)])


def test_abandoned_staged_lease_blocks_later_epoch_rearm_without_changing_active_state():
    """An abandoned native lease remains a conservative recovery limitation."""
    gate, _, active_binding = armed()
    advance(gate, 16)
    gate.revoke(18)
    abandoned_meta = metadata("abandoned-stage-e1")
    gate.prepare_recovery(abandoned_meta, time_seconds=19)
    abandoned_frames = [frame(t, abandoned_meta) for t in range(20, 28)]
    for current in abandoned_frames:
        gate.feed(current)
    abandoned = gate.calibrate(abandoned_frames, abandoned_meta, time_seconds=27,
                               context="recovery")
    gate.accept(acceptance(abandoned, at=27), time_seconds=27, context="recovery")
    abandoned_binding = binding(abandoned)
    gate.scope(scope(abandoned_binding, identifier="abandoned-scope", received=27,
                     start=27, stop=57), time_seconds=27, context="recovery")
    gate.cue(cue(abandoned_binding, identifier="abandoned-cue", received=27,
                 start=27, stop=57), time_seconds=27, context="recovery")
    abandoned_declarations = gate.declarations
    gate.revoke(28, reason="operator_abandoned_first_recovery")

    replacement_meta = metadata("replacement-stage-e2")
    gate.prepare_recovery(replacement_meta, time_seconds=28)
    replacement_frames = [frame(t, replacement_meta) for t in range(29, 37)]
    for current in replacement_frames:
        gate.feed(current)
    replacement = gate.calibrate(replacement_frames, replacement_meta, time_seconds=36,
                                 context="recovery")
    gate.accept(acceptance(replacement, at=36), time_seconds=36, context="recovery")
    replacement_binding = binding(replacement)
    gate.scope(scope(replacement_binding, identifier="replacement-scope", received=37,
                     start=37, stop=67), time_seconds=37, context="recovery")
    gate.cue(cue(replacement_binding, identifier="replacement-cue", received=37,
                 start=37, stop=67), time_seconds=37, context="recovery")
    with pytest.raises(ValueError, match="conflicting_declaration"):
        gate.rearm(37)
    assert gate.binding == active_binding
    assert gate.binding != replacement_binding
    assert gate.declarations[:len(abandoned_declarations)] == abandoned_declarations
    assert_held([gate.evaluate(38)], 0.0)
    # No retry is implied: the accepted native policy retains E1's unexpired lease.


def test_numerical_fault_requires_fresh_history_but_does_not_create_intent_loss():
    gate, _, _ = armed()
    rows = []
    for t in range(11, 37):
        gate.feed(frame(t, changes={"AC GUITAR": 2.0},
                        observation_age_seconds=1.0 if t == 19 else 0.0))
        if t % 2 == 0:
            rows.append(gate.evaluate(t))
    assert not policy_calls(gate, "revoke")
    assert all(row["gate_phase"] == "monitoring" for row in rows)
    assert all(not row["synthetic_controller_eligible"] for row in rows
               if 20 <= row["time_seconds"] < 28)
    assert moves(rows) == [(32, -0.5)]


def test_input_records_and_exported_state_are_defensive_copies():
    gate = IntentHoldGate()
    meta = metadata()
    frames = [frame(t, meta) for t in range(1, 9)]
    before = copy.deepcopy((meta, frames))
    candidate = gate.calibrate(frames, meta, time_seconds=8)
    assert (meta, frames) == before
    gate.accept(acceptance(candidate), time_seconds=9)
    bind = binding(candidate)
    scope_record, cue_record = scope(bind), cue(bind)
    before = copy.deepcopy((scope_record, cue_record))
    gate.scope(scope_record, time_seconds=9)
    gate.cue(cue_record, time_seconds=9)
    assert (scope_record, cue_record) == before
    candidate["theta_db"] = 999
    meta["baseline_faders_db"]["AC GUITAR"] = 999
    frames[0]["levels"]["AC GUITAR"] = 999
    scope_record["declaration"]["expires_at"] = 999
    cue_record["expires_at"] = 999
    exported = gate.declarations
    exported[0]["declaration"]["expires_at"] = 999
    exported[0]["provenance"]["binding"]["target_revision"] = "changed"
    exported_binding = gate.binding
    exported_binding["target_revision"] = "changed"
    assert gate.target["theta_db"] == -8.0
    assert gate.binding == bind
    assert gate.declarations[0]["declaration"]["expires_at"] == 39
    gate.arm(10)
    rows = advance(gate, 40)
    assert rows[-1]["gate_phase"] == "held"
    assert policy_calls(gate, "revoke")[0]["time_seconds"] == 39


def test_prefixes_and_external_private_label_permutations_do_not_change_decisions():
    public_frames = [frame(t, changes={"AC GUITAR": 2.0}) for t in range(11, 37)]
    frozen_frames = copy.deepcopy(public_frames)

    def replay(end, *, corrupt_suffix=False):
        gate, _, _ = armed()
        incoming = copy.deepcopy(public_frames)
        if corrupt_suffix:
            for current in incoming:
                if current["time_seconds"] > end:
                    current["levels"] = dict.fromkeys(ROLES, None)
        rows = []
        for current in incoming:
            if current["time_seconds"] > end and not corrupt_suffix:
                break
            gate.feed(current)
            if current["time_seconds"] % 2 == 0:
                row = gate.evaluate(current["time_seconds"])
                if current["time_seconds"] <= end:
                    rows.append(row)
        return rows

    full = replay(36)
    associations = [
        {"first": "unwanted", "second": "intentional"},
        {"first": "intentional", "second": "unwanted"},
    ]
    for external_labels in associations:
        # Labels belong to this evaluator record, never any gate input.
        records = [(external_labels[name], replay(36)) for name in ("first", "second")]
        assert records[0][1] == records[1][1] == full
    assert moves(full) == [(22, -0.5), (32, -1.0)]  # Intentional accents can still be cut.
    for end in range(12, 37, 2):
        expected = [row for row in full if row["time_seconds"] <= end]
        assert replay(end) == expected
        assert replay(end, corrupt_suffix=True) == expected
    assert public_frames == frozen_frames
    assert "private" not in json.dumps(public_frames)
