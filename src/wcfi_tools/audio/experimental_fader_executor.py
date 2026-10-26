"""Experimental single-fader coordinator; injected dependencies, no startup I/O.

All timestamps use the injected monotonic clock. The caller supplies genuinely
qualified live measurements and serializes calls; this class cannot qualify an
audio path. Only inspect_session and set_fader RPCs are used. A synchronous
journal sink must durably record each event before returning in production.

The existing bridge has a five-second pending-request expiry, not a decision
lease or cancellation API. Its native-value CAS does not atomically protect
topology. Manual moves away and back between inspections are undetectable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid


class FaderExecutorError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FaderExecutorError("invalid_number")
    return float(value)


def _db(display):
    if not isinstance(display, str):
        raise FaderExecutorError("invalid_fader_display")
    text = display.strip().lower().replace("\u2212", "-").replace(",", ".")
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*db", text)
    if not match:
        raise FaderExecutorError("invalid_fader_display")
    return _number(float(match.group(1)))


class ExperimentalFaderExecutor:
    """One explicit binding, one owned value, and a permanent stop on any fault.

    Construction and arm() never call the host. arm() consumes a fresh existing
    inspection. The first move waits ten seconds after that baseline receipt;
    subsequent moves wait ten seconds after confirmed readback. There is no
    rearm, automatic restoration, transport control, or implicit reconnection.
    """

    MAX_MEASUREMENT_AGE = 0.5
    MIN_STEP_SECONDS = 10.0
    RAW_TOLERANCE = 1e-6

    def __init__(self, client, clock, journal_sink):
        self._client, self._clock, self._sink = client, clock, journal_sink
        self._state = "unarmed"
        self._stop_reason = None
        self._last_clock = None
        self._busy = False
        self._binding = None
        self._fingerprint = None
        self._baseline_raw = self._baseline_db = None
        self._owned_raw = self._owned_db = None
        self._offset_db = 0.0
        self._confirmed_at = None

    @property
    def status(self):
        return {"state": self._state, "stop_reason": self._stop_reason,
                "binding": copy.deepcopy(self._binding), "state_fingerprint": self._fingerprint,
                "baseline_raw": self._baseline_raw, "baseline_db": self._baseline_db,
                "confirmed_raw": self._owned_raw, "confirmed_db": self._owned_db,
                "confirmed_offset_db": self._offset_db, "confirmed_at": self._confirmed_at}

    def _record(self, event, **values):
        try:
            self._sink(copy.deepcopy({"event": event, **values}))
        except Exception as exc:
            self._state, self._stop_reason = "stopped", "journal_failed"
            raise FaderExecutorError("journal_failed") from exc

    def stop(self, reason="caller_stopped"):
        """Yield ownership without issuing any RPC or restoring the baseline."""
        if self._state != "stopped":
            self._state, self._stop_reason = "stopped", str(reason)
            self._record("stopped", reason=self._stop_reason,
                         confirmed_raw=self._owned_raw, confirmed_offset_db=self._offset_db)
        return self.status

    def _fail(self, code):
        self.stop(code)
        raise FaderExecutorError(code)

    def _now(self):
        now = _number(self._clock())
        if self._last_clock is not None and now < self._last_clock:
            self._fail("clock_reversed")
        self._last_clock = now
        return now

    def _fresh(self, now, received_at, proposed_at=None):
        received_at = _number(received_at)
        if not 0 <= now - received_at < self.MAX_MEASUREMENT_AGE:
            self._fail("stale_measurement")
        if proposed_at is not None and not received_at <= proposed_at <= now:
            self._fail("invalid_proposal_clock")

    def _target(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("session_id") != self._binding["session_id"]:
            self._fail("session_changed")
        tracks = snapshot.get("tracks")
        if not isinstance(tracks, list):
            self._fail("invalid_inspection")
        matches = [t for t in tracks if isinstance(t, dict) and t.get("id") == self._binding["track_id"]]
        if len(matches) != 1:
            self._fail("track_changed")
        track = matches[0]
        parameter = track.get("volume")
        if not isinstance(parameter, dict) or parameter.get("id") != self._binding["volume_id"]:
            self._fail("volume_changed")
        if (parameter.get("is_enabled") is not True
                or type(parameter.get("automation_state")) is not int
                or parameter["automation_state"] != 0):
            self._fail("fader_not_writable")
        value = _number(parameter.get("value"))
        if not _number(parameter.get("min")) <= value <= _number(parameter.get("max")):
            self._fail("invalid_native_value")
        _db(parameter.get("display"))
        return parameter

    def _state_hash(self, snapshot):
        frozen = copy.deepcopy(snapshot)
        frozen.pop("inspected_at", None)
        for track in frozen["tracks"]:
            if track.get("id") == self._binding["track_id"]:
                track["volume"].pop("value", None)
                track["volume"].pop("display", None)
        payload = json.dumps(frozen, sort_keys=True, allow_nan=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _inspect(self, snapshot, expected_raw):
        parameter = self._target(snapshot)
        if self._state_hash(snapshot) != self._fingerprint:
            self._fail("state_changed")
        if abs(parameter["value"] - expected_raw) > self.RAW_TOLERANCE:
            self._fail("manual_override_or_conflict")
        return parameter

    def arm(self, snapshot, *, session_id, track_id, volume_id, received_at, live_qualified):
        if self._state != "unarmed":
            self._fail("arm_not_allowed")
        try:
            if live_qualified is not True:
                self._fail("live_path_unqualified")
            if any(not isinstance(v, str) or not v for v in (session_id, track_id, volume_id)):
                self._fail("invalid_binding")
            now = self._now()
            self._fresh(now, received_at)
            self._binding = {"session_id": session_id, "track_id": track_id, "volume_id": volume_id}
            parameter = self._target(snapshot)
            self._fingerprint = self._state_hash(snapshot)
            self._baseline_raw = self._owned_raw = float(parameter["value"])
            self._baseline_db = self._owned_db = _db(parameter["display"])
            self._confirmed_at = now
            self._state = "armed"
            self._record("armed", **self.status, inspection_received_at=received_at)
            return self.status
        except Exception as exc:
            self.stop(getattr(exc, "code", "invalid_inspection"))
            raise

    def _plan(self, plan, target_db, parameter, offset_db):
        if not isinstance(plan, dict) or plan.get("dry_run") is not True:
            self._fail("invalid_dry_run")
        before = plan.get("before", {})
        if (before.get("id") != self._binding["volume_id"]
                or abs(_number(before.get("value")) - self._owned_raw) > self.RAW_TOLERANCE):
            self._fail("dry_run_before_mismatch")
        raw = _number(plan.get("requested_value"))
        if not parameter["min"] <= raw <= parameter["max"]:
            self._fail("dry_run_native_range")
        if (raw - self._owned_raw) * (offset_db - self._offset_db) <= 0:
            self._fail("dry_run_direction_mismatch")
        if abs(_db(plan.get("requested_display")) - target_db) > 0.11:
            self._fail("dry_run_display_mismatch")
        return raw

    def apply(self, offset_db, *, proposed_at, measurement_received_at, policy_valid, live_qualified):
        """Apply one eligible proposal; errors stop the run without a retry/undo.

        policy_valid/live_qualified are explicit caller attestations, not facts
        inferred by this executor. No new measurement can renew an old proposal.
        """
        if self._state != "armed":
            self._fail("not_armed")
        if self._busy:
            self._fail("concurrent_call")
        self._busy = True
        try:
            offset_db, proposed_at = _number(offset_db), _number(proposed_at)
            measurement_received_at = _number(measurement_received_at)
            self._record("proposal", proposed_offset_db=offset_db, proposed_at=proposed_at,
                         measurement_received_at=measurement_received_at,
                         confirmed_offset_db=self._offset_db, confirmed_raw=self._owned_raw)
            now = self._now()
            self._fresh(now, measurement_received_at, proposed_at)
            if live_qualified is not True or policy_valid is not True:
                self._fail("unqualified_proposal")
            if not -3 <= offset_db <= 0 or abs(abs(offset_db - self._offset_db) - 0.5) > 1e-9:
                self._fail("invalid_step_or_bounds")
            if now - self._confirmed_at < self.MIN_STEP_SECONDS:
                self._fail("cadence_violation")
            snapshot = self._client.call("inspect_session")
            parameter = self._inspect(snapshot, self._owned_raw)
            target_db = self._baseline_db + offset_db
            arguments = {"session_id": self._binding["session_id"],
                         "track_id": self._binding["track_id"], "expected_value": self._owned_raw,
                         "db": target_db, "relative": False}
            if offset_db == 0:
                arguments.update(db=None, raw_value=self._baseline_raw)
            self._fresh(self._now(), measurement_received_at, proposed_at)
            dry_args = dict(arguments, dry_run=True)
            dry_id = uuid.uuid4().hex
            self._record("dry_run_pending", request_id=dry_id, arguments=dry_args)
            plan = self._client.call("set_fader", dry_args, request_id=dry_id)
            intended = self._plan(plan, target_db, parameter, offset_db)
            if offset_db == 0 and abs(intended - self._baseline_raw) > self.RAW_TOLERANCE:
                self._fail("dry_run_restore_mismatch")
            snapshot = self._client.call("inspect_session")
            self._inspect(snapshot, self._owned_raw)
            self._fresh(self._now(), measurement_received_at, proposed_at)
            if self._state != "armed":
                self._fail("run_stopped")
            write_args = dict(arguments, dry_run=False)
            write_id = uuid.uuid4().hex
            self._record("write_pending", request_id=write_id, arguments=write_args,
                         intended_raw=intended, proposed_offset_db=offset_db)
            self._fresh(self._now(), measurement_received_at, proposed_at)
            if self._state != "armed":
                self._fail("run_stopped")
            result = self._client.call("set_fader", write_args, request_id=write_id)
            if (not isinstance(result, dict) or result.get("verified") is not True
                    or result.get("dry_run") is not False):
                self._fail("unverified_write")
            after = result.get("after", {})
            before = result.get("before", {})
            if (before.get("id") != self._binding["volume_id"]
                    or abs(_number(before.get("value")) - self._owned_raw) > self.RAW_TOLERANCE
                    or after.get("id") != self._binding["volume_id"]
                    or abs(_number(result.get("requested_value")) - intended) > self.RAW_TOLERANCE
                    or abs(_number(after.get("value")) - intended) > 1e-5
                    or abs(_db(result.get("requested_display")) - target_db) > 0.11
                    or abs(_db(after.get("display")) - target_db) > 0.11):
                self._fail("write_readback_mismatch")
            current = self._inspect(self._client.call("inspect_session"), after["value"])
            current_db = _db(current["display"])
            if abs(current_db - target_db) > 0.11:
                self._fail("inspection_display_mismatch")
            confirmed_at = self._now()
            self._owned_raw, self._owned_db = float(current["value"]), current_db
            self._offset_db, self._confirmed_at = offset_db, confirmed_at
            self._record("confirmed", request_id=write_id, proposed_at=proposed_at, **self.status)
            return self.status
        except Exception as exc:
            self.stop(getattr(exc, "code", "operation_failed"))
            if isinstance(exc, FaderExecutorError):
                raise
            raise FaderExecutorError(getattr(exc, "code", "operation_failed")) from exc
        finally:
            self._busy = False
