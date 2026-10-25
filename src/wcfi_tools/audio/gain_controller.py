"""Pure offline gain-controller prerequisite, not a live musical balance policy.

The limits below are frozen engineering bounds from the reviewed October 24,
2026 revision-2 plan. A caller must supply causal, independently attributed
drift and immutable source support. This module cannot infer either from audio.
It has no I/O, control-client, audio-device or network dependencies. All changes
are virtual offsets from the same baseline, never mutations of observations.
"""
from __future__ import annotations

import math
from collections import deque
from statistics import median

# Keep internal policy separate so mutating the exported provenance dict cannot
# silently alter an already reviewed controller's limits.
_POLICY = {
    "version": "october-offline-gain-r2-2",
    "minimum_offset_db": -3.0,
    "maximum_offset_db": 0.0,
    "step_db": 0.5,
    "minimum_move_interval_seconds": 10.0,
    "evaluation_interval_seconds": 2.0,
    "evidence_seconds": 8.0,
    "history_estimates": 3,
    "direction_estimates": 3,
    "entry_error_db": 1.5,
    "continuation_error_db": 0.75,
    "release_raw_error_db": 0.5,
    "overcorrection_error_db": -0.75,
    "maximum_observation_age_seconds_exclusive": 0.5,
    "maximum_state_age_seconds_inclusive": 2.5,
    "target": "virtual AC GUITAR offset only",
    "live_musical_policy": False,
}
POLICY = dict(_POLICY)


def _number(value):
    """Reject bools, nonfinite values and coerced strings at the pure boundary."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _revision(value):
    return isinstance(value, str) and 0 < len(value) <= 256


class GainController:
    """Bounded virtual state machine; ``step`` returns a JSON-safe decision.

    ``offset_db`` in a result is the total virtual offset AFTER the proposal;
    ``delta_db`` is only that proposal's increment. ``effective_gain`` is always
    computed once from the total offset. Ineligible observations clear evidence
    but retain the virtual offset. Revision changes suspend rather than restore.
    Only an explicit new revision AND anchor can rearm the controller.

    Evaluations are scheduled exactly two logical seconds apart. Early calls
    clear pending evidence and abstain without advancing the evaluation clock.
    A late evaluation clears old evidence before considering its fresh sample;
    it does not reset the separate ten-second minimum between virtual moves.

    ``contiguous_seconds`` is supplied by the causal adapter, measured from the
    last actual bad frame. We do not restart an eight-second timer each time an
    overlapping window still contains that frame. Rearm starts its own fresh
    eight-second requirement because old data belongs to a different baseline.
    """

    def __init__(self, revision="base", anchor_revision="base"):
        if not _revision(revision) or not _revision(anchor_revision):
            raise ValueError("Nonempty revision and anchor_revision strings are required")
        self.revision = revision
        self.anchor_revision = anchor_revision
        self.offset_db = 0.0
        self.suspended = False
        self._suspension_reason = None
        self._raw = deque(maxlen=_POLICY["history_estimates"])
        self._direction = None
        self._direction_count = 0
        self._new_estimates = 0
        self._release_count = 0
        self._attenuating = False
        self._last_time = None
        self._last_evaluation_time = None
        self._timing_status = None
        self._last_move = None
        self._rearmed_at = None

    def _clear_evidence(self):
        self._raw.clear()
        self._clear_persistence()
        self._release_count = 0

    def _clear_persistence(self):
        self._direction = None
        self._direction_count = 0
        self._new_estimates = 0

    def _result(self, now, raw, before, action, reason, eligible, corrected=None):
        return {
            "time_seconds": now if _number(now) else None,
            "raw_error_db": raw if _number(raw) else None,
            "offset_db": self.offset_db,
            "total_before_db": before,
            "delta_db": self.offset_db - before if action == "propose" else 0.0,
            "effective_gain": 10 ** (self.offset_db / 20),
            "action": action,
            "reason": reason,
            "eligible": eligible,
            "revision": self.revision,
            "anchor_revision": self.anchor_revision,
            "corrected_error_db": corrected,
            "direction_count": self._direction_count,
            "new_estimates_since_move": self._new_estimates,
            "raw_history_count": len(self._raw),
            "suspended": self.suspended,
            "timing_status": self._timing_status,
        }

    def _invalid(self, now, raw, before, reason, *, suspend=False):
        self._clear_evidence()
        if suspend:
            self.suspended = True
            self._suspension_reason = reason
        return self._result(
            now, raw, before, "suspended" if self.suspended else "abstain", reason, False
        )

    def rearm(self, time_seconds, revision, anchor_revision):
        """Explicitly accept a new virtual baseline; perform no restore action.

        Both identities must differ from the previously accepted identities.
        Time may equal the last observation time (a same-event explicit rearm),
        but may not rewind the logical clock. The next step must be later.
        """
        if not _number(time_seconds) or time_seconds < 0:
            raise ValueError("Rearm requires a finite, nonnegative event time")
        if self._last_time is not None and time_seconds < self._last_time:
            raise ValueError("Rearm cannot rewind the logical clock")
        if not _revision(revision) or not _revision(anchor_revision):
            raise ValueError("Rearm requires revision and anchor_revision strings")
        if revision == self.revision or anchor_revision == self.anchor_revision:
            raise ValueError("Rearm requires a new revision and a new anchor_revision")
        before = self.offset_db
        self.revision, self.anchor_revision = revision, anchor_revision
        self.offset_db = 0.0
        self.suspended = False
        self._suspension_reason = None
        self._clear_evidence()
        self._attenuating = False
        self._last_time = time_seconds
        self._last_evaluation_time = time_seconds
        self._timing_status = "rearmed"
        self._last_move = None
        self._rearmed_at = time_seconds
        return self._result(time_seconds, None, before, "rearmed", "explicit_rearm", False)

    def step(self, observation):
        """Consume one scheduled estimate; never retain observation dictionaries."""
        if not isinstance(observation, dict):
            raise TypeError("observation must be a dict")
        now, raw = observation.get("time_seconds"), observation.get("raw_error_db")
        self._timing_status = None
        before = self.offset_db
        if not _number(now) or now < 0:
            return self._invalid(now, raw, before, "invalid_event_time", suspend=True)
        if self._last_time is not None and now <= self._last_time:
            return self._invalid(now, raw, before, "nonmonotonic_event_time", suspend=True)
        self._last_time = now
        if self.suspended:
            return self._invalid(now, raw, before, self._suspension_reason)
        if (observation.get("revision") != self.revision
                or observation.get("anchor_revision") != self.anchor_revision):
            return self._invalid(now, raw, before, "revision_changed", suspend=True)
        if observation.get("completed_revision", self.revision) != self.revision:
            return self._invalid(now, raw, before, "inflight_revision_changed", suspend=True)

        self._timing_status = "first_evaluation"
        if self._last_evaluation_time is not None:
            interval = now - self._last_evaluation_time
            if interval < _POLICY["evaluation_interval_seconds"]:
                self._timing_status = "too_soon"
                return self._invalid(now, raw, before, "evaluation_too_soon")
            if interval > _POLICY["evaluation_interval_seconds"]:
                self._clear_evidence()
                self._timing_status = "missed_evaluation_reset"
            else:
                self._timing_status = "scheduled"
        self._last_evaluation_time = now

        age = observation.get("observation_age_seconds")
        state_age = observation.get("state_age_seconds")
        contiguous = observation.get("contiguous_seconds")
        if not _number(age) or not 0 <= age < 0.5:
            return self._invalid(now, raw, before, "observation_stale_or_invalid_age")
        if not _number(state_age) or not 0 <= state_age <= 2.5:
            return self._invalid(now, raw, before, "state_stale_or_invalid_age")
        if not _number(contiguous) or contiguous < 8:
            return self._invalid(now, raw, before, "fresh_history_required")
        if self._rearmed_at is not None and now - self._rearmed_at < 8:
            return self._invalid(now, raw, before, "fresh_history_after_rearm_required")
        if observation.get("eligible") is not True:
            reason = observation.get("reason")
            reason = reason[:512] if isinstance(reason, str) and reason else "ineligible"
            return self._invalid(now, raw, before, reason)
        if not _number(raw):
            return self._invalid(now, raw, before, "nonfinite_or_missing_drift")

        self._raw.append(float(raw))
        self._new_estimates = min(3, self._new_estimates + 1)
        self._release_count = min(3, self._release_count + 1) if abs(raw) <= 0.5 else 0
        corrected = median(self._raw) + self.offset_db
        direction = None
        proposal_reason = None
        if self.offset_db < 0 and (abs(raw) <= 0.5 or corrected < -0.75):
            direction = "release"
            proposal_reason = (
                "release_raw_returned" if self._release_count >= 3 else "relieve_overcorrection"
            )
        elif corrected >= 1.5 or (self._attenuating and corrected > 0.75):
            direction = "attenuate"
            proposal_reason = "attenuate_drift"
        else:
            self._attenuating = False

        if direction is None:
            self._direction, self._direction_count = None, 0
            return self._result(now, raw, before, "hold", "deadband", True, corrected)
        self._direction_count = (
            min(3, self._direction_count + 1) if direction == self._direction else 1
        )
        self._direction = direction
        if direction == "attenuate" and self.offset_db == -3:
            return self._result(now, raw, before, "hold", "cap_unresolved", True, corrected)
        # The first two in-band raw estimates count toward release direction,
        # but cannot themselves authorize release. The same third estimate may
        # complete both raw-return and direction persistence; no second vote
        # sequence is added. Overcorrection has its own corrected-error rule.
        if direction == "release" and self._release_count < 3 and corrected >= -0.75:
            return self._result(now, raw, before, "hold", "release_evidence_required", True,
                                corrected)
        if (len(self._raw) < 3 or self._new_estimates < 3 or self._direction_count < 3):
            return self._result(now, raw, before, "hold", "direction_evidence_required", True,
                                corrected)
        if self._last_move is not None and now - self._last_move < 10:
            return self._result(now, raw, before, "hold", "cadence_wait", True, corrected)

        self.offset_db = max(-3.0, min(0.0, self.offset_db + (
            -0.5 if direction == "attenuate" else 0.5
        )))
        self._last_move = now
        self._attenuating = direction == "attenuate"
        self._clear_persistence()
        # Three fresh raw-in-band estimates are required for each release move,
        # just as three fresh eligible estimates are required for attenuation.
        self._release_count = 0
        return self._result(now, raw, before, "propose", proposal_reason, True, corrected)
