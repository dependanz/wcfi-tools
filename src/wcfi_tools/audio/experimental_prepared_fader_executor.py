"""Prepare a single native fader target before receiving a new live decision.

No startup I/O or policy advancement. Dependencies and all clocks are injected.
Freshness is checked AT DISPATCH ONLY. The existing bridge may queue a request;
this class supplies neither a host application deadline nor cancellation. Native
CAS protects the fader value, not atomic topology or away-and-back manual moves.
"""
from __future__ import annotations

import copy
import uuid

from .experimental_fader_executor import (
    ExperimentalFaderExecutor, FaderExecutorError, _db, _number,
)


class ExperimentalPreparedFaderExecutor(ExperimentalFaderExecutor):
    """One pending prepared target; any conflict or unknown outcome stops it."""

    MAX_STATE_AGE = 2.5

    def __init__(self, client, clock, journal_sink):
        super().__init__(client, clock, journal_sink)
        self._reference_id = None
        self._prepared = None
        self._last_dispatch = None

    @property
    def status(self):
        result = super().status
        result.update(reference_id=self._reference_id,
                      prepared_id=self._prepared["prepared_id"] if self._prepared else None,
                      freshness_scope="dispatch_only",
                      host_application_freshness_verified=False,
                      last_dispatch=copy.deepcopy(self._last_dispatch))
        return result

    def arm(self, snapshot, *, reference_id, **arguments):
        if self._state != "unarmed":
            self._fail("arm_not_allowed")
        if not isinstance(reference_id, str) or not reference_id:
            self._fail("invalid_reference")
        self._reference_id = reference_id
        return super().arm(snapshot, **arguments)

    def _step(self, offset_db):
        offset_db = _number(offset_db)
        if not -3 <= offset_db <= 0 or abs(abs(offset_db - self._offset_db) - .5) > 1e-9:
            self._fail("invalid_step_or_bounds")
        return offset_db

    def _ready(self):
        if self._state != "armed":
            self._fail("not_armed")
        if self._busy:
            self._fail("concurrent_call")

    def _state_fresh(self, now, observed_not_before):
        if not 0 <= now - observed_not_before <= self.MAX_STATE_AGE:
            self._fail("stale_prepared_state")

    def prepare(self, offset_db, *, reference_id):
        """Inspect/dry-run/inspect without a decision, policy change or write.

        The final inspection's request-start time conservatively bounds its age.
        A pending target cannot be replaced to extend its validity.
        """
        self._ready()
        self._busy = True
        try:
            if self._prepared is not None:
                self._fail("prepared_move_pending")
            if reference_id != self._reference_id:
                self._fail("reference_changed")
            offset_db = self._step(offset_db)
            self._now()
            parameter = self._inspect(self._client.call("inspect_session"), self._owned_raw)
            target_db = self._baseline_db + offset_db
            args = {**self._binding, "expected_value": self._owned_raw,
                    "db": target_db, "relative": False, "dry_run": True}
            args.pop("volume_id")  # The native command binds by session/track.
            if offset_db == 0:
                args.update(db=None, raw_value=self._baseline_raw)
            request_id = uuid.uuid4().hex
            self._record("prepare_dry_run_pending", request_id=request_id, arguments=args)
            if self._state != "armed":
                self._fail("run_stopped")
            plan = self._client.call("set_fader", args, request_id=request_id)
            raw = self._plan(plan, target_db, parameter, offset_db)
            if offset_db == 0 and abs(raw - self._baseline_raw) > self.RAW_TOLERANCE:
                self._fail("dry_run_restore_mismatch")
            inspection_started_at = self._now()
            self._inspect(self._client.call("inspect_session"), self._owned_raw)
            prepared = {"prepared_id": uuid.uuid4().hex,
                        "reference_id": self._reference_id,
                        "state_fingerprint": self._fingerprint,
                        "binding": copy.deepcopy(self._binding),
                        "expected_raw": self._owned_raw, "target_raw": raw,
                        "target_db": target_db, "offset_db": offset_db,
                        "state_observed_not_before": inspection_started_at}
            self._record("prepared_candidate", prepared=prepared)
            # Include all slow preparation/journal work before this watermark.
            finished = self._now()
            self._state_fresh(finished, inspection_started_at)
            if self._state != "armed":
                self._fail("run_stopped")
            prepared["prepared_at"] = finished
            self._prepared = prepared
            return copy.deepcopy(prepared)
        except Exception as exc:
            self.stop(getattr(exc, "code", "preparation_failed"))
            if isinstance(exc, FaderExecutorError):
                raise
            raise FaderExecutorError("preparation_failed") from exc
        finally:
            self._busy = False

    def discard_preparation(self, prepared_id):
        """Discard an unconsumed preflight, without changing ownership or gain.

        This does not cancel a policy decision or submitted RPC. The caller must
        stop if its policy has already advanced without a matching usable ticket.
        A replacement requires a complete new prepare() and a later new decision.
        """
        self._ready()
        self._busy = True
        try:
            if self._prepared is None or prepared_id != self._prepared["prepared_id"]:
                self._fail("prepared_move_mismatch")
            self._record("preparation_discarded", prepared_id=prepared_id,
                         discarded_at=self._now(), no_write_submitted=True,
                         confirmed_offset_db=self._offset_db, confirmed_raw=self._owned_raw)
            if self._state != "armed":
                self._fail("run_stopped")
            self._prepared = None
            return self.status
        except Exception as exc:
            self.stop(getattr(exc, "code", "discard_failed"))
            raise
        finally:
            self._busy = False

    def apply(self, prepared_id, *, offset_db, reference_id, state_fingerprint,
              proposed_at, measurement_received_at, policy_valid, live_qualified):
        """Consume one NEW decision matching the prepared move, then submit once.

        No pre-dispatch host inspection and no refresh of the decision timestamps.
        A later response can confirm an applied value but cannot prove it was
        applied within the dispatch measurement-age limit.
        """
        self._ready()
        self._busy = True
        request_id = None
        try:
            prepared = self._prepared
            if prepared is None or prepared_id != prepared["prepared_id"]:
                self._fail("prepared_move_mismatch")
            offset_db = self._step(offset_db)
            proposed_at, received = _number(proposed_at), _number(measurement_received_at)
            if (reference_id != self._reference_id or reference_id != prepared["reference_id"]
                    or state_fingerprint != prepared["state_fingerprint"]
                    or offset_db != prepared["offset_db"]):
                self._fail("prepared_decision_mismatch")
            if policy_valid is not True or live_qualified is not True:
                self._fail("unqualified_proposal")
            if received <= prepared["prepared_at"]:
                self._fail("decision_predates_preparation")
            now = self._now()
            self._fresh(now, received, proposed_at)
            self._state_fresh(now, prepared["state_observed_not_before"])
            if now - self._confirmed_at < self.MIN_STEP_SECONDS:
                self._fail("cadence_violation")
            if abs(self._owned_raw - prepared["expected_raw"]) > self.RAW_TOLERANCE:
                self._fail("prepared_ownership_changed")
            args = {"session_id": prepared["binding"]["session_id"],
                    "track_id": prepared["binding"]["track_id"],
                    "expected_value": prepared["expected_raw"],
                    "raw_value": prepared["target_raw"], "db": None,
                    "relative": False, "dry_run": False}
            request_id = uuid.uuid4().hex
            self._record("write_pending", request_id=request_id, arguments=args,
                         prepared=prepared, proposed_at=proposed_at,
                         measurement_received_at=received,
                         freshness_scope="dispatch_only")
            dispatched = self._now()
            self._fresh(dispatched, received, proposed_at)
            self._state_fresh(dispatched, prepared["state_observed_not_before"])
            if self._state != "armed":
                self._fail("run_stopped")
            self._prepared = None  # Never retry this submission, even if its outcome is lost.
            self._last_dispatch = {"request_id": request_id, "dispatched_at": dispatched,
                                   "measurement_age_at_dispatch": dispatched - received,
                                   "state_age_at_dispatch": dispatched - prepared["state_observed_not_before"],
                                   "host_applied_at": None,
                                   "host_application_freshness_verified": False}
            result = self._client.call("set_fader", args, request_id=request_id)
            returned = self._now()
            self._last_dispatch["rpc_elapsed_seconds"] = returned - dispatched
            if (not isinstance(result, dict) or result.get("verified") is not True
                    or result.get("dry_run") is not False):
                self._fail("unverified_write")
            before, after = result.get("before", {}), result.get("after", {})
            if (before.get("id") != self._binding["volume_id"]
                    or abs(_number(before.get("value")) - prepared["expected_raw"]) > self.RAW_TOLERANCE
                    or after.get("id") != self._binding["volume_id"]
                    or abs(_number(result.get("requested_value")) - prepared["target_raw"]) > self.RAW_TOLERANCE
                    or abs(_number(after.get("value")) - prepared["target_raw"]) > 1e-5
                    or abs(_db(result.get("requested_display")) - prepared["target_db"]) > .11
                    or abs(_db(after.get("display")) - prepared["target_db"]) > .11):
                self._fail("write_readback_mismatch")
            current = self._inspect(self._client.call("inspect_session"), after["value"])
            current_db = _db(current["display"])
            if abs(current_db - prepared["target_db"]) > .11:
                self._fail("inspection_display_mismatch")
            confirmed_at = self._now()
            self._owned_raw, self._owned_db = float(current["value"]), current_db
            self._offset_db, self._confirmed_at = offset_db, confirmed_at
            self._record("confirmed", request_id=request_id, proposed_at=proposed_at, **self.status)
            return self.status
        except Exception as exc:
            self.stop(getattr(exc, "code", "operation_failed"))
            if isinstance(exc, FaderExecutorError):
                raise
            raise FaderExecutorError("operation_failed") from exc
        finally:
            self._busy = False
