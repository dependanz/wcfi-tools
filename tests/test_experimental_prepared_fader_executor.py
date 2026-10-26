"""Fake transport only; no host, audio, emulator or filesystem side effects."""
import copy
import math

import pytest

from wcfi_tools.audio.experimental_prepared_fader_executor import (
    ExperimentalPreparedFaderExecutor, FaderExecutorError,
)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Host:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.delay, self.write_delay = 0.0, 0.0
        self.fail_after_write = False
        self.original_raw = .673819245
        self.snapshot = {"session_id": "session", "inspected_at": 0,
                         "tracks": [{"id": "ac", "routing": "Master", "mute": False,
                                     "volume": {"id": "vol", "min": 0., "max": 1.,
                                                "value": self.original_raw, "display": "-6.0 dB",
                                                "is_enabled": True, "automation_state": 0}}]}

    @property
    def volume(self):
        return self.snapshot["tracks"][0]["volume"]

    @property
    def writes(self):
        return [args for op, args in self.calls if op == "set_fader" and not args["dry_run"]]

    def call(self, op, args=None, *, request_id=None):
        assert op in ("inspect_session", "set_fader")
        self.calls.append((op, copy.deepcopy(args)))
        self.clock.value += self.delay
        if op == "inspect_session":
            return copy.deepcopy(self.snapshot)
        assert request_id and args["session_id"] == "session" and args["track_id"] == "ac"
        if args["expected_value"] != self.volume["value"]:
            raise RuntimeError("native CAS conflict")
        before = copy.deepcopy(self.volume)
        if args["db"] is None:
            raw = args["raw_value"]
            db = math.sqrt(raw / self.original_raw) * 114 - 120
        else:
            db = args["db"]
            raw = self.original_raw * ((db + 120) / 114) ** 2
        result = {"before": before, "requested_value": raw,
                  "requested_display": f"{db:.1f} dB", "dry_run": args["dry_run"]}
        if args["dry_run"]:
            return result
        self.clock.value += self.write_delay
        self.volume.update(value=raw, display=f"{db:.1f} dB")
        if self.fail_after_write:
            raise TimeoutError("application outcome lost")
        return dict(result, after=copy.deepcopy(self.volume), verified=True)


def setup():
    clock, events = Clock(), []
    host = Host(clock)
    executor = ExperimentalPreparedFaderExecutor(host, clock, events.append)
    executor.arm(host.snapshot, reference_id="reference", session_id="session", track_id="ac",
                 volume_id="vol", received_at=0., live_qualified=True)
    clock.value = 10.
    return host, clock, events, executor


def decide(executor, prepared, clock, **changes):
    args = {"offset_db": prepared["offset_db"], "reference_id": prepared["reference_id"],
            "state_fingerprint": prepared["state_fingerprint"], "proposed_at": clock.value,
            "measurement_received_at": clock.value, "policy_valid": True, "live_qualified": True}
    args.update(changes)
    return executor.apply(prepared["prepared_id"], **args)


def test_slow_preflight_then_new_decision_uses_exact_prepared_native_target():
    host, clock, events, executor = setup()
    host.delay = .7
    prepared = executor.prepare(-.5, reference_id="reference")
    assert clock.value == pytest.approx(12.1)
    assert not host.writes and executor.status["confirmed_offset_db"] == 0
    assert prepared["prepared_at"] == clock.value
    host.delay = 0
    clock.value += .1
    count = len(host.calls)
    result = decide(executor, prepared, clock)
    assert host.calls[count][0] == "set_fader"  # No new preflight before dispatch.
    assert host.writes[0]["raw_value"] == prepared["target_raw"]
    assert host.writes[0]["expected_value"] == prepared["expected_raw"]
    assert host.writes[0]["db"] is None
    assert result["confirmed_offset_db"] == -.5
    assert events[-1]["event"] == "confirmed"


@pytest.mark.parametrize("which", ["before", "exact_finish"])
def test_old_measurement_cannot_be_refreshed_by_new_proposal_time(which):
    host, clock, _, executor = setup()
    host.delay = .7
    prepared = executor.prepare(-.5, reference_id="reference")
    old = 10. if which == "before" else prepared["prepared_at"]
    clock.value += .1
    with pytest.raises(FaderExecutorError, match="predates_preparation"):
        decide(executor, prepared, clock, measurement_received_at=old)
    assert not host.writes and executor.status["state"] == "stopped"


@pytest.mark.parametrize("field,value", [("reference_id", "other"), ("state_fingerprint", "other"),
                                        ("offset_db", -.0), ("policy_valid", False),
                                        ("live_qualified", False)])
def test_new_decision_must_match_prepared_move_and_qualification(field, value):
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    with pytest.raises(FaderExecutorError):
        decide(executor, prepared, clock, **{field: value})
    assert not host.writes


def test_changed_live_value_is_not_adopted_and_native_cas_stops_without_retry():
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    host.volume["value"] = .71
    clock.value += .1
    with pytest.raises(FaderExecutorError, match="operation_failed"):
        decide(executor, prepared, clock)
    assert host.volume["value"] == .71 and len(host.writes) == 1
    assert executor.status["confirmed_offset_db"] == 0
    with pytest.raises(FaderExecutorError, match="not_armed"):
        decide(executor, prepared, clock)
    assert len(host.writes) == 1


def test_delayed_application_is_reported_as_unknown_freshness_not_misrepresented():
    host, clock, events, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    host.write_delay = 2.
    result = decide(executor, prepared, clock)
    assert result["confirmed_offset_db"] == -.5
    assert result["freshness_scope"] == "dispatch_only"
    assert result["host_application_freshness_verified"] is False
    assert result["last_dispatch"]["rpc_elapsed_seconds"] == 2.
    assert result["last_dispatch"]["measurement_age_at_dispatch"] == 0
    assert result["last_dispatch"]["host_applied_at"] is None
    assert events[-1]["host_application_freshness_verified"] is False


def test_state_age_includes_final_inspection_latency_not_just_response_age():
    host, clock, _, executor = setup()
    host.delay = 2.
    prepared = executor.prepare(-.5, reference_id="reference")
    host.delay = 0
    clock.value += .6
    with pytest.raises(FaderExecutorError, match="stale_prepared_state"):
        decide(executor, prepared, clock)
    assert not host.writes


def test_preparation_too_slow_to_supply_state_is_rejected_without_writes():
    host, _, _, executor = setup()
    host.delay = 2.6
    with pytest.raises(FaderExecutorError, match="stale_prepared_state"):
        executor.prepare(-.5, reference_id="reference")
    assert not host.writes


def test_journal_delay_cannot_send_stale_decision():
    host, clock, events, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    def sink(event):
        events.append(event)
        if event["event"] == "write_pending":
            clock.value += .5
    executor._sink = sink
    with pytest.raises(FaderExecutorError, match="stale_measurement"):
        decide(executor, prepared, clock)
    assert not host.writes


def test_unknown_write_outcome_stops_and_never_restores_or_retries():
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    host.fail_after_write = True
    with pytest.raises(FaderExecutorError, match="operation_failed"):
        decide(executor, prepared, clock)
    assert host.volume["value"] != host.original_raw
    assert executor.status["confirmed_offset_db"] == 0
    assert executor.status["state"] == "stopped"
    assert executor.status["last_dispatch"]["host_application_freshness_verified"] is False
    with pytest.raises(FaderExecutorError):
        executor.prepare(-.5, reference_id="reference")
    assert len(host.writes) == 1


@pytest.mark.parametrize("offset", [-3.5, .5, -1., float("nan")])
def test_invalid_prepared_steps_have_no_host_effect(offset):
    host, _, _, executor = setup()
    with pytest.raises(FaderExecutorError):
        executor.prepare(offset, reference_id="reference")
    assert not host.calls


def test_confirmed_readback_cadence_and_exact_native_baseline_restoration():
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    host.write_delay = 1.
    first = decide(executor, prepared, clock)
    host.write_delay = 0
    clock.value = first["confirmed_at"] + 10
    restore = executor.prepare(0., reference_id="reference")
    clock.value += .1
    second = decide(executor, restore, clock)
    assert second["confirmed_offset_db"] == 0
    assert host.volume["value"] == host.original_raw
    assert host.writes[-1]["raw_value"] == host.original_raw


def test_too_early_next_dispatch_stops_even_if_target_was_prepared():
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    decide(executor, prepared, clock)
    next_move = executor.prepare(-1., reference_id="reference")
    clock.value += .1
    with pytest.raises(FaderExecutorError, match="cadence_violation"):
        decide(executor, next_move, clock)
    assert len(host.writes) == 1


def test_caller_cannot_mutate_or_replace_pending_preparation():
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    prepared["target_raw"] = .99
    clock.value += .1
    decide(executor, prepared, clock)
    assert host.volume["value"] != .99
    pending = executor.prepare(-1., reference_id="reference")
    assert pending
    with pytest.raises(FaderExecutorError, match="prepared_move_pending"):
        executor.prepare(-1., reference_id="reference")
    assert len(host.writes) == 1


def test_expired_idle_preparation_can_be_discarded_without_rearming_or_recentering():
    host, clock, events, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    before = executor.status
    count = len(host.calls)
    clock.value += 3
    discarded = executor.discard_preparation(prepared["prepared_id"])
    assert len(host.calls) == count and discarded["prepared_id"] is None
    for key in ("state", "reference_id", "state_fingerprint", "baseline_raw", "baseline_db",
                "confirmed_raw", "confirmed_db", "confirmed_offset_db", "confirmed_at"):
        assert discarded[key] == before[key]
    assert events[-1]["event"] == "preparation_discarded"
    replacement = executor.prepare(-.5, reference_id="reference")
    assert replacement["prepared_id"] != prepared["prepared_id"]
    assert replacement["expected_raw"] == prepared["expected_raw"]
    assert replacement["prepared_at"] > prepared["prepared_at"]
    clock.value += .1
    result = decide(executor, replacement, clock)
    assert result["confirmed_offset_db"] == -.5 and len(host.writes) == 1


def test_refresh_does_not_refresh_an_old_decision_timestamp():
    host, clock, _, executor = setup()
    old = executor.prepare(-.5, reference_id="reference")
    old_measurement = clock.value + .1
    clock.value += 3
    executor.discard_preparation(old["prepared_id"])
    fresh = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    with pytest.raises(FaderExecutorError, match="predates_preparation"):
        decide(executor, fresh, clock, measurement_received_at=old_measurement)
    assert not host.writes


def test_discard_cannot_clear_fault_or_cancel_a_consumed_ticket():
    host, clock, _, executor = setup()
    prepared = executor.prepare(-.5, reference_id="reference")
    clock.value += .1
    decide(executor, prepared, clock)
    with pytest.raises(FaderExecutorError, match="prepared_move_mismatch"):
        executor.discard_preparation(prepared["prepared_id"])
    assert executor.status["state"] == "stopped"
    with pytest.raises(FaderExecutorError, match="not_armed"):
        executor.discard_preparation(prepared["prepared_id"])
    assert len(host.writes) == 1
