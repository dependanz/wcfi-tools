"""Reversible journals against a fake host; no DAW, audio or desktop access."""
import copy
import json

import pytest

from wcfi_tools.ableton.client import AbletonError
from wcfi_tools.ableton.transaction import ReversibleTrial


class FakeClient:
    def __init__(self):
        self.snapshot = {
            "session_id": "session-a",
            "transport": {"is_playing": False, "record_mode": False},
            "tracks": [{"id": "track-red", "name": "RED", "kind": "track",
                        "volume": {"value": .673819245},
                        "devices": [{"id": "device-eq", "name": "EQ", "chains": [],
                                     "parameters": [{"id": "parameter-gain", "name": "Gain",
                                                     "value": -.123456789}]}]}]}
        self.calls = []
        self.write_failure = None
        self.inspect_failures = 0
        self.fail_reconciliation = False
        self.before_write = None
        self.dry_run_hook = None

    def control(self, operation):
        track = self.snapshot["tracks"][0]
        return track["volume"] if operation == "set_fader" else track["devices"][0]["parameters"][0]

    def call(self, operation, arguments=None):
        self.calls.append((operation, copy.deepcopy(arguments)))
        assert operation in {"inspect_session", "set_fader", "set_parameter"}
        if operation == "inspect_session":
            if self.inspect_failures:
                self.inspect_failures -= 1
                raise TimeoutError("inspect unavailable")
            return copy.deepcopy(self.snapshot)
        assert arguments["session_id"] == self.snapshot["session_id"]
        parameter = self.control(operation)
        assert arguments["expected_value"] == parameter["value"]
        if operation == "set_fader":
            target = arguments.get("raw_value")
            if target is None:
                target = ((arguments["db"] + 120) / 126) ** 2
        else:
            target = arguments["value"]
        plan = {"before": copy.deepcopy(parameter), "requested_value": target,
                "dry_run": arguments.get("dry_run", False)}
        if plan["dry_run"]:
            if self.dry_run_hook:
                self.dry_run_hook(self)
                self.dry_run_hook = None
            return plan
        if self.before_write:
            self.before_write(operation, arguments)
        failure, self.write_failure = self.write_failure, None
        if failure != "before":
            parameter["value"] = target
        if self.fail_reconciliation:
            self.inspect_failures += 1
            self.fail_reconciliation = False
        if failure:
            raise TimeoutError("response lost")
        return dict(plan, after=copy.deepcopy(parameter), verified=True)


def trial(tmp_path):
    fake = FakeClient()
    return fake, ReversibleTrial(fake, tmp_path / "journal.json")


def actual_writes(fake):
    return [(op, args) for op, args in fake.calls
            if op != "inspect_session" and not args.get("dry_run")]


def test_fader_and_parameter_exact_reverse_restore(tmp_path):
    fake, transaction = trial(tmp_path)
    original = copy.deepcopy(fake.snapshot)
    transaction.set_fader("RED", -6)
    transaction.set_parameter("RED", "EQ", "Gain", -2)
    transaction.set_fader("RED", -12)
    assert transaction.restore()["restored"]
    assert fake.snapshot == original
    writes = actual_writes(fake)
    assert [op for op, _ in writes] == ["set_fader", "set_parameter", "set_fader",
                                     "set_fader", "set_parameter", "set_fader"]
    assert writes[-1][1]["raw_value"] == .673819245
    assert writes[-1][1]["db"] is None
    assert writes[-2][1]["value"] == -.123456789
    count = len(actual_writes(fake))
    assert transaction.restore()["restored"]
    assert len(actual_writes(fake)) == count
    with pytest.raises(AbletonError, match="restored"):
        transaction.set_fader("RED", 0)


def test_intent_persisted_before_write(tmp_path):
    fake, transaction = trial(tmp_path)

    def verify(_operation, arguments):
        record = json.loads(transaction.journal_path.read_text())["entries"][-1]
        assert record["status"] == "write_pending"
        assert record["write_arguments"] == arguments
        assert "intended_raw" in record

    fake.before_write = verify
    transaction.set_fader("RED", -6)
    transaction.restore()


def test_concurrent_edit_before_first_write_is_rejected(tmp_path):
    fake, transaction = trial(tmp_path)
    fake.control("set_fader")["value"] = .9
    with pytest.raises(AbletonError, match="concurrently"):
        transaction.set_fader("RED", -6)
    assert not actual_writes(fake)
    transaction.restore()
    assert fake.control("set_fader")["value"] == .9


def test_concurrent_edit_after_dry_run_is_rejected(tmp_path):
    fake, transaction = trial(tmp_path)
    fake.dry_run_hook = lambda host: host.control("set_fader").update(value=.95)
    with pytest.raises(AbletonError, match="after dry run"):
        transaction.set_fader("RED", -6)
    assert not actual_writes(fake)
    assert transaction.journal["entries"][-1]["status"] == "dry_run_failed"


def test_restore_keeps_concurrent_edit_and_restores_independent_parameter(tmp_path):
    fake, transaction = trial(tmp_path)
    transaction.set_parameter("RED", "EQ", "Gain", -2)
    transaction.set_fader("RED", -6)
    fake.control("set_fader")["value"] = .1
    with pytest.raises(AbletonError, match="safely restored"):
        transaction.restore()
    assert fake.control("set_fader")["value"] == .1
    assert fake.control("set_parameter")["value"] == -.123456789
    assert not transaction.journal["restored"]


@pytest.mark.parametrize("failure,applied", [("after", True), ("before", False)])
def test_timeout_reconciles_before_restoring(tmp_path, failure, applied):
    fake, transaction = trial(tmp_path)
    original = copy.deepcopy(fake.snapshot)
    fake.write_failure = failure
    with pytest.raises(TimeoutError):
        transaction.set_fader("RED", -6)
    entry = transaction.journal["entries"][0]
    assert entry["status"] == ("applied" if applied else "not_applied")
    transaction.restore()
    assert fake.snapshot == original
    assert len(actual_writes(fake)) == (2 if applied else 1)


def test_unknown_timeout_outcome_resolved_on_restore(tmp_path):
    fake, transaction = trial(tmp_path)
    original = copy.deepcopy(fake.snapshot)
    fake.write_failure = "after"
    fake.fail_reconciliation = True
    with pytest.raises(TimeoutError):
        transaction.set_parameter("RED", "EQ", "Gain", -2)
    assert transaction.journal["entries"][0]["status"] == "outcome_unknown"
    with pytest.raises(AbletonError, match="previous write"):
        transaction.set_fader("RED", -6)
    assert transaction.restore()["restored"]
    assert fake.snapshot == original


def test_restore_timeout_with_applied_readback_is_success(tmp_path):
    fake, transaction = trial(tmp_path)
    original = copy.deepcopy(fake.snapshot)
    transaction.set_fader("RED", -6)
    fake.write_failure = "after"
    assert transaction.restore()["restored"]
    assert fake.snapshot == original
    assert transaction.journal["entries"][-1]["error"] == "response lost"


@pytest.mark.parametrize("transport", [
    {"is_playing": True, "record_mode": False},
    {"is_playing": False, "record_mode": True},
])
def test_constructor_refuses_active_transport(tmp_path, transport):
    fake = FakeClient()
    fake.snapshot["transport"] = transport
    with pytest.raises(AbletonError, match="stopped"):
        ReversibleTrial(fake, tmp_path / "journal.json")
    assert not actual_writes(fake)
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("change", ["transport", "session"])
def test_changed_host_refuses_write_and_restore(tmp_path, change):
    fake, transaction = trial(tmp_path)
    transaction.set_fader("RED", -6)
    if change == "transport":
        fake.snapshot["transport"]["is_playing"] = True
    else:
        fake.snapshot["session_id"] = "session-b"
    with pytest.raises(AbletonError):
        transaction.set_parameter("RED", "EQ", "Gain", -2)
    with pytest.raises(AbletonError):
        transaction.restore()
    assert len(actual_writes(fake)) == 1


def test_existing_journal_is_never_overwritten(tmp_path):
    fake, transaction = trial(tmp_path)
    before = transaction.journal_path.read_bytes()
    with pytest.raises(FileExistsError):
        ReversibleTrial(fake, transaction.journal_path)
    assert transaction.journal_path.read_bytes() == before
