"""Experimental scalar-only gain decisions; no musical approval or host control.

One frozen initial reference replaces the same-time oracle used by older tests.
Audio-derived support is a fallible hypothesis, never an operator assertion.
This module deliberately has no I/O, automatic rearm, or live integration.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
from statistics import median

from .gain_controller import GainController


ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")
CONTEXT_ROLES = ROLES[2:]
_IDENTITIES = ("source_map_revision", "routing_revision", "state_revision", "epoch")
POLICY = {
    "kind": "experimental_audio_gain_v1", "calibration_seconds": 16,
    "window_seconds": 8, "evaluation_seconds": 2,
    "admission_seconds": 20, "return_seconds": 8,
    "admission_error_db": 2.0, "context_tolerance_db": 1.5,
    "minimum_range_db": 1.0, "maximum_correlation": 0.95,
    "musical_approval_valid": False, "live_eligible": False,
}


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def _percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _envelope(pairs):
    ac, red = [p[0] for p in pairs], [p[1] for p in pairs]
    am, rm = sum(ac) / len(ac), sum(red) / len(red)
    av = sum((x - am) ** 2 for x in ac) / len(ac)
    rv = sum((x - rm) ** 2 for x in red) / len(red)
    correlation = None
    if av > 0 and rv > 0:
        correlation = sum((a - am) * (r - rm) for a, r in pairs) / len(pairs)
        correlation /= math.sqrt(av * rv)
    return {"ac_range_db": _percentile(ac, .9) - _percentile(ac, .1),
            "ac_std_db": math.sqrt(av), "red_std_db": math.sqrt(rv),
            "correlation": correlation}


class ExperimentalAudioGain:
    """Feed explicit completed one-second frames; receive every even decision row.

    Sequence equals logical endpoint. Missing endpoints become invalid slots.
    Receipt/acquisition clocks and source identities are supplied simulation
    evidence, not discovered facts about a running audio system. Invalid input
    is retained in events; no setup/reset method can silently restore gain.
    """

    def __init__(self):
        self._reference = None
        self._controller = None
        self._identity = None
        self._last_endpoint = 0
        self._last_receipt = None
        self._calibration = []
        self._history = deque(maxlen=8)
        self._last_bad_frame = 0
        self._hard_reason = None
        self._calibration_rejection = None
        self._admission_since = None
        self._return_since = None
        self.phase = "calibration"
        self.events = []

    @property
    def reference(self):
        return deepcopy(self._reference)

    @property
    def offset_db(self):
        return self._controller.offset_db if self._controller is not None else 0.0

    def _clear_episode(self):
        self._admission_since = None
        self._return_since = None

    def _hard(self, reason, endpoint=None):
        if self._hard_reason is None:
            self._hard_reason = reason
        self._clear_episode()
        self.phase = "suspended"
        self.events.append({"kind": "hard_fault", "reason": reason,
                            "at_endpoint": endpoint if _number(endpoint) else None,
                            "last_trusted_endpoint": self._last_endpoint,
                            "retained_offset_db": self.offset_db})

    def _frame(self, incoming, endpoint):
        """Copy only declared fields; classify faults before changing evidence."""
        receipt = incoming.get("received_at")
        acquired = incoming.get("acquired_at")
        observed = incoming.get("state_observed_at")
        sequence = incoming.get("sequence")
        if (not isinstance(sequence, int) or isinstance(sequence, bool)
                or sequence != endpoint or acquired != endpoint
                or not _number(acquired) or not _number(receipt)
                or not _number(observed) or receipt < acquired or observed > receipt):
            self._hard("unknown_or_invalid_clock", endpoint)
        elif self._last_receipt is not None and receipt < self._last_receipt:
            self._hard("receipt_clock_rollback", endpoint)
        else:
            self._last_receipt = receipt
        ids = incoming.get("source_ids")
        valid_ids = (isinstance(ids, dict) and set(ids) == set(ROLES)
                     and all(_identifier(value) for value in ids.values())
                     and len(set(ids.values())) == len(ROLES))
        if (not valid_ids or not all(_identifier(incoming.get(key)) for key in _IDENTITIES)
                or incoming.get("alignment_known") is not True):
            self._hard("unknown_identity_or_alignment", endpoint)
        else:
            identity = {"source_ids": dict(ids), **{k: incoming[k] for k in _IDENTITIES}}
            if self._identity is None:
                self._identity = identity
            elif identity != self._identity:
                self._hard("identity_changed", endpoint)

        reason = None
        observation_age = receipt - acquired if _number(receipt) and _number(acquired) else None
        state_age = receipt - observed if _number(receipt) and _number(observed) else None
        levels = incoming.get("levels")
        if not isinstance(levels, dict) or set(levels) != set(ROLES):
            reason = "missing_source_measurement"
        elif not all(_number(levels[role]) for role in ROLES):
            reason = "nonfinite_measurement"
        elif not _number(observation_age) or not 0 <= observation_age < .5:
            reason = "stale_observation"
        elif not _number(state_age) or not 0 <= state_age <= 2.5:
            reason = "stale_state"
        if self._hard_reason:
            reason = self._hard_reason
        clean_levels = {r: float(levels[r]) for r in ROLES} if reason is None else None
        return {"endpoint": endpoint, "levels": clean_levels, "valid": reason is None,
                "reason": reason, "observation_age_seconds": observation_age,
                "state_age_seconds": state_age}

    def feed(self, frame):
        """One explicit frame; no implementation sees an unreceived future suffix."""
        if not isinstance(frame, dict):
            self._hard("malformed_frame")
            return []
        endpoint = frame.get("time_seconds")
        if not isinstance(endpoint, int) or isinstance(endpoint, bool) or endpoint < 1:
            self._hard("invalid_endpoint", endpoint)
            return []
        if endpoint <= self._last_endpoint:
            self._hard("duplicate_or_out_of_order_frame", endpoint)
            return []
        rows = []
        for missing in range(self._last_endpoint + 1, endpoint):
            missing_slot = {"endpoint": missing, "levels": None, "valid": False,
                            "reason": "missing_frame", "observation_age_seconds": None,
                            "state_age_seconds": None}
            row = self._push(missing_slot)
            if row is not None:
                rows.append(row)
        # A newly received identity/clock fault belongs to this endpoint, not
        # earlier missing slots. Preserve chronological uncertainty in the log.
        slot = self._frame(frame, endpoint)
        row = self._push(slot)
        if row is not None:
            rows.append(row)
        self._last_endpoint = endpoint
        return rows

    def _push(self, slot):
        endpoint = slot["endpoint"]
        self._history.append(slot)
        if endpoint <= 16:
            self._calibration.append(slot)
        if not slot["valid"]:
            self._last_bad_frame = endpoint
            self._clear_episode()
            if not self._hard_reason:
                self.phase = "hold"
        if endpoint % 2:
            return None
        if endpoint == 16 and not self._hard_reason:
            self._calibrate()
        return self._evaluate(endpoint, slot)

    @staticmethod
    def _support(frames, thresholds, role):
        return [f for f in frames if f["valid"]
                and f["levels"][role] > thresholds[role]
                and f["levels"]["RED"] > thresholds["RED"]]

    @staticmethod
    def _envelope_rejection(envelope, initial_range=None):
        if envelope["ac_range_db"] < 1:
            return "stationary_ac_envelope"
        if initial_range is not None and envelope["ac_range_db"] > 2 * initial_range + 3:
            return "volatile_ac_envelope"
        if (envelope["ac_std_db"] > .5 and envelope["red_std_db"] > .5
                and envelope["correlation"] is not None
                and abs(envelope["correlation"]) >= .95):
            return "duplicate_envelope_ambiguity"
        return None

    def _calibrate(self):
        if len(self._calibration) != 16 or not all(f["valid"] for f in self._calibration):
            self._calibration_rejection = "invalid_initial_frames"
            return
        thresholds = {r: max(-60., _percentile([f["levels"][r] for f in self._calibration], .9) - 18)
                      for r in ROLES}
        supports = {r: self._support(self._calibration, thresholds, r)
                    for r in ("AC GUITAR", *CONTEXT_ROLES)}
        contexts = [r for r in CONTEXT_ROLES if len(supports[r]) >= 12]
        if len(supports["AC GUITAR"]) < 12 or len(contexts) < 2:
            self._calibration_rejection = "insufficient_initial_support"
            return
        ac_pairs = [(f["levels"]["AC GUITAR"], f["levels"]["RED"])
                    for f in supports["AC GUITAR"]]
        envelope = _envelope(ac_pairs)
        reason = self._envelope_rejection(envelope)
        if reason:
            self._calibration_rejection = reason
            return
        theta = {r: median(f["levels"][r] - f["levels"]["RED"] for f in supports[r])
                 for r in ("AC GUITAR", *contexts)}
        self._reference = {"thresholds_dbfs": thresholds, "theta_db": theta,
                           "ac_range_db": envelope["ac_range_db"], "context_roles": contexts,
                           "calibration_interval": [0, 16], "envelope": envelope,
                           "initial_ac_support": [f["endpoint"] for f in supports["AC GUITAR"]],
                           "initial_pair_support": {r: [f["endpoint"] for f in supports[r]] for r in theta}}
        self._controller = GainController("experimental-initial", "experimental-frozen-reference")
        self.phase = "hold"

    def _measure(self, endpoint):
        evidence = {"source_interval": [max(0, endpoint - 8), endpoint],
                    "support_kind": "raw_energy_not_semantic_activity", "pair_support": {},
                    "residuals_db": {}, "envelope": None}
        if len(self._history) != 8 or not all(f["valid"] for f in self._history):
            return None, evidence, "invalid_window"
        thresholds = self._reference["thresholds_dbfs"]
        residuals = evidence["residuals_db"]
        for role in ("AC GUITAR", *self._reference["context_roles"]):
            supported = self._support(self._history, thresholds, role)
            evidence["pair_support"][role] = [f["endpoint"] for f in supported]
            if len(supported) < 6 or supported[-1]["endpoint"] != endpoint:
                return None, evidence, "insufficient_or_inactive_support:" + role
            residuals[role] = median(f["levels"][role] - f["levels"]["RED"] for f in supported)
            residuals[role] -= self._reference["theta_db"][role]
            if role == "AC GUITAR":
                evidence["envelope"] = _envelope([(f["levels"][role], f["levels"]["RED"])
                                                   for f in supported])
        raw = residuals["AC GUITAR"]
        for role in self._reference["context_roles"]:
            if abs(residuals[role]) > 1.5:
                return raw, evidence, "context_departure:" + role
        reason = self._envelope_rejection(evidence["envelope"], self._reference["ac_range_db"])
        return raw, evidence, reason

    def _qualify(self, endpoint, raw, reason, contiguous):
        if reason or contiguous < 8:
            self._clear_episode()
            self.phase = "hold"
            return False, reason or "fresh_history_required"
        offset = self.offset_db
        if abs(raw) <= .5:
            self._admission_since = None
            if offset >= 0:
                self._return_since = None
                self.phase = "hold"
                return False, "at_reference"
            if self.phase not in ("return_wait", "release") or self._return_since is None:
                self._return_since = endpoint
                self.phase = "return_wait"
            if endpoint - self._return_since >= 8:
                self.phase = "release"
                return True, "return_release_supported"
            return False, "return_evidence_wait"
        # Any departure from the return band ends its timer, even during release.
        self._return_since = None
        if raw + offset < -.75:
            self._admission_since = None
            self.phase = "hold"
            return False, "overcorrection_without_return_hold"
        if self.phase == "attenuate":
            return True, "admitted_audio_hypothesis"
        if raw < 2:
            self._admission_since = None
            self.phase = "hold"
            return False, "below_admission_threshold"
        if self.phase != "admission_wait" or self._admission_since is None:
            self._admission_since = endpoint
            self.phase = "admission_wait"
        if endpoint - self._admission_since >= 20:
            self.phase = "attenuate"
            return True, "admitted_audio_hypothesis"
        return False, "admission_evidence_wait"

    def _evaluate(self, endpoint, slot):
        contiguous = endpoint - self._last_bad_frame
        raw, evidence, result = None, {}, None
        eligible, reason = False, "initial_calibration"
        if self._hard_reason:
            self.phase, reason = "suspended", self._hard_reason
        elif self._calibration_rejection:
            self.phase, reason = "calibration_rejected", self._calibration_rejection
        elif self._reference is None:
            self.phase = "calibration"
        else:
            raw, evidence, guard_reason = self._measure(endpoint)
            eligible, reason = self._qualify(endpoint, raw, guard_reason, contiguous)
        if self._controller is not None:
            result = self._controller.step({
                "time_seconds": endpoint, "raw_error_db": raw,
                "eligible": eligible, "reason": reason,
                "contiguous_seconds": contiguous,
                "observation_age_seconds": slot["observation_age_seconds"],
                "state_age_seconds": slot["state_age_seconds"],
                "revision": self._controller.revision,
                "anchor_revision": self._controller.anchor_revision,
            })
        action = result["action"] if result else "calibrating"
        if self.phase == "suspended":
            action = "suspended"
        elif self.phase == "calibration_rejected":
            action = "blocked"
        phase = self.phase
        if phase == "release" and self.offset_db == 0:
            self._clear_episode()
            self.phase = "hold"
        return {"time_seconds": endpoint, "phase": phase, "next_phase": self.phase,
                "action": action, "reason": reason, "eligible": eligible,
                "raw_error_db": raw, "contiguous_seconds": contiguous,
                "offset_db": self.offset_db, "delta_db": result["delta_db"] if result else 0.,
                "effective_gain": 10 ** (self.offset_db / 20),
                "admission_since": self._admission_since, "return_since": self._return_since,
                "reference": self.reference, "evidence": evidence, "controller": result,
                "musical_approval_valid": False, "live_eligible": False}
