"""Injected fake host only: no filesystem, network, audio, or Ableton access."""
import copy
import math

import pytest

from wcfi_tools.audio.experimental_fader_executor import (
    ExperimentalFaderExecutor,
    FaderExecutorError,
)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class FakeClient:
    def __init__(self):
        self.snapshot = {
            "session_id": "session-a", "inspected_at": 1000,
            "transport": {"is_playing": True, "record_mode": False},
            "tracks": [{"id": "track-ac", "name": "AC GUITAR", "kind": "track",
                        "routing": {"output_routing_type": "Master"}, "devices": [],
                        "mute": False, "solo": False,
                        "volume": {"id": "volume-ac", "value": 0.673819245,
                                   "min": 0.0, "max": 1.0, "display": "-6.0 dB",
                                   "is_enabled": True, "automation_state": 0}}]}
        self.calls = []
        self.dry_hook = self.write_hook = self.inspect_hook = None
        self.failure = None
        self.verified = True
        self.original_raw = self.volume["value"]

    @property
    def volume(self):
        return self.snapshot["tracks"][0]["volume"]

    @property
    def writes(self):
        return [args for op, args in self.calls if op == "set_fader" and not args["dry_run"]]

    def call(self, operation, arguments=None, *, request_id=None):
        assert operation in {"inspect_session", "set_fader"}
        self.calls.append((operation, copy.deepcopy(arguments)))
        if operation == "inspect_session":
            if self.inspect_hook:
                self.inspect_hook(self)
            return copy.deepcopy(self.snapshot)
        assert request_id and len(request_id) == 32
        assert arguments["session_id"] == self.snapshot["session_id"]
        assert arguments["track_id"] == "track-ac"
        assert arguments["expected_value"] == self.volume["value"]
        assert arguments["relative"] is False
        before = copy.deepcopy(self.volume)
        if arguments["db"] is None:
            raw, db = arguments["raw_value"], -6.0
        else:
            db = arguments["db"]
            # Deliberately nonlinear and distinct from an amplitude multiplier.
            raw = self.original_raw * ((db + 120) / 114) ** 2
        plan = {"before": before, "requested_value": raw,
                "requested_display": f"{db:.1f} dB", "dry_run": arguments["dry_run"]}
        if arguments["dry_run"]:
            if self.dry_hook:
                self.dry_hook(self)
            return plan
        if self.write_hook:
            self.write_hook(self)
        if self.failure == "before":
            raise TimeoutError("unknown outcome")
        self.volume.update(value=raw, display=f"{db:.1f} dB")
        if self.failure == "after":
            raise TimeoutError("response lost after application")
        return dict(plan, verified=self.verified, after=copy.deepcopy(self.volume), actual_db=db)


def setup_executor():
    host, clock, events = FakeClient(), Clock(), []
    executor = ExperimentalFaderExecutor(host, clock, events.append)
    assert not host.calls
    executor.arm(copy.deepcopy(host.snapshot), session_id="session-a", track_id="track-ac",
                 volume_id="volume-ac", received_at=0.0, live_qualified=True)
    assert not host.calls
    clock.value = 10.0
    return host, clock, events, executor


def apply(executor, clock, offset=-0.5, **overrides):
    arguments = {"proposed_at": clock.value, "measurement_received_at": clock.value,
                 "policy_valid": True, "live_qualified": True}
    arguments.update(overrides)
    return executor.apply(offset, **arguments)


def test_successful_half_db_and_exact_native_restore_with_persisted_intent():
    host, clock, events, executor = setup_executor()
    host.dry_hook = lambda _: (events[-1]["event"] == "dry_run_pending") or pytest.fail("no intent")
    host.write_hook = lambda _: (events[-1]["event"] == "write_pending") or pytest.fail("no intent")
    result = apply(executor, clock)
    assert result["confirmed_offset_db"] == -0.5
    assert result["confirmed_db"] == -6.5
    assert host.writes[0]["db"] == -6.5
    assert host.writes[0]["expected_value"] == host.original_raw
    clock.value = 20.0
    result = apply(executor, clock, 0.0)
    assert result["confirmed_raw"] == host.original_raw
    assert host.writes[-1]["db"] is None
    assert host.writes[-1]["raw_value"] == host.original_raw
    assert [e["event"] for e in events].count("confirmed") == 2


@pytest.mark.parametrize("when", ["before_first", "after_dry_run", "after_confirmed"])
def test_manual_override_is_never_adopted_or_restored(when):
    host, clock, _, executor = setup_executor()
    if when == "after_confirmed":
        apply(executor, clock)
        clock.value += 10
    if when == "after_dry_run":
        host.dry_hook = lambda h: h.volume.update(value=0.71)
    else:
        host.volume["value"] = 0.71
    count = len(host.writes)
    with pytest.raises(FaderExecutorError, match="manual_override"):
        apply(executor, clock, -1.0 if when == "after_confirmed" else -0.5)
    assert len(host.writes) == count
    assert host.volume["value"] == 0.71
    assert executor.status["state"] == "stopped"
    with pytest.raises(FaderExecutorError, match="not_armed"):
        apply(executor, clock)
    assert len(host.writes) == count


@pytest.mark.parametrize("change", ["session", "track", "volume", "routing", "automation"])
def test_identity_or_state_changes_latch_stop(change):
    host, clock, _, executor = setup_executor()
    if change == "session":
        host.snapshot["session_id"] = "other-session"
    elif change == "track":
        host.snapshot["tracks"][0]["id"] = "wrong-track"
    elif change == "volume":
        host.volume["id"] = "wrong-volume"
    elif change == "routing":
        host.snapshot["tracks"][0]["routing"]["output_routing_type"] = "Other"
    else:
        host.volume["automation_state"] = 1
    with pytest.raises(FaderExecutorError):
        apply(executor, clock)
    assert executor.status["state"] == "stopped"
    assert not host.writes


@pytest.mark.parametrize("offset,overrides", [
    (-3.5, {}), (0.5, {}), (-1.0, {}),
    (-0.5, {"measurement_received_at": 9.5}),
    (-0.5, {"measurement_received_at": 10.1}),
    (-0.5, {"proposed_at": 9.0}),
    (-0.5, {"policy_valid": False}),
    (-0.5, {"live_qualified": False}),
])
def test_invalid_or_stale_proposal_never_reaches_host(offset, overrides):
    host, clock, _, executor = setup_executor()
    with pytest.raises(FaderExecutorError):
        apply(executor, clock, offset, **overrides)
    assert not host.calls
    assert executor.status["state"] == "stopped"


def test_cadence_uses_confirmed_readback_time_not_proposal_time():
    host, clock, _, executor = setup_executor()
    host.write_hook = lambda _: setattr(clock, "value", 10.4)
    result = apply(executor, clock)
    assert result["confirmed_at"] == 10.4
    clock.value = 20.0
    with pytest.raises(FaderExecutorError, match="cadence_violation"):
        apply(executor, clock, -1.0)
    assert len(host.writes) == 1


def test_measurement_expiring_during_dry_run_blocks_write():
    host, clock, _, executor = setup_executor()
    host.dry_hook = lambda _: setattr(clock, "value", 10.5)
    with pytest.raises(FaderExecutorError, match="stale_measurement"):
        apply(executor, clock)
    assert not host.writes


def test_slow_intent_persistence_cannot_send_a_stale_write():
    host, clock, events, executor = setup_executor()

    def delayed_sink(event):
        events.append(event)
        if event["event"] == "write_pending":
            clock.value = 10.5

    executor._sink = delayed_sink
    with pytest.raises(FaderExecutorError, match="stale_measurement"):
        apply(executor, clock)
    assert not host.writes
    assert events[-1]["event"] == "stopped"


def test_state_changed_during_dry_run_blocks_actual_write():
    host, clock, _, executor = setup_executor()
    host.dry_hook = lambda h: h.snapshot["tracks"][0].update(mute=True)
    with pytest.raises(FaderExecutorError, match="state_changed"):
        apply(executor, clock)
    assert not host.writes


@pytest.mark.parametrize("failure", ["before", "after"])
def test_uncertain_rpc_never_retries_or_advances_confirmed_offset(failure):
    host, clock, events, executor = setup_executor()
    host.failure = failure
    with pytest.raises(FaderExecutorError, match="operation_failed"):
        apply(executor, clock)
    assert len(host.writes) == 1
    assert executor.status["confirmed_offset_db"] == 0
    assert executor.status["confirmed_raw"] == host.original_raw
    assert executor.status["state"] == "stopped"
    assert any(e["event"] == "proposal" and e["proposed_offset_db"] == -0.5 for e in events)
    assert not any(e["event"] == "confirmed" for e in events)


def test_failed_readback_does_not_hide_applied_but_unconfirmed_proposal():
    host, clock, events, executor = setup_executor()
    host.verified = False
    with pytest.raises(FaderExecutorError, match="unverified_write"):
        apply(executor, clock)
    assert len(host.writes) == 1
    assert not math.isclose(host.volume["value"], host.original_raw)
    assert executor.status["confirmed_offset_db"] == 0
    assert events[-1]["event"] == "stopped"


def test_post_write_inspection_must_match_native_receipt():
    host, clock, _, executor = setup_executor()

    def change_after_receipt(h):
        h.inspect_hook = lambda current: current.volume.update(value=0.71)

    host.write_hook = change_after_receipt
    with pytest.raises(FaderExecutorError, match="manual_override"):
        apply(executor, clock)
    assert len(host.writes) == 1
    assert executor.status["confirmed_offset_db"] == 0
    assert host.volume["value"] == 0.71


def test_journal_failure_before_rpc_latches_stop():
    host, clock, _, executor = setup_executor()

    def failed_sink(_event):
        raise OSError("journal unavailable")

    executor._sink = failed_sink
    with pytest.raises(FaderExecutorError, match="journal_failed"):
        apply(executor, clock)
    assert not host.calls
    assert executor.status["state"] == "stopped"


def test_unqualified_arm_has_no_host_effect_and_cannot_be_rearmed():
    host, clock, events = FakeClient(), Clock(), []
    executor = ExperimentalFaderExecutor(host, clock, events.append)
    arguments = dict(session_id="session-a", track_id="track-ac", volume_id="volume-ac",
                     received_at=0, live_qualified=False)
    with pytest.raises(FaderExecutorError, match="live_path_unqualified"):
        executor.arm(host.snapshot, **arguments)
    assert not host.calls
    arguments["live_qualified"] = True
    with pytest.raises(FaderExecutorError, match="arm_not_allowed"):
        executor.arm(host.snapshot, **arguments)
