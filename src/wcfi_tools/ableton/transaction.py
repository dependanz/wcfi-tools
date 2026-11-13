"""Journaled, reversible bridge edits; no transport or desktop operations.

Stopped transport is checked around calls, not atomically with Live's writes.
An uncertain response is never retried blindly: observed values determine what
can safely be restored. Unrelated routing/chain guards belong to the caller.
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from pathlib import Path

from .client import AbletonError, choose, choose_track, devices


class ReversibleTrial:
    """Make bounded edits, then call ``restore()`` in the caller's finally block."""

    def __init__(self, client, journal_path):
        self.client = client
        self.journal_path = Path(journal_path)
        if self.journal_path.exists():
            raise FileExistsError(self.journal_path)
        self.initial_snapshot = copy.deepcopy(client.call("inspect_session"))
        self._check(self.initial_snapshot)
        self._owned = {}
        self.journal = {"kind": "wcfi_reversible_ableton_trial_v1",
                        "initial_snapshot": self.initial_snapshot,
                        "entries": [], "restored": False}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("x", encoding="utf-8") as stream:
            json.dump(self.journal, stream, indent=2, allow_nan=False)

    def _check(self, snapshot):
        if snapshot.get("transport") != {"is_playing": False, "record_mode": False}:
            raise AbletonError("Trial requires stopped Play and Record", code="transport_changed")
        initial = getattr(self, "initial_snapshot", None)
        if initial and snapshot.get("session_id") != initial.get("session_id"):
            raise AbletonError("Trial session changed; refusing further writes", code="stale_session")

    def _snapshot(self):
        snapshot = self.client.call("inspect_session")
        self._check(snapshot)
        return snapshot

    def _save(self):
        payload = json.dumps(self.journal, indent=2, allow_nan=False)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                         dir=self.journal_path.parent, suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, self.journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _value(snapshot, target):
        track = choose_track(snapshot, target["track_id"])
        if target["kind"] == "fader":
            return float(track["volume"]["value"])
        device = choose(list(devices(track)), target["device_id"], "Device")
        return float(choose(device["parameters"], target["parameter_id"], "Parameter")["value"])

    @staticmethod
    def _key(target):
        return target.get("parameter_id", target["track_id"])

    @staticmethod
    def _same(left, right):
        return abs(left - right) <= 1e-6

    def _ready(self):
        if self.journal["restored"]:
            raise AbletonError("Trial was restored; create a new trial", code="trial_closed")
        if any(e.get("status") in {"outcome_unknown", "conflict"}
               and not e.get("restored") for e in self.journal["entries"]):
            raise AbletonError("Reconcile or restore the previous write first", code="uncertain_write")

    def set_fader(self, track, db):
        self._ready()
        selected = choose_track(self.initial_snapshot, track)
        target = {"kind": "fader", "track_id": selected["id"]}
        return self._edit("set_fader", target, {"track_id": selected["id"], "db": db})

    def set_parameter(self, track, device, parameter, value):
        self._ready()
        selected = choose_track(self.initial_snapshot, track)
        effect = choose(list(devices(selected)), device, "Device")
        control = choose(effect["parameters"], parameter, "Parameter")
        target = {"kind": "parameter", "track_id": selected["id"],
                  "device_id": effect["id"], "parameter_id": control["id"]}
        return self._edit("set_parameter", target,
                          {"parameter_id": control["id"], "value": value})

    def _edit(self, operation, target, arguments):
        key = self._key(target)
        before = self._owned.get(key, self._value(self.initial_snapshot, target))
        return self._write(operation, target, arguments, before, phase="edit")

    def _write(self, operation, target, arguments, before, *, phase, undo_of=None):
        snapshot = self._snapshot()
        if not self._same(self._value(snapshot, target), before):
            raise AbletonError("Control changed concurrently; refusing to overwrite it", code="conflict")
        args = dict(arguments, session_id=snapshot["session_id"], expected_value=before)
        entry = {"phase": phase, "operation": operation, "target": target,
                 "before_raw": before, "initial_raw": self._value(self.initial_snapshot, target),
                 "dry_run_arguments": dict(args, dry_run=True), "status": "dry_run_pending"}
        if undo_of is not None:
            entry["undo_of"] = undo_of
        self.journal["entries"].append(entry)
        self._save()
        try:
            plan = self.client.call(operation, entry["dry_run_arguments"])
            intended = float(plan["requested_value"])
            if not math.isfinite(intended):
                raise ValueError("Bridge returned a nonfinite intended value")
            entry.update(dry_run_result=plan, intended_raw=intended)
            self._save()
            latest = self._snapshot()
            if not self._same(self._value(latest, target), before):
                raise AbletonError("Control changed after dry run", code="conflict")
        except Exception as exc:
            entry.update(status="dry_run_failed", error=str(exc))
            self._save()
            raise
        entry.update(write_arguments=args, status="write_pending")
        self._save()  # Persist intent before a call whose response might be lost.
        failure = None
        result = None
        try:
            result = self.client.call(operation, args)
            entry["result"] = result
        except Exception as exc:  # noqa: BLE001 - preserve every uncertain RPC outcome for rollback
            failure = exc
            entry["error"] = str(exc)
        try:
            observed = self._value(self._snapshot(), target)
            entry["observed_raw"] = observed
            if self._same(observed, intended):
                entry["status"] = "no_change" if self._same(before, intended) else "applied"
                self._owned[self._key(target)] = observed
            elif self._same(observed, before):
                entry["status"] = "not_applied"
            else:
                entry["status"] = "conflict"
        except Exception as exc:  # noqa: BLE001 - failed readback must retain the pending write
            entry.update(status="outcome_unknown", reconciliation_error=str(exc))
        self._save()
        if failure is not None:
            raise failure
        if entry["status"] not in {"applied", "no_change"} or not result.get("verified"):
            raise AbletonError("Write was not verified; inspect the trial journal", code="unverified_write")
        return dict(result, observed_value=entry["observed_raw"])

    def restore(self):
        """Restore owned values in reverse order, rejecting unknown operator edits.

        Safe independent controls are restored even if another control conflicts.
        Calling again retries unresolved restoration, never a completed write.
        """
        errors, blocked = [], set()
        originals = list(enumerate(self.journal["entries"]))
        for index, entry in reversed(originals):
            if entry["phase"] != "edit" or entry.get("restored"):
                continue
            if entry["status"] in {"dry_run_failed", "no_change", "not_applied"}:
                entry["restored"] = True
                continue
            target, before = entry["target"], entry["before_raw"]
            key = self._key(target)
            if key in blocked:
                continue
            try:
                now = self._value(self._snapshot(), target)
                intended = entry["intended_raw"]
                if self._same(now, before):
                    entry["restored"] = True
                    self._owned[key] = now
                    continue
                if not self._same(now, intended):
                    raise AbletonError("Control changed concurrently; rollback withheld", code="conflict")
                if target["kind"] == "fader":
                    args = {"track_id": target["track_id"], "db": None, "raw_value": before}
                else:
                    args = {"parameter_id": target["parameter_id"], "value": before}
                count = len(self.journal["entries"])
                try:
                    self._write(entry["operation"], target, args, now, phase="restore", undo_of=index)
                except Exception:
                    # A missing response with verified rollback is still safely restored.
                    attempts = self.journal["entries"][count:]
                    if not attempts or attempts[-1]["status"] not in {"applied", "no_change"}:
                        raise
                entry["restored"] = True
            except Exception as exc:  # noqa: BLE001 - continue restoring independent owned controls
                entry["restore_error"] = str(exc)
                errors.append({"target": target, "error": str(exc)})
                blocked.add(key)
            self._save()
        self.journal["restored"] = all(e.get("restored") for e in self.journal["entries"]
                                       if e["phase"] == "edit")
        self._save()
        if errors:
            raise AbletonError("Some controls could not be safely restored", code="restore_incomplete",
                               details={"errors": errors})
        self._snapshot()
        return {"restored": self.journal["restored"], "journal_path": str(self.journal_path)}
