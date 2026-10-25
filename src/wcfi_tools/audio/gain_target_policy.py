"""Offline, explicitly accepted scalar targets; never a live mixing policy.

Only ``synthetic_test_only`` may construct the unchanged virtual controller.
``input_domain_diagnostic`` performs arithmetic without any controller calls.
The caller supplies declarations, not detected musical intent or host evidence.
All state is copied at receipt; future records and private fixture labels are
not accepted as decision-time evidence. No I/O capability is imported here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import deque
from statistics import median

ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")
CONTEXT_ROLES = ROLES[2:]
APPLICABILITY_KEYS = (
    "same_sources", "same_lead", "comparable_section", "comparable_arrangement",
    "comparable_dynamics",
)
SCOPE_KEYS = ("performer_assignment", "lead_reference", "section", "arrangement", "dynamic_intent")
NAMESPACES = ("synthetic_test_only", "input_domain_diagnostic")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def _identity(scope_revision, state_revision, authorization_epoch):
    encoded = json.dumps([scope_revision, state_revision, authorization_epoch], separators=(",", ":"))
    return "synthetic-target:" + hashlib.sha256(encoded.encode()).hexdigest()


def _p90(values):
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.9
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _role_map(value, predicate):
    return isinstance(value, dict) and set(value) == set(ROLES) and all(
        predicate(value[role]) for role in ROLES
    )


class TargetPolicy:
    """Receipt-aware target lifecycle with a hard synthetic-controller boundary.

    Events use a caller-supplied monotonic clock. ``feed`` receives one completed
    one-second frame; ``evaluate`` is called on even endpoints. Neither method
    runs audio or obtains host state. Initial arm and rearm are explicit and
    start eight fresh seconds. Acceptance alone never arms the controller.
    """

    def __init__(self, namespace="input_domain_diagnostic"):
        if namespace not in NAMESPACES:
            raise ValueError("unsupported offline target namespace")
        self.namespace = namespace
        self.lifecycle = "unconfigured"
        self.calibration_error = None
        self.controller_calls = 0
        self._candidate = None
        self._target = None
        self._acceptance = None
        self._controller = None
        self._clock = 0.0
        self._frames = deque(maxlen=8)
        self._last_frame_time = None
        self._last_evaluation_time = None
        self._last_bad_time = None
        self._armed_at = None
        self._armed_identity = None
        self._cycle_metadata = None
        self._loss_event = None
        self._loss_serial = 0
        self._declarations = []
        self._seen_declaration_ids = {}
        self._retired_epochs = set()

    @property
    def target(self):
        return copy.deepcopy(self._target)

    @property
    def candidate(self):
        return copy.deepcopy(self._candidate)

    @property
    def controller_created(self):
        return self._controller is not None

    def _advance(self, now):
        if not _number(now) or now < self._clock:
            raise ValueError("event time must be finite, nonnegative and monotonic")
        self._clock = now
        if self._armed_at is not None and self._loss_event is None:
            boundaries = {self._armed_at, now}
            for declaration in self._declarations:
                boundaries.update(
                    boundary for boundary in (declaration["effective_from"], declaration["expires_at"])
                    if self._armed_at <= boundary <= now
                )
            for boundary in sorted(boundaries):
                allowed, reason = self._permission_at(boundary)
                if not allowed:
                    self._suspend(reason, boundary)
                    break

    def _suspend(self, reason, now):
        if self._armed_at is None or self._loss_event is not None:
            return
        self._loss_serial += 1
        metadata = self._cycle_metadata
        epoch = metadata["authorization_epoch"] + f":suspended:{self._loss_serial}"
        self._loss_event = {"reason": reason, "time_seconds": now, "authorization_epoch": epoch}
        self._armed_identity = _identity(metadata["scope_revision"], metadata["state_revision"], epoch)
        self.lifecycle = "suspended"

    @staticmethod
    def _validate_metadata(metadata, namespace):
        if not isinstance(metadata, dict):
            raise ValueError("calibration metadata must be a mapping")
        for key in ("target_revision", "scope_revision", "state_revision", "authorization_epoch"):
            if not _identifier(metadata.get(key)):
                raise ValueError("missing calibration revision: " + key)
        for key in ("source_ids", "measurement_locations"):
            if not _role_map(metadata.get(key), _identifier):
                raise ValueError("complete source identity and measurement locations are required")
        scope = metadata.get("scope")
        if not isinstance(scope, dict) or any(not _identifier(scope.get(key)) for key in SCOPE_KEYS):
            raise ValueError("explicit calibration scope is required")
        snapshot = metadata.get("state_snapshot")
        if not isinstance(snapshot, dict) or any(snapshot.get(key) is None for key in ("chain", "routing", "sends")):
            raise ValueError("explicit chain/routing/send snapshot is required")
        if namespace == "synthetic_test_only":
            if metadata.get("measurement_domain") != "post_fx_pre_fader":
                raise ValueError("synthetic control requires post_fx_pre_fader measurement domain")
            if not _role_map(metadata.get("baseline_faders_db"), _number):
                raise ValueError("known finite baseline faders are required")
        elif metadata.get("measurement_domain") != "raw_input" or metadata.get("baseline_faders_db") is not None:
            raise ValueError("raw-input diagnostics must leave baseline faders unavailable")
        # JSON-safe snapshots have no executable objects or nonfinite metadata.
        json.dumps(metadata, allow_nan=False)

    def calibrate(self, frames, metadata, *, time_seconds):
        self._advance(time_seconds)
        if self._armed_at is not None and self._loss_event is None:
            self._suspend("state_changed", time_seconds)
        self.calibration_error = None
        self._candidate = None
        try:
            self._validate_metadata(metadata, self.namespace)
            rows = list(frames)
            if len(rows) != 8:
                raise ValueError("calibration requires exactly eight completed frames")
            endpoints = [row.get("time_seconds") for row in rows]
            if any(not isinstance(value, int) or isinstance(value, bool) for value in endpoints):
                raise ValueError("calibration endpoints must be integer seconds")
            if endpoints != list(range(endpoints[0], endpoints[0] + 8)) or endpoints[0] < 1:
                raise ValueError("calibration must be eight contiguous completed seconds")
            if endpoints[-1] > time_seconds:
                raise ValueError("future calibration is forbidden")
            for row in rows:
                if row.get("valid", True) is not True or not _role_map(row.get("levels"), _number):
                    raise ValueError("calibration contains invalid levels")
                if any(row.get(key) != metadata[key] for key in ("source_ids", "measurement_locations")):
                    raise ValueError("calibration measurement identity mismatch")
                if self.namespace == "synthetic_test_only":
                    if row.get("state_matches") is not True or any(
                        row.get(key) != metadata[key] for key in ("scope_revision", "state_revision")
                    ):
                        raise ValueError("calibration state/scope does not match the snapshot")
                    observation_age = row.get("observation_age_seconds")
                    state_age = row.get("state_age_seconds")
                    if not _number(observation_age) or not 0 <= observation_age < 0.5:
                        raise ValueError("calibration observation is stale or unverified")
                    if not _number(state_age) or not 0 <= state_age <= 2.5:
                        raise ValueError("calibration state is stale or unverified")
            levels = {role: [row["levels"][role] for row in rows] for role in ROLES}
            floors = {role: max(-60.0, _p90(values) - 24.0) for role, values in levels.items()}
            faders = metadata.get("baseline_faders_db")
            offsets = faders if faders is not None else dict.fromkeys(ROLES, 0.0)
            pairs = {}
            ratios = {}
            for role in ("AC GUITAR", *CONTEXT_ROLES):
                indices = [index for index in range(8)
                           if levels[role][index] > floors[role] and levels["RED"][index] > floors["RED"]]
                if len(indices) < 4:
                    raise ValueError("insufficient calibration paired support: " + role)
                pairs[role] = [endpoints[index] for index in indices]
                ratios[role] = median(
                    levels[role][index] + offsets[role] - levels["RED"][index] - offsets["RED"]
                    for index in indices
                )
            record = {
                "namespace": self.namespace,
                "metadata": copy.deepcopy(metadata),
                "calibration_interval": [endpoints[0] - 1, endpoints[-1]],
                "calibration_levels": levels,
                "level_summaries": {role: {"p90": _p90(values), "median": median(values),
                                            "minimum": min(values), "maximum": max(values)}
                                    for role, values in levels.items()},
                "thresholds_dbfs": floors,
                "theta_db": ratios["AC GUITAR"],
                "phi_db": {role: -ratios[role] for role in CONTEXT_ROLES},
                "paired_support": pairs,
                "created_at": time_seconds,
            }
            record["calibration_id"] = hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            self._candidate = record
            if self.namespace == "input_domain_diagnostic":
                self._target = copy.deepcopy(record)
                self.lifecycle = "diagnostic"
            elif self._loss_event is None:
                self.lifecycle = "awaiting_acceptance"
            return copy.deepcopy(record)
        except (ValueError, TypeError, KeyError) as exc:
            self.calibration_error = str(exc)
            if self._target is None:
                self.lifecycle = "calibration_unavailable"
            return None

    def accept(self, record, *, time_seconds):
        self._advance(time_seconds)
        if self.namespace != "synthetic_test_only":
            raise ValueError("diagnostic data cannot receive musical or synthetic control approval")
        if self._candidate is None or not isinstance(record, dict):
            raise ValueError("completed calibration candidate is required")
        candidate = self._candidate
        metadata = candidate["metadata"]
        if record.get("namespace") != "synthetic_test_only" or not _identifier(record.get("operator_id")):
            raise ValueError("explicit test-only operator acceptance is required")
        if record.get("calibration_id") != candidate["calibration_id"]:
            raise ValueError("acceptance does not identify the exact candidate")
        if any(record.get(key) != metadata[key] for key in ("scope_revision", "target_revision", "state_revision")):
            raise ValueError("acceptance revision mismatch")
        accepted, received = record.get("accepted_at"), record.get("received_at")
        if not _number(accepted) or not _number(received) or received != time_seconds or not (
            candidate["created_at"] <= accepted <= received
        ):
            raise ValueError("acceptance must be received now after completed calibration")
        if self._armed_at is not None and self._loss_event is None:
            self._suspend("state_changed", time_seconds)
        self._target = copy.deepcopy(candidate)
        self._acceptance = copy.deepcopy(record)
        self.lifecycle = "suspended" if self._loss_event is not None else "frozen_unarmed"
        return copy.deepcopy(record)

    @staticmethod
    def _validate_declaration(record, now):
        if not isinstance(record, dict):
            raise ValueError("declaration must be a mapping")
        allowed_keys = {
            "declaration_id", "issuer", "provenance", "scope_revision", "target_revision",
            "state_revision", "authorization_epoch", "issued_at", "received_at", "effective_from",
            "expires_at", "operator_mode", "applicability",
        }
        if set(record) != allowed_keys:
            raise ValueError("declaration accepts only the explicit public operator contract")
        for key in ("declaration_id", "issuer", "provenance", "scope_revision", "target_revision",
                    "state_revision", "authorization_epoch"):
            if not _identifier(record.get(key)):
                raise ValueError("missing declaration identity: " + key)
        times = [record.get(key) for key in ("issued_at", "received_at", "effective_from", "expires_at")]
        if not all(_number(value) and value >= 0 for value in times):
            raise ValueError("finite declaration times are required")
        issued, received, start, end = times
        if not (issued <= received == now <= start < end) or end - start > 30:
            raise ValueError("invalid declaration timing or lifetime exceeds 30 seconds")
        if record.get("operator_mode") not in ("track_accepted_ratio", "hold"):
            raise ValueError("explicit track or hold operator mode is required")
        applicability = record.get("applicability")
        if not isinstance(applicability, dict):
            raise ValueError("explicit applicability is required")
        if set(applicability) != {*APPLICABILITY_KEYS, "expected_activity", "bleed_suitable"}:
            raise ValueError("applicability accepts only the explicit public context contract")
        boolean_or_unknown = lambda value: value is True or value is False or value is None
        if any(key not in applicability or not boolean_or_unknown(applicability[key]) for key in APPLICABILITY_KEYS):
            raise ValueError("explicit source/context applicability is required")
        if any(not _role_map(applicability.get(key), boolean_or_unknown)
               for key in ("expected_activity", "bleed_suitable")):
            raise ValueError("all source activity and bleed expectations are required")
        json.dumps(record, allow_nan=False)

    def declare(self, record, *, time_seconds):
        self._advance(time_seconds)
        if isinstance(record, dict) and record.get("declaration_id") in self._seen_declaration_ids:
            previous = self._seen_declaration_ids[record["declaration_id"]]
            if record == previous:
                return copy.deepcopy(previous)  # Receipt/polling never extends lifetime.
            self._suspend("conflicting_declaration", time_seconds)
            raise ValueError("declaration identity was reused with changed content")
        try:
            self._validate_declaration(record, time_seconds)
        except (ValueError, TypeError) as exc:
            self._suspend("invalid_declaration", time_seconds)
            raise ValueError(str(exc)) from exc
        saved = copy.deepcopy(record)
        self._declarations.append(saved)
        self._seen_declaration_ids[saved["declaration_id"]] = saved
        # Future-effective hold/unknown scope does not rewrite earlier permission.
        self._advance(time_seconds)
        return copy.deepcopy(saved)

    def _permission_at(self, time_seconds, *, retiring_epoch=None):
        if self._target is None:
            return False, "missing_declaration"
        metadata = self._target["metadata"]
        records = [record for record in self._declarations
                   if record["received_at"] <= time_seconds
                   and record["effective_from"] <= time_seconds < record["expires_at"]
                   and record["authorization_epoch"] not in self._retired_epochs
                   and record["authorization_epoch"] != retiring_epoch]
        if not records:
            return False, "authorization_expired"
        signatures = set()
        for record in records:
            if any(record[key] != metadata[key] for key in (
                "scope_revision", "target_revision", "state_revision", "authorization_epoch"
            )):
                return False, "conflicting_declaration"
            signatures.add(json.dumps([record["operator_mode"], record["applicability"]], sort_keys=True))
        if len(signatures) != 1:
            return False, "conflicting_declaration"
        record = records[0]
        applicability = record["applicability"]
        allowed = record["operator_mode"] == "track_accepted_ratio" and all(
            applicability[key] is True for key in APPLICABILITY_KEYS
        ) and all(applicability[key][role] is True
                  for key in ("expected_activity", "bleed_suitable") for role in ROLES)
        return allowed, "authorized" if allowed else "declaration_not_applicable"

    def arm(self, time_seconds):
        self._advance(time_seconds)
        if self.namespace != "synthetic_test_only" or self._acceptance is None or self._target is None:
            raise ValueError("explicit received synthetic target acceptance is required")
        if self._controller is not None or self._armed_at is not None:
            raise ValueError("existing controller requires explicit accepted rearm")
        allowed, reason = self._permission_at(time_seconds)
        if not allowed:
            raise ValueError(reason)
        from .gain_controller import GainController

        metadata = self._target["metadata"]
        identity = _identity(metadata["scope_revision"], metadata["state_revision"], metadata["authorization_epoch"])
        self._controller = GainController(revision=identity, anchor_revision=metadata["target_revision"])
        self._start_cycle(time_seconds, identity)

    def _start_cycle(self, now, identity):
        self._armed_at = now
        self._armed_identity = identity
        self._cycle_metadata = copy.deepcopy(self._target["metadata"])
        self._loss_event = None
        self._frames.clear()
        self._last_bad_time = now
        self._last_frame_time = None
        self._last_evaluation_time = None
        self.lifecycle = "armed_warming"

    def rearm(self, time_seconds):
        self._advance(time_seconds)
        if self.namespace != "synthetic_test_only" or self._controller is None or self._loss_event is None:
            raise ValueError("only a suspended synthetic controller can rearm")
        metadata = self._target["metadata"]
        if self._acceptance is None or any(metadata[key] == self._cycle_metadata[key] for key in (
            "target_revision", "state_revision", "authorization_epoch"
        )) or self._target["calibration_interval"][0] < self._loss_event["time_seconds"]:
            raise ValueError("rearm requires fresh calibration, acceptance, target/state revisions and epoch")
        previous_epoch = self._cycle_metadata["authorization_epoch"]
        allowed, reason = self._permission_at(time_seconds, retiring_epoch=previous_epoch)
        if not allowed:
            raise ValueError(reason)
        identity = _identity(metadata["scope_revision"], metadata["state_revision"], metadata["authorization_epoch"])
        result = self._controller.rearm(time_seconds, identity, metadata["target_revision"])
        self.controller_calls += 1
        # Only an explicit accepted rearm retires old permission. An unapproved
        # future epoch during the old active cycle still conflicts and suspends;
        # simply receiving or accepting a replacement cannot bypass that latch.
        self._retired_epochs.add(previous_epoch)
        self._start_cycle(time_seconds, identity)
        return {"namespace": "synthetic_test_only", "controller_decision": result, "gain_proposal": None}

    def revoke(self, time_seconds, reason="revoked"):
        self._advance(time_seconds)
        self._suspend(str(reason), time_seconds)
        self._acceptance = None

    def feed(self, frame):
        if not isinstance(frame, dict):
            raise ValueError("frame must be a scalar mapping")
        now = frame.get("time_seconds")
        if not isinstance(now, int) or isinstance(now, bool) or now < 1:
            raise ValueError("completed frame endpoint must be a positive integer")
        self._advance(now)
        if self._last_frame_time is not None and now <= self._last_frame_time:
            raise ValueError("frames must have strictly increasing endpoints")
        if self._last_frame_time is not None and now != self._last_frame_time + 1:
            self._last_bad_time = now - 1
            self._frames.clear()
        self._last_frame_time = now
        saved = {key: copy.deepcopy(frame[key]) for key in (
            "time_seconds", "levels", "source_ids", "measurement_locations", "scope_revision",
            "state_revision", "state_matches", "observation_age_seconds", "state_age_seconds", "valid"
        ) if key in frame}
        if frame.get("takeover") or frame.get("revoked"):
            self._suspend("revoked" if frame.get("revoked") else "state_changed", now)
        target = self._target or self._candidate
        if target is not None and self.namespace == "synthetic_test_only":
            metadata = target["metadata"]
            if any(key in frame and frame[key] != metadata[key] for key in (
                "source_ids", "measurement_locations", "scope_revision", "state_revision"
            )) or frame.get("state_matches") is False:
                self._suspend("state_changed", now)
        saved["data_reason"] = self._data_reason(saved)
        if saved["data_reason"] is not None:
            self._last_bad_time = now
        self._frames.append(saved)

    def _data_reason(self, frame):
        if frame.get("valid", True) is not True or not _role_map(frame.get("levels"), _number):
            return "invalid_measurement"
        age = frame.get("observation_age_seconds")
        if not _number(age) or not 0 <= age < 0.5:
            return "stale_measurement"
        state_age = frame.get("state_age_seconds")
        if self.namespace == "synthetic_test_only" and (not _number(state_age) or not 0 <= state_age <= 2.5):
            return "stale_state"
        return None

    def evaluate(self, time_seconds):
        self._advance(time_seconds)
        if not isinstance(time_seconds, int) or isinstance(time_seconds, bool) or time_seconds % 2:
            raise ValueError("evaluate requires an even integer endpoint")
        if self._last_evaluation_time is not None and time_seconds <= self._last_evaluation_time:
            raise ValueError("evaluations must increase strictly")
        self._last_evaluation_time = time_seconds
        target = self._target or self._candidate
        reasons = []
        raw = None
        context_errors = {}
        paired = {}
        numerical = False
        consistent = False
        rows = list(self._frames)
        full_window = len(rows) == 8 and [row["time_seconds"] for row in rows] == list(range(time_seconds - 7, time_seconds + 1))
        if target is None:
            reasons.append("calibration_unavailable")
        elif not full_window:
            reasons.append("fresh_history_required")
        elif any(row["data_reason"] is not None for row in rows):
            reasons.extend(dict.fromkeys(row["data_reason"] for row in rows if row["data_reason"] is not None))
        else:
            floors = target["thresholds_dbfs"]
            offsets = target["metadata"]["baseline_faders_db"] or dict.fromkeys(ROLES, 0.0)
            ratios = {}
            unsupported_latest = None
            for role in ("AC GUITAR", *CONTEXT_ROLES):
                supported = [row for row in rows if row["levels"][role] > floors[role]
                             and row["levels"]["RED"] > floors["RED"]]
                paired[role] = [row["time_seconds"] for row in supported]
                if len(supported) < 4:
                    unsupported_latest = max(
                        unsupported_latest or 0, *(row["time_seconds"] for row in rows if row not in supported)
                    )
                else:
                    ratios[role] = median(row["levels"][role] + offsets[role]
                                          - row["levels"]["RED"] - offsets["RED"] for row in supported)
            if unsupported_latest is not None:
                self._last_bad_time = max(self._last_bad_time or 0, unsupported_latest)
                reasons.append("insufficient_support")
            else:
                numerical = True
                raw = ratios["AC GUITAR"] - target["theta_db"]
                context_errors = {role: -ratios[role] - target["phi_db"][role] for role in CONTEXT_ROLES}
                consistent = all(abs(value) <= 0.5 for value in context_errors.values())
                if not consistent:
                    reasons.append("context_disagreement")
                    bad_endpoints = [row["time_seconds"] for role in CONTEXT_ROLES
                                     if abs(context_errors[role]) > 0.5 for row in rows
                                     if row["time_seconds"] in paired[role]
                                     and abs(row["levels"]["RED"] + offsets["RED"]
                                             - row["levels"][role] - offsets[role]
                                             - target["phi_db"][role]) > 0.5]
                    self._last_bad_time = max(self._last_bad_time or 0, *bad_endpoints)
        contiguous = max(0, min(8, time_seconds - (self._last_bad_time or (time_seconds - len(rows)))))
        if contiguous < 8 and "fresh_history_required" not in reasons:
            reasons.append("fresh_history_required")
        state_verified = bool(full_window and target is not None and all(
            row.get("state_matches") is True
            and all(row.get(key) == target["metadata"][key] for key in (
                "scope_revision", "state_revision", "source_ids", "measurement_locations"
            )) for row in rows
        ))
        if not state_verified:
            reasons.append("state_unverified")
        permission, permission_reason = self._permission_at(time_seconds)
        authorized = bool(self._armed_at is not None and self._loss_event is None and permission)
        if self._acceptance is None:
            reasons.append("missing_approval")
        if not permission:
            reasons.append("missing_semantic_declarations" if permission_reason == "authorization_expired" else permission_reason)
        if self.namespace == "input_domain_diagnostic":
            reasons.append("missing_output_observability")
        eligible = bool(self.namespace == "synthetic_test_only" and authorized
                        and self._acceptance is not None and numerical and consistent
                        and state_verified and contiguous >= 8 and full_window
                        and time_seconds - self._armed_at >= 8)
        if self._loss_event is not None:
            reason = "suspended:" + self._loss_event["reason"]
        elif self.namespace == "input_domain_diagnostic":
            reason = "diagnostic_only" if numerical else (reasons[0] if reasons else "calibration_unavailable")
        elif self._acceptance is None:
            reason = "awaiting_acceptance" if target is not None else "calibration_unavailable"
        elif self._armed_at is None:
            reason = "not_armed"
        elif eligible:
            reason = "eligible"
            self.lifecycle = "monitoring"
        else:
            reason = reasons[0] if reasons else "fresh_history_required"
        observation = decision = proposal = None
        if self.namespace == "synthetic_test_only" and self._controller is not None:
            latest = rows[-1] if rows and rows[-1]["time_seconds"] == time_seconds else {}
            observation = {
                "namespace": "synthetic_test_only", "time_seconds": time_seconds,
                "eligible": eligible, "raw_error_db": raw if eligible else None,
                "reason": reason, "revision": self._armed_identity,
                "anchor_revision": self._controller.anchor_revision,
                "observation_age_seconds": latest.get("observation_age_seconds"),
                "state_age_seconds": latest.get("state_age_seconds"),
                "contiguous_seconds": contiguous,
            }
            decision = self._controller.step(observation)
            self.controller_calls += 1
            decision = {"namespace": "synthetic_test_only", **decision}
            if decision["action"] == "propose":
                proposal = {"namespace": "synthetic_test_only", "target": "virtual AC GUITAR",
                            "delta_db": decision["delta_db"], "offset_db": decision["offset_db"],
                            "effective_gain": decision["effective_gain"]}
        return {
            "time_seconds": time_seconds, "namespace": self.namespace, "lifecycle": self.lifecycle,
            "numerical_diagnostic_valid": numerical, "synthetic_controller_eligible": eligible,
            "musical_approval_valid": False, "live_eligible": False,
            "raw_error_db": raw, "context_errors_db": context_errors,
            "context_consistent": consistent, "paired_support": paired,
            "reason": reason, "reasons": list(dict.fromkeys(reasons)),
            "authorization_valid": authorized, "loss_event": copy.deepcopy(self._loss_event),
            "controller_observation": observation, "controller_decision": decision,
            "gain_proposal": proposal, "contiguous_seconds": contiguous,
            "target_revision": target["metadata"]["target_revision"] if target else None,
            "calibration_id": target["calibration_id"] if target else None,
        }
