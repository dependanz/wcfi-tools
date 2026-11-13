"""Native EQ Eight mode controls are narrow, inspectable and never retried."""
import ast
import copy
from enum import IntEnum
from pathlib import Path

import pytest
from test_ableton_bridge import Device, Song
from typer.testing import CliRunner

from wcfi_tools.ableton import client
from wcfi_tools.ableton.remote.adapter import BridgeError, LiveAdapter
from wcfi_tools.commands.ableton import app


class Eq8(Device):
    def __init__(self):
        super().__init__("EQ Eight")
        self.class_name = "Eq8"
        self._mode = 0
        self.edit_mode = False
        self.oversample = False
        self.writes = []
        self.failure = None
        self.getter_failed = False

    @property
    def global_mode(self):
        if self.getter_failed:
            raise RuntimeError("native getter unavailable")
        return self._mode

    @global_mode.setter
    def global_mode(self, value):
        self.writes.append(value)
        if self.failure == "before":
            raise RuntimeError("setter rejected")
        self._mode = value
        if self.failure == "after":
            raise RuntimeError("setter failed after applying")
        if self.failure == "readback":
            self.getter_failed = True
        if self.failure == "wrong":
            self._mode = 1


class StoppedSong(Song):
    def __init__(self):
        super().__init__()
        self.playing = False
        self.record_mode = False

    @property
    def is_playing(self):
        return self.playing


@pytest.fixture
def eqhost():
    song = StoppedSong()
    device = Eq8()
    song.tracks[0].devices = [device]
    adapter = LiveAdapter(song)
    snapshot = adapter.inspect_session()
    return song, device, adapter, {
        "session_id": snapshot["session_id"],
        "device_id": snapshot["tracks"][0]["devices"][0]["id"],
    }


def change(eqhost, **arguments):
    _, _, adapter, target = eqhost
    return adapter.execute("set_eq8_mode", dict(target, expected_mode=0, mode=2, **arguments))


def test_snapshot_and_native_inspection(eqhost):
    song, device, adapter, target = eqhost
    result = adapter.execute("inspect_eq8_state", target)
    assert result["supported"] and result["global_mode"] == 0
    assert result["edit_mode"] is False and result["oversample"] is False
    assert result["class_name"] == "Eq8" and not device.writes
    snapshot = adapter.inspect_session()
    native = snapshot["tracks"][0]["devices"][0]["native_state"]
    assert native == {k: v for k, v in result.items() if k not in ("device_id", "class_name")}
    assert "native_state" not in snapshot["tracks"][1]["devices"][0]
    assert adapter.status()["bridge_revision"] == 8
    assert song.undo_steps == 0


def test_change_noop_dryrun_and_exact_restore(eqhost):
    song, device, adapter, target = eqhost
    before = adapter.inspect_session()["tracks"]
    plan = change(eqhost, dry_run=True)
    assert plan["dry_run"] and plan["verified"] and not plan["changed"]
    assert not device.writes and adapter.status()["eq8_last_mutation"] is None
    result = change(eqhost)
    assert result["verified"] and result["changed"] and result["state"] == "complete"
    assert result["after"]["global_mode"] == 2 and device.writes == [2]
    noop = adapter.set_eq8_mode(**target, expected_mode=2, mode=2)
    assert noop["verified"] and not noop["changed"] and device.writes == [2]
    adapter.set_eq8_mode(**target, expected_mode=2, mode=0)
    assert adapter.inspect_session()["tracks"] == before
    assert "eq8_last_mutation" not in adapter.inspect_session()
    assert song.undo_steps == 0


@pytest.mark.parametrize("value", [True, False, -1, 3, 2.0, "2", None])
@pytest.mark.parametrize("field", ["expected_mode", "mode"])
def test_argument_modes_are_strict(eqhost, field, value):
    _, device, adapter, target = eqhost
    args = dict(target, expected_mode=0, mode=2)
    args[field] = value
    with pytest.raises(BridgeError) as caught:
        adapter.execute("set_eq8_mode", args)
    assert caught.value.code == "invalid_argument" and not device.writes


@pytest.mark.parametrize("value", [True, False, 2.0, "2", None, 3])
def test_ambiguous_or_unavailable_native_mode_is_readable_but_not_writable(eqhost, value):
    _, device, adapter, target = eqhost
    device._mode = value
    result = adapter.execute("inspect_eq8_state", target)
    assert not result["supported"] and result["global_mode"] is None
    assert "global_mode" in result["errors"]
    with pytest.raises(BridgeError) as caught:
        change(eqhost)
    assert caught.value.code == "unsupported" and not device.writes


def test_int_enum_is_supported(eqhost):
    class Mode(IntEnum):
        STEREO = 0
        MS = 2
    _, device, adapter, target = eqhost
    device._mode = Mode.STEREO
    result = adapter.set_eq8_mode(**target, expected_mode=Mode.STEREO, mode=Mode.MS)
    assert result["verified"] and type(result["after"]["global_mode"]) is int


@pytest.mark.parametrize("property_name", ["edit_mode", "oversample"])
def test_missing_auxiliary_getter_fails_closed(eqhost, property_name):
    _, device, adapter, target = eqhost
    delattr(device, property_name)
    assert not adapter.inspect_eq8_state(**target)["supported"]
    with pytest.raises(BridgeError, match="cannot be verified"):
        change(eqhost)
    assert not device.writes


@pytest.mark.parametrize("field,value", [("playing", True), ("record_mode", True),
                                         ("record_mode", None), ("playing", 0)])
def test_transport_is_never_touched_and_unknown_is_rejected(eqhost, field, value):
    song, device, _, _ = eqhost
    setattr(song, field, value)
    with pytest.raises(BridgeError) as caught:
        change(eqhost)
    assert caught.value.code == "transport_active"
    assert getattr(song, field) is value and not device.writes


def test_conflict_wrong_class_and_stale_ids(eqhost):
    _, device, adapter, target = eqhost
    device._mode = 1
    with pytest.raises(BridgeError) as caught:
        change(eqhost)
    assert caught.value.code == "conflict"
    device.class_name = "PluginDevice"
    with pytest.raises(BridgeError) as caught:
        adapter.inspect_eq8_state(**target)
    assert caught.value.code == "invalid_target"
    with pytest.raises(BridgeError) as caught:
        adapter.inspect_eq8_state(session_id="old", device_id=target["device_id"])
    assert caught.value.code == "stale_session"
    assert not device.writes


def test_unresolved_insertion_blocks_mode_write_but_not_inspection(eqhost):
    _, device, adapter, target = eqhost
    adapter.device_browser.uncertain = True
    assert adapter.inspect_eq8_state(**target)["supported"]
    with pytest.raises(BridgeError) as caught:
        change(eqhost)
    assert caught.value.code == "insertion_unresolved" and not device.writes
    with pytest.raises(BridgeError):
        adapter.set_eq8_mode(**target, expected_mode=0, mode=2)


@pytest.mark.parametrize("failure,changed,verified,state", [
    ("before", False, False, "failed"), ("after", True, True, "failed"),
    ("readback", None, False, "uncertain"), ("wrong", True, False, "failed"),
])
def test_mutation_failure_keeps_actual_or_unknown_outcome_without_retry(eqhost, failure, changed, verified, state):
    song, device, adapter, _ = eqhost
    device.failure = failure
    with pytest.raises(BridgeError) as caught:
        change(eqhost)
    assert caught.value.code == "eq8_mode_unverified"
    result = caught.value.details
    assert result["changed"] is changed and result["verified"] is verified
    assert result["state"] == state
    assert adapter.status()["eq8_last_mutation"] == result
    assert device.writes == [2] and song.undo_steps == 0


def test_unknown_property_write_not_supported(eqhost):
    _, device, adapter, target = eqhost
    with pytest.raises(TypeError):
        adapter.execute("set_eq8_mode", dict(target, expected_mode=0, mode=2, property="oversample"))
    assert not device.writes


class FakeClient:
    def __init__(self, eqhost):
        self.adapter = eqhost[2]
        self.calls = []

    def call(self, operation, arguments=None, **kwargs):
        self.calls.append((operation, copy.deepcopy(arguments)))
        try:
            return self.adapter.execute(operation, arguments or {})
        except BridgeError as exc:
            raise client.AbletonError(str(exc), code=exc.code, details=exc.details) from exc


def test_client_reads_current_mode_then_sends_one_cas(eqhost):
    connected = FakeClient(eqhost)
    result = client.set_eq8_mode(connected, "RED", "EQ Eight", 2)
    assert result["verified"]
    assert [op for op, _ in connected.calls] == ["inspect_session", "inspect_eq8_state", "set_eq8_mode"]
    assert connected.calls[-1][1]["expected_mode"] == 0
    assert client.inspect_eq8_state(connected, "RED", "EQ Eight")["global_mode"] == 2


def test_client_does_not_retry_uncertain_write(eqhost):
    eqhost[1].failure = "readback"
    connected = FakeClient(eqhost)
    with pytest.raises(client.AbletonError):
        client.set_eq8_mode(connected, "RED", "EQ Eight", 2)
    assert sum(op == "set_eq8_mode" for op, _ in connected.calls) == 1


def test_cli_dry_run_and_read_only_inspection(eqhost, monkeypatch):
    connected = FakeClient(eqhost)
    monkeypatch.setattr(client, "connect", lambda **kwargs: connected)
    runner = CliRunner()
    assert runner.invoke(app, ["eq8-state", "RED", "EQ Eight"]).exit_code == 0
    result = runner.invoke(app, ["set-eq8-mode", "RED", "EQ Eight", "--mode", "2", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert not eqhost[1].writes


def test_remote_adapter_stays_python37_compatible():
    source = Path("src/wcfi_tools/ableton/remote/adapter.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 7))
