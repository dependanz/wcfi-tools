"""Synthetic-only intent HOLD around the unchanged accepted target policy.

Intent is an explicit operator assertion, never inferred from scalar levels.
Only public, received, correctly bound records can create permission. There is
no application queue, host command, automatic rearm or automatic rebaseline.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import deque

from .gain_target_policy import TargetPolicy

ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")
BINDING_KEYS = {
    "controlled_source_id", "lead_source_id", "calibration_id", "target_revision",
    "scope_revision", "state_revision", "authorization_epoch",
}
APPLICABILITY_KEYS = {
    "same_sources", "same_lead", "comparable_section", "comparable_arrangement",
    "comparable_dynamics", "expected_activity", "bleed_suitable",
}
DECLARATION_KEYS = {
    "declaration_id", "issuer", "provenance", "issued_at", "received_at",
    "effective_from", "expires_at", "scope_revision", "target_revision",
    "state_revision", "authorization_epoch", "operator_mode", "applicability",
}
CUE_KEYS = {
    "cue_id", "issuer", "provenance", "binding", "issued_at", "received_at",
    "effective_from", "expires_at", "assertion",
}
FRAME_KEYS = (
    "time_seconds", "levels", "source_ids", "measurement_locations", "scope_revision",
    "state_revision", "state_matches", "observation_age_seconds", "state_age_seconds", "valid",
)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _binding_key(binding):
    return _canonical(binding)


def pair_key(binding, scope_id, cue_id):
    """Immutable public pair identity, also usable by pure contract fixtures."""
    return _canonical([binding, scope_id, cue_id])


def _all_true_scope(record):
    declaration = record["declaration"]
    applicability = declaration["applicability"]
    return declaration["operator_mode"] == "track_accepted_ratio" and all(
        applicability[key] is True for key in APPLICABILITY_KEYS - {"expected_activity", "bleed_suitable"}
    ) and all(applicability[key][role] is True
              for key in ("expected_activity", "bleed_suitable") for role in ROLES)


def translation_batch(binding, scopes, cues, emitted_keys, translation_time,
                      *, new_scope_id=None, new_cue_id=None):
    """Pure all-pair translation; reject an entire delayed batch before emission.

    Inputs are already validated positive public records. At least one newly
    received ID normally narrows the batch. Supplying neither is useful for the
    separate deferred-translation contract check; this function cannot mutate a
    gate or create permission by itself.
    """
    candidates = []
    for scope in scopes:
        declaration = scope["declaration"]
        if scope["binding"] != binding or not _all_true_scope(scope):
            continue
        for cue in cues:
            if cue["binding"] != binding or cue["assertion"] != "unwanted_level_departure":
                continue
            scope_id, cue_id = declaration["declaration_id"], cue["cue_id"]
            if (new_scope_id is not None or new_cue_id is not None) and not (
                scope_id == new_scope_id or cue_id == new_cue_id
            ):
                continue
            key = pair_key(binding, scope_id, cue_id)
            if key in emitted_keys:
                continue
            if declaration["expires_at"] <= translation_time or cue["expires_at"] <= translation_time:
                continue
            start = max(declaration["effective_from"], cue["effective_from"])
            end = min(declaration["expires_at"], cue["expires_at"])
            if start >= end:
                continue
            if start < translation_time:
                raise ValueError("delayed_translation")
            provenance = {
                "gate_generated": True, "binding": copy.deepcopy(binding),
                "scope_id": scope_id, "cue_id": cue_id,
                "scope_received_at": declaration["received_at"],
                "cue_received_at": cue["received_at"],
            }
            output = {
                "declaration_id": "intent-pair:" + hashlib.sha256(key.encode()).hexdigest(),
                "issuer": "synthetic-intent-hold-gate",
                # The accepted policy bounds identifier strings to 256 bytes.
                # This pointer identifies the complete immutable gate audit below.
                "provenance": "intent-pair:" + hashlib.sha256(key.encode()).hexdigest(),
                "issued_at": translation_time, "received_at": translation_time,
                "effective_from": start, "expires_at": end,
                "operator_mode": "track_accepted_ratio",
                "applicability": copy.deepcopy(declaration["applicability"]),
                **{name: binding[name] for name in (
                    "scope_revision", "target_revision", "state_revision", "authorization_epoch"
                )},
            }
            candidates.append((start, end, scope_id, cue_id,
                               {"declaration": output, "provenance": provenance}))
    return [item[-1] for item in sorted(candidates, key=lambda item: item[:4])]


class IntentHoldGate:
    """Serialized finite intent gate; an armed policy can never be replaced."""

    def __init__(self):
        self._policy = TargetPolicy(namespace="synthetic_test_only")
        self._clock = 0.0
        self._binding = None
        self._active_metadata = None
        self._stage = None
        self._ever_armed = False
        self._semantic_suspended = False
        self._hold_reason = None
        self._loss_time = None
        self._initial_invalid_since = None
        self._initial_continuity_lost = False
        self._initial_accepted = False
        self._retained_offset = 0.0
        self._scopes = {}
        self._cues = {}
        self._derived = []
        self._emitted = set()
        self._denials = []
        self._denial_done = set()
        self._audited_boundaries = set()
        self._retired = set()
        self._events = []
        self._acquired = deque(maxlen=8)
        self._last_result = None

    @property
    def candidate(self):
        return self._policy.candidate

    @property
    def target(self):
        return self._policy.target

    @property
    def binding(self):
        return copy.deepcopy(self._binding)

    @property
    def staged_binding(self):
        return copy.deepcopy(self._stage["binding"]) if self._stage is not None else None

    @property
    def declarations(self):
        return copy.deepcopy(self._derived)

    @property
    def events(self):
        return copy.deepcopy(self._events)

    @property
    def controller_created(self):
        return self._policy.controller_created

    @property
    def controller_calls(self):
        return self._policy.controller_calls

    @staticmethod
    def _make_binding(candidate):
        metadata = candidate["metadata"]
        return {
            "controlled_source_id": metadata["source_ids"]["AC GUITAR"],
            "lead_source_id": metadata["source_ids"]["RED"],
            "calibration_id": candidate["calibration_id"],
            **{name: metadata[name] for name in (
                "target_revision", "scope_revision", "state_revision", "authorization_epoch"
            )},
        }

    def _event(self, event, now, **fields):
        self._events.append({"event": event, "time_seconds": now, **copy.deepcopy(fields)})

    def _observe(self, now, result=None):
        if isinstance(result, dict) and isinstance(result.get("controller_decision"), dict):
            self._retained_offset = result["controller_decision"]["offset_db"]
        if self._policy.lifecycle == "suspended" and not self._semantic_suspended:
            self._semantic_suspended = True
            self._loss_time = now
            self._hold_reason = "native_suspension"
            self._event("native_suspension", now)

    def _forward(self, method, now, *args, **kwargs):
        # Every call site has drained pending boundaries first. This method is
        # also the only owned-policy call path, making ordering inspectable.
        details = {}
        if method == "revoke":
            details["reason"] = kwargs.get("reason", "revoked")
        if method == "declare" and args:
            details["declaration_id"] = args[0]["declaration_id"]
        self._event("policy_call", now, method=method, **details)
        try:
            result = getattr(self._policy, method)(*args, **kwargs)
        except (ValueError, TypeError):
            self._observe(now)
            self._event("policy_rejected", now, method=method)
            raise
        self._observe(now, result)
        return result

    def _positive_coverage(self, now, binding):
        if binding is None:
            return False
        key = _binding_key(binding)
        return any(
            _binding_key(record["provenance"]["binding"]) == key
            and record["declaration"]["effective_from"] <= now < record["declaration"]["expires_at"]
            for record in self._derived
        )

    def _intent_status(self, now, binding):
        if binding is None:
            return False, "missing_intent"
        key = _binding_key(binding)
        denying = [record for record in self._denials if record["binding_key"] == key
                   and record["effective_from"] <= now < record["expires_at"]]
        if denying:
            denial = sorted(denying, key=lambda record: (
                record["effective_from"], record["reason"], record["id"]
            ))[0]
            if denial["reason"] == "preserve_intended_change" and any(
                cue["binding"] == binding and cue["assertion"] == "unwanted_level_departure"
                and cue["effective_from"] == denial["effective_from"]
                and cue["effective_from"] <= now < cue["expires_at"] for cue in self._cues.values()
            ):
                return False, "conflicting_intent"
            return False, denial["reason"]
        return (True, "unwanted_level_departure") if self._positive_coverage(now, binding) else (
            False, "missing_intent"
        )

    def _hold(self, now, reason, *, context="active", invalidate_initial=False):
        self._event("intent_hold", now, reason=reason, context=context)
        if context == "recovery" and self._stage is not None:
            self._stage["valid"] = False
            self._stage["accepted"] = False
            # A staged denial concerns the new acceptance, unlike old-cycle
            # audit events. The public revoke call clears that acceptance.
            if self._policy.target is not None and self._stage["binding"] is not None:
                self._forward("revoke", now, now, reason=reason)
            return
        if self._ever_armed:
            if not self._semantic_suspended:
                self._hold_reason = reason
                self._loss_time = now
                self._forward("revoke", now, now, reason=reason)
                self._semantic_suspended = True
                self._hold_reason = reason
            else:
                self._event("old_cycle_audit_only", now, reason=reason)
        elif invalidate_initial:
            if self._initial_invalid_since is None:
                self._initial_invalid_since = now
            if reason == "initial_continuity_lost":
                self._initial_continuity_lost = True
            self._initial_accepted = False
            self._forward("revoke", now, now, reason=reason)

    def _drain(self, now):
        if not _number(now) or now < self._clock:
            raise ValueError("event time must be finite and monotonic")
        boundaries = {now}
        for record in self._denials:
            boundaries.update(value for value in (record["effective_from"], record["expires_at"])
                              if self._clock <= value <= now)
        for record in self._derived:
            declaration = record["declaration"]
            boundaries.update(value for value in (declaration["effective_from"], declaration["expires_at"])
                              if self._clock <= value <= now)
        for boundary in sorted(boundaries):
            self._clock = boundary
            # Observe native suspension before deciding whether another revoke
            # would accidentally clear an accepted staged target.
            self._observe(boundary)
            if self._stage is not None and self._stage["binding"] is not None:
                stage_key = _binding_key(self._stage["binding"])
                for denial in self._denials:
                    if (denial["binding_key"] == stage_key and denial["id"] not in self._denial_done
                            and denial["effective_from"] <= boundary < denial["expires_at"]):
                        self._denial_done.add(denial["id"])
                        self._hold(boundary, denial["reason"], context="recovery")
            if not self._ever_armed and self._binding is not None:
                initial_key = _binding_key(self._binding)
                for denial in self._denials:
                    if (denial["binding_key"] == initial_key and denial["id"] not in self._denial_done
                            and denial.get("invalidate_initial", False)
                            and denial["effective_from"] <= boundary < denial["expires_at"]):
                        self._denial_done.add(denial["id"])
                        self._hold(boundary, "initial_continuity_lost", invalidate_initial=True)
            if self._ever_armed and self._binding is not None:
                active_key = _binding_key(self._binding)
                for denial in self._denials:
                    if (denial["binding_key"] == active_key and denial["id"] not in self._denial_done
                            and denial["effective_from"] <= boundary < denial["expires_at"]
                            and self._semantic_suspended):
                        self._denial_done.add(denial["id"])
                        self._hold(boundary, denial["reason"])
                allowed, reason = self._intent_status(boundary, self._binding)
                if not allowed and not self._semantic_suspended:
                    reason = "intent_coverage_expired" if reason == "missing_intent" else reason
                    self._hold(boundary, reason)
                    for denial in self._denials:
                        if (denial["binding_key"] == active_key
                                and denial["effective_from"] <= boundary < denial["expires_at"]):
                            self._denial_done.add(denial["id"])
            for translated in self._derived:
                declaration = translated["declaration"]
                key = (declaration["declaration_id"], declaration["expires_at"])
                if declaration["expires_at"] == boundary and key not in self._audited_boundaries:
                    self._audited_boundaries.add(key)
                    self._event("positive_interval_expired", boundary,
                                declaration_id=declaration["declaration_id"],
                                binding=translated["provenance"]["binding"])
        self._clock = now

    def _context_binding(self, context):
        if context == "active":
            return self._binding
        if context == "recovery" and self._stage is not None and self._stage["valid"]:
            return self._stage["binding"]
        raise ValueError("unrecognized recovery context")

    def _check_binding(self, binding, context):
        if not isinstance(binding, dict) or set(binding) != BINDING_KEYS or not all(
            _identifier(value) for value in binding.values()
        ):
            raise ValueError("unverifiable_binding")
        expected = self._context_binding(context)
        if expected is None or binding != expected:
            raise ValueError("unverifiable_binding")

    def _reject(self, now, reason, context, *, binding=None):
        # Audit-only handling requires positive identification of the old cycle.
        # A mislabeled staged record, or ambiguous input while staging, cannot
        # exploit its caller-supplied context to preserve staged acceptance.
        destination = "active"
        if self._stage is not None:
            if binding is None or binding != self._binding:
                destination = "recovery"
        self._hold(now, reason, context=destination, invalidate_initial=True)
        raise ValueError(reason)

    def calibrate(self, frames, metadata, *, time_seconds, context="active"):
        self._drain(time_seconds)
        if context == "active":
            if self._ever_armed:
                raise ValueError("post-arm calibration requires explicit recovery preparation")
            if self._initial_invalid_since is not None and (
                not isinstance(frames, (list, tuple)) or not frames
                or frames[0].get("time_seconds", 0) - 1 < self._initial_invalid_since
            ):
                raise ValueError("new calibration must follow initial continuity loss")
        elif context == "recovery":
            stage = self._stage
            if stage is None or not stage["valid"] or metadata != stage["metadata"]:
                self._reject(time_seconds, "invalid_recovery_preparation", "recovery")
            observed = list(self._acquired)
            supplied = [{key: copy.deepcopy(frame[key]) for key in FRAME_KEYS if key in frame}
                        for frame in frames]
            if (len(observed) != 8 or supplied != observed
                    or observed[0]["time_seconds"] - 1 < stage["prepared_at"]
                    or observed[0]["time_seconds"] - 1 < self._loss_time):
                self._reject(time_seconds, "invalid_recovery_acquisition", "recovery")
        else:
            raise ValueError("unknown calibration context")
        result = self._forward("calibrate", time_seconds, frames, metadata, time_seconds=time_seconds)
        if result is not None:
            if context == "active" and self._initial_continuity_lost:
                # No controller has ever existed here. A genuinely changed
                # pre-arm state cannot inherit obsolete positive native leases.
                # First validate the new completed calibration above; only then
                # retire the unarmed policy. Audit records and pair identities
                # stay in this gate, while acceptance/arm remain explicit.
                if self._ever_armed or self._policy.controller_created:
                    raise RuntimeError("an armed policy cannot be replaced")
                self._event("unarmed_policy_replaced", time_seconds,
                            retired_binding=self._binding,
                            continuity_lost_at=self._initial_invalid_since)
                self._policy = TargetPolicy(namespace="synthetic_test_only")
                replacement = self._forward("calibrate", time_seconds, frames, metadata,
                                            time_seconds=time_seconds)
                if replacement != result:
                    raise RuntimeError("fresh unarmed calibration changed during replacement")
                result = replacement
            binding = self._make_binding(result)
            if context == "active":
                self._binding = binding
                self._active_metadata = copy.deepcopy(metadata)
                self._initial_accepted = False
                self._initial_invalid_since = None
                self._initial_continuity_lost = False
            else:
                self._stage["binding"] = binding
                self._stage["accepted"] = False
        elif context == "recovery":
            self._stage["valid"] = False
        return result

    def accept(self, record, *, time_seconds, context="active"):
        self._drain(time_seconds)
        supplied_binding = None
        if isinstance(record, dict):
            candidates = [self._binding]
            if self._stage is not None:
                candidates.append(self._stage["binding"])
            supplied_binding = next((value for value in candidates if value is not None
                                     and record.get("calibration_id") == value["calibration_id"]), None)
        try:
            binding = self._context_binding(context)
        except ValueError:
            self._reject(time_seconds, "unverifiable_binding", context, binding=supplied_binding)
        if context == "active" and self._ever_armed:
            self._reject(time_seconds, "post-arm acceptance requires recovery context", context,
                         binding=supplied_binding)
        if binding is None or not isinstance(record, dict) or record.get("calibration_id") != binding["calibration_id"]:
            self._reject(time_seconds, "unverifiable_binding", context, binding=supplied_binding)
        try:
            result = self._forward("accept", time_seconds, record, time_seconds=time_seconds)
        except (ValueError, TypeError):
            self._reject(time_seconds, "invalid_acceptance", context, binding=supplied_binding)
        if context == "recovery":
            self._stage["accepted"] = True
        else:
            self._initial_accepted = True
        return result

    @staticmethod
    def _validate_scope(record, now):
        if not isinstance(record, dict) or set(record) != {"binding", "declaration"}:
            raise ValueError("malformed_scope")
        declaration = record["declaration"]
        if not isinstance(declaration, dict) or set(declaration) != DECLARATION_KEYS:
            raise ValueError("malformed_scope")
        for key in ("declaration_id", "issuer", "provenance", "scope_revision", "target_revision",
                    "state_revision", "authorization_epoch"):
            if not _identifier(declaration[key]):
                raise ValueError("malformed_scope")
        issued, received, start, end = (declaration[key] for key in (
            "issued_at", "received_at", "effective_from", "expires_at"
        ))
        if not all(_number(value) and value >= 0 for value in (issued, received, start, end)) or not (
            issued <= received == now <= start < end and end - start <= 30
        ):
            raise ValueError("malformed_scope")
        applicability = declaration["applicability"]
        if not isinstance(applicability, dict) or set(applicability) != APPLICABILITY_KEYS:
            raise ValueError("malformed_scope")
        for key in APPLICABILITY_KEYS - {"expected_activity", "bleed_suitable"}:
            if applicability[key] is not True and applicability[key] is not False and applicability[key] is not None:
                raise ValueError("malformed_scope")
        for key in ("expected_activity", "bleed_suitable"):
            values = applicability[key]
            if not isinstance(values, dict) or set(values) != set(ROLES) or any(
                value is not True and value is not False and value is not None for value in values.values()
            ):
                raise ValueError("malformed_scope")
        if declaration["operator_mode"] not in ("track_accepted_ratio", "hold"):
            raise ValueError("malformed_scope")

    def scope(self, record, *, time_seconds, context="active"):
        self._drain(time_seconds)
        declaration = record.get("declaration") if isinstance(record, dict) else None
        identifier = declaration.get("declaration_id") if isinstance(declaration, dict) else None
        if isinstance(identifier, str) and identifier in self._scopes:
            if record == self._scopes[identifier]:
                try:
                    self._check_binding(record["binding"], context)
                except ValueError as exc:
                    self._reject(time_seconds, str(exc), context, binding=record["binding"])
                return copy.deepcopy(record)
            self._reject(time_seconds, "scope_id_conflict", context, binding=record.get("binding"))
        try:
            self._validate_scope(record, time_seconds)
            self._check_binding(record["binding"], context)
            if any(record["declaration"][key] != record["binding"][key] for key in (
                "scope_revision", "target_revision", "state_revision", "authorization_epoch"
            )):
                raise ValueError("unverifiable_binding")
        except (ValueError, TypeError, KeyError) as exc:
            self._reject(time_seconds, str(exc), context,
                         binding=record.get("binding") if isinstance(record, dict) else None)
        saved = copy.deepcopy(record)
        self._scopes[identifier] = saved
        self._event("scope_received", time_seconds, context=context, record=saved)
        if not _all_true_scope(saved):
            declaration = saved["declaration"]
            continuity_unknown = any(declaration["applicability"][key] is not True for key in (
                "same_sources", "same_lead", "comparable_section", "comparable_arrangement",
                "comparable_dynamics",
            ))
            self._queue_denial(identifier, saved["binding"], declaration["effective_from"],
                               declaration["expires_at"], "scope_not_applicable", "scope",
                               invalidate_initial=continuity_unknown)
            self._drain(time_seconds)
        else:
            self._translate_records(list(self._scopes.values()), list(self._cues.values()),
                                    time_seconds=time_seconds, context=context, new_scope_id=identifier)
        return copy.deepcopy(saved)

    def _queue_denial(self, identifier, binding, start, end, reason, kind, *, invalidate_initial=False):
        self._denials.append({"id": kind + ":" + identifier, "binding_key": _binding_key(binding),
                              "effective_from": start, "expires_at": end, "reason": reason,
                              "invalidate_initial": invalidate_initial})

    def cue(self, record, *, time_seconds, context="active"):
        self._drain(time_seconds)
        identifier = record.get("cue_id") if isinstance(record, dict) else None
        if isinstance(identifier, str) and identifier in self._cues:
            if record == self._cues[identifier]:
                try:
                    self._check_binding(record["binding"], context)
                except ValueError as exc:
                    self._reject(time_seconds, str(exc), context, binding=record["binding"])
                return copy.deepcopy(record)
            self._reject(time_seconds, "cue_id_conflict", context, binding=record.get("binding"))
        try:
            if not isinstance(record, dict) or set(record) != CUE_KEYS:
                raise ValueError("malformed_cue")
            if not all(_identifier(record[key]) for key in ("cue_id", "issuer", "provenance")):
                raise ValueError("malformed_cue")
            self._check_binding(record["binding"], context)
            issued, received, start, end = (record[key] for key in (
                "issued_at", "received_at", "effective_from", "expires_at"
            ))
            if not all(_number(value) and value >= 0 for value in (issued, received, start, end)) or not (
                issued <= received == time_seconds and issued <= start < end
            ):
                raise ValueError("malformed_cue")
            if end <= time_seconds:
                raise ValueError("expired_cue")
            if end - issued > 30:
                raise ValueError("cue_lifetime_exceeded")
            assertion = record["assertion"]
            if assertion not in ("unwanted_level_departure", "preserve_intended_change", "unknown"):
                raise ValueError("malformed_cue")
            if assertion == "unwanted_level_departure" and start < time_seconds:
                raise ValueError("late_positive_cue")
        except (ValueError, TypeError, KeyError) as exc:
            self._reject(time_seconds, str(exc), context,
                         binding=record.get("binding") if isinstance(record, dict) else None)
        saved = copy.deepcopy(record)
        self._cues[identifier] = saved
        self._event("cue_received", time_seconds, context=context, record=saved)
        if assertion != "unwanted_level_departure":
            actual_start = max(start, time_seconds)
            self._queue_denial(identifier, saved["binding"], actual_start, end,
                               assertion, "cue")
            if start < time_seconds:
                self._event("missed_intent_interval", time_seconds, cue_id=identifier,
                            advertised_start=start, actual_start=actual_start)
            self._drain(time_seconds)
        else:
            self._translate_records(list(self._scopes.values()), list(self._cues.values()),
                                    time_seconds=time_seconds, context=context, new_cue_id=identifier)
        return copy.deepcopy(saved)

    def _translate_records(self, scopes, cues, *, time_seconds, context="active",
                           new_scope_id=None, new_cue_id=None):
        """Narrow internal batch boundary; P02 may exercise delayed rejection here."""
        self._drain(time_seconds)
        binding = self._context_binding(context)
        try:
            batch = translation_batch(binding, scopes, cues, self._emitted, time_seconds,
                                      new_scope_id=new_scope_id, new_cue_id=new_cue_id)
        except ValueError as exc:
            self._reject(time_seconds, str(exc), context, binding=binding)
        # Validation of the entire batch above precedes the first permission write.
        for translated in batch:
            declaration = translated["declaration"]
            self._drain(time_seconds)
            self._forward("declare", time_seconds, declaration, time_seconds=time_seconds)
            provenance = translated["provenance"]
            self._emitted.add(pair_key(binding, provenance["scope_id"], provenance["cue_id"]))
            self._derived.append(copy.deepcopy(translated))
        return copy.deepcopy(batch)

    def arm(self, time_seconds):
        self._drain(time_seconds)
        if self._ever_armed:
            raise ValueError("ever-armed gate requires explicit recovery and rearm")
        allowed, reason = self._intent_status(time_seconds, self._binding)
        if not allowed or not self._initial_accepted or self._initial_invalid_since is not None:
            raise ValueError("initial_blocked:" + reason)
        result = self._forward("arm", time_seconds, time_seconds)
        self._ever_armed = True
        self._semantic_suspended = False
        return result

    def prepare_recovery(self, metadata, *, time_seconds):
        self._drain(time_seconds)
        if not self._ever_armed or not self._semantic_suspended:
            raise ValueError("recovery preparation requires a suspended armed cycle")
        if not isinstance(metadata, dict) or self._active_metadata is None:
            self._reject(time_seconds, "invalid_recovery_metadata", "recovery")
        old = self._active_metadata
        try:
            if any(not _identifier(metadata.get(key)) or metadata[key] == old[key]
                   for key in ("target_revision", "state_revision", "authorization_epoch")):
                raise ValueError("recovery requires new target/state/epoch identities")
            if any(metadata.get(key) != old[key] for key in ("source_ids", "measurement_locations", "measurement_domain")):
                raise ValueError("unsupported recovery source or measurement change")
            faders = metadata["baseline_faders_db"]
            if not isinstance(faders, dict) or set(faders) != set(ROLES) or not all(_number(value) for value in faders.values()):
                raise ValueError("known recovery baseline faders are required")
            if any(faders[role] != old["baseline_faders_db"][role] + (
                self._retained_offset if role == "AC GUITAR" else 0.0
            ) for role in ROLES):
                raise ValueError("recovery must explicitly rebase the retained virtual level")
        except (ValueError, KeyError, TypeError):
            self._reject(time_seconds, "invalid_recovery_metadata", "recovery")
        self._stage = {"metadata": copy.deepcopy(metadata), "prepared_at": time_seconds,
                       "binding": None, "valid": True, "accepted": False}
        self._acquired.clear()
        self._event("recovery_prepared", time_seconds, metadata=metadata)

    def rearm(self, time_seconds):
        self._drain(time_seconds)
        stage = self._stage
        if not self._ever_armed or not self._semantic_suspended or stage is None or not stage["valid"] or not stage["accepted"]:
            raise ValueError("complete explicit staged recovery is required")
        allowed, reason = self._intent_status(time_seconds, stage["binding"])
        if not allowed:
            raise ValueError("recovery_held:" + reason)
        result = self._forward("rearm", time_seconds, time_seconds)
        self._retired.add(_binding_key(self._binding))
        self._binding = copy.deepcopy(stage["binding"])
        self._active_metadata = copy.deepcopy(stage["metadata"])
        self._stage = None
        self._semantic_suspended = False
        self._hold_reason = None
        self._loss_time = None
        self._acquired.clear()
        self._event("recovery_promoted", time_seconds, binding=self._binding)
        return result

    def feed(self, frame):
        now = frame.get("time_seconds") if isinstance(frame, dict) else None
        self._drain(now)
        if not isinstance(frame, dict):
            raise ValueError("frame must be a mapping")
        if self._stage is not None and self._stage["valid"] and now > self._stage["prepared_at"]:
            metadata = self._stage["metadata"]
            matched = frame.get("state_matches") is True and all(frame.get(key) == metadata[key] for key in (
                "source_ids", "measurement_locations", "scope_revision", "state_revision"
            ))
            if not matched or frame.get("takeover") or frame.get("revoked"):
                self._hold(now, "recovery_acquisition_state_changed", context="recovery")
            else:
                self._acquired.append({key: copy.deepcopy(frame[key]) for key in FRAME_KEYS if key in frame})
        elif not self._ever_armed and self._active_metadata is not None:
            metadata = self._active_metadata
            if frame.get("takeover") or frame.get("revoked") or frame.get("state_matches") is not True or any(frame.get(key) != metadata[key] for key in (
                "source_ids", "measurement_locations", "scope_revision", "state_revision"
            )):
                self._hold(now, "initial_continuity_lost", invalidate_initial=True)
        result = self._forward("feed", now, frame)
        return result

    def evaluate(self, time_seconds):
        self._drain(time_seconds)
        result = self._forward("evaluate", time_seconds, time_seconds)
        allowed, reason = self._intent_status(time_seconds, self._binding)
        if not self._ever_armed:
            phase = "initial_ready" if allowed and self._initial_accepted and self._initial_invalid_since is None else "initial_blocked"
        elif self._semantic_suspended:
            phase = "held"
            reason = self._hold_reason
        else:
            phase = "monitoring"
        if phase in ("initial_blocked", "held") and result["gain_proposal"] is not None:
            raise RuntimeError("HOLD invariant violated: owned policy returned a gain proposal")
        result = {**result, "native_lifecycle": result["lifecycle"], "native_reason": result["reason"],
                  "gate_phase": phase, "intent_reason": reason,
                  "retained_offset_db": self._retained_offset,
                  "gate_loss_time": self._loss_time,
                  "musical_approval_valid": False, "live_eligible": False}
        self._last_result = copy.deepcopy(result)
        return result

    def revoke(self, time_seconds, reason="revoked"):
        self._drain(time_seconds)
        if self._stage is not None:
            self._hold(time_seconds, reason, context="recovery")
        elif self._ever_armed:
            self._hold(time_seconds, reason)
        else:
            self._hold(time_seconds, reason, invalidate_initial=True)
