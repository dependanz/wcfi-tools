"""Exercise the real RPC/client against a fake Live host on the test main thread."""
import ast
import concurrent.futures
import json
import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from wcfi_tools.ableton import client
from wcfi_tools.ableton.install import FILES, RUNTIME_FILES, install
from wcfi_tools.ableton.remote.adapter import BridgeError, LiveAdapter
from wcfi_tools.ableton.remote.server import BridgeServer, Dispatcher, decode_json
from wcfi_tools.cli import app


class Parameter:
    def __init__(self, name, value=0, minimum=-24, maximum=24, quantized=False, fader=False):
        self.name, self.min, self.max = name, minimum, maximum
        self._value = value
        self.is_quantized = quantized
        self.is_enabled = True
        self.automation_state = 0
        self.value_items = ("Off", "On") if quantized else ()
        self.fader = fader
        self.writes = []

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self.writes.append((threading.get_ident(), value))
        self._value = value

    def str_for_value(self, value):
        if self.fader:
            # Deliberately different from linear amplitude AND normalized dB.
            return "-inf dB" if value == 0 else "%.1f dB" % (60 * math.log10(value) + 6)
        return str(value)


class Device:
    def __init__(self, name, chains=()):
        self.name = name
        self.class_name = "PluginDevice"
        self.type = 2
        self.parameters = [Parameter("Device On", 1, 0, 1, True), Parameter("Gain")]
        self.chains = list(chains)
        self.can_have_chains = bool(chains)


class Track:
    def __init__(self, name, devices=()):
        self.name = name
        self.devices = list(devices)
        self.mixer_device = SimpleNamespace(
            volume=Parameter("Track Volume", 10 ** (-6 / 60), 0, 1, fader=True),
            panning=Parameter("Pan", 0, -1, 1), sends=[])
        self.group_track = None
        self.is_foldable = False
        self.mute = self.solo = False


class Song:
    def __init__(self):
        self.tracks = [Track("RED", [Device("Before"), Device("Pro-Q 4"), Device("After")]),
                       Track("KIT", [Device("Before"), Device("After")])]
        self.return_tracks = [Track("A Reverb")]
        self.master_track = Track("Master", [Device("MiniMeters")])
        self.name = "Sunday"
        self.file_path = "Sunday.als"
        self.undo_steps = 0
        self.moves = []
        self.nearest = False

    @property
    def is_playing(self):
        return False

    @is_playing.setter
    def is_playing(self, _value):
        raise AssertionError("Transport must never be changed")

    def begin_undo_step(self):
        self.undo_steps += 1

    def end_undo_step(self):
        self.undo_steps -= 1

    def move_device(self, device, target, index):
        self.moves.append((threading.get_ident(), device, target, index))
        # Observed Live 11 behavior: index refers to the pre-removal list.
        if device in target.devices and target.devices.index(device) < index:
            index -= 1
        for track in self.tracks + self.return_tracks + [self.master_track]:
            for container in self._containers(track):
                if device in container.devices:
                    container.devices.remove(device)
        target.devices.insert(0 if self.nearest else index, device)

    def _containers(self, container):
        yield container
        for device in container.devices:
            for chain in device.chains:
                yield from self._containers(chain)


@pytest.fixture
def host():
    song = Song()
    adapter = LiveAdapter(song)
    return song, adapter


def snapshot(host):
    return host[1].inspect_session()


def set_gain(host, **overrides):
    inspected = snapshot(host)
    control = inspected["tracks"][0]["devices"][1]["parameters"][1]
    arguments = {"session_id": inspected["session_id"], "parameter_id": control["id"],
                 "expected_value": control["value"], "value": -3}
    arguments.update(overrides)
    return host[1].set_parameter(**arguments)


def test_inspection_is_complete_stable_and_read_only(host):
    first = snapshot(host)
    second = snapshot(host)
    assert [t["kind"] for t in first["tracks"]] == ["track", "track", "return", "master"]
    assert first["tracks"] == second["tracks"]
    assert first["transport"] == {"is_playing": False, "record_mode": False}
    assert not first["transport_control"]
    assert first["capabilities"]["move_device"]
    assert not host[0].tracks[0].mixer_device.volume.writes
    with pytest.raises(BridgeError, match="Unsupported"):
        host[1].execute("start_playing", {})


def test_parameter_ranges_quantization_dry_run_and_readback(host):
    parameter = host[0].tracks[0].devices[1].parameters[1]
    assert set_gain(host, dry_run=True)["requested_value"] == -3
    assert parameter.value == 0
    result = set_gain(host, normalized=True, value=.25)
    assert result["verified"] and result["after"]["value"] == -12
    assert host[0].undo_steps == 0
    for value in (-25, 25, float("inf"), float("nan"), True):
        with pytest.raises(BridgeError):
            set_gain(host, value=value)
    assert len(parameter.writes) == 1
    parameter.is_quantized = True
    with pytest.raises(BridgeError, match="integer"):
        set_gain(host, value=.5)


def test_faders_use_live_conversion_for_absolute_and_relative_db(host):
    track = snapshot(host)["tracks"][0]
    fader = host[0].tracks[0].mixer_device.volume
    result = host[1].set_fader(host[1].session_id, track["id"], -6, fader.value)
    assert result["verified"] and fader.str_for_value(fader.value) == "-6.0 dB"
    result = host[1].set_fader(host[1].session_id, track["id"], -3, fader.value, relative=True)
    assert result["requested_db"] == -9
    assert fader.str_for_value(fader.value) == "-9.0 dB"
    before = fader.value
    with pytest.raises(BridgeError):
        host[1].set_fader(host[1].session_id, track["id"], 10, fader.value)
    assert fader.value == before


def test_exact_fader_restoration_does_not_round_through_db(host):
    inspected = snapshot(host)
    track = inspected["tracks"][0]
    fader = host[0].tracks[0].mixer_device.volume
    original = fader.value
    host[1].set_fader(host[1].session_id, track["id"], -6, original)
    result = host[1].set_fader(host[1].session_id, track["id"], None, fader.value,
                              raw_value=original)
    assert result["verified"] and fader.value == original
    with pytest.raises(BridgeError):
        host[1].set_fader(host[1].session_id, track["id"], -6, fader.value, raw_value=original)
    with pytest.raises(BridgeError):
        host[1].set_fader(host[1].session_id, track["id"], None, fader.value, raw_value=2)


@pytest.mark.parametrize("attribute,value,code", [
    ("automation_state", 1, "automation_active"),
    ("automation_state", 2, "automation_active"),
    ("automation_state", None, "automation_active"),
    ("is_enabled", False, "parameter_disabled"),
])
def test_automated_and_disabled_controls_never_change(host, attribute, value, code):
    parameter = host[0].tracks[0].devices[1].parameters[1]
    setattr(parameter, attribute, value)
    with pytest.raises(BridgeError) as exc:
        set_gain(host)
    assert exc.value.code == code
    assert not parameter.writes


def test_stale_and_concurrent_edits_are_rejected(host):
    inspection = snapshot(host)
    parameter_id = inspection["tracks"][0]["devices"][1]["parameters"][1]["id"]
    with pytest.raises(BridgeError) as exc:
        set_gain(host, expected_value=5)
    assert exc.value.code == "conflict"
    with pytest.raises(BridgeError) as exc:
        set_gain(host, session_id="old")
    assert exc.value.code == "stale_session"
    host[0].tracks[0].devices.pop(1)
    with pytest.raises(BridgeError) as exc:
        host[1].set_parameter(inspection["session_id"], parameter_id, -3, 0)
    assert exc.value.code == "stale_target"


def move(host, *, destination=None, index=2, **overrides):
    inspected = snapshot(host)
    source = inspected["tracks"][0]
    target = destination or source
    arguments = {"session_id": inspected["session_id"], "device_id": source["devices"][1]["id"],
        "target_id": target["id"], "index": index,
        "expected_source_order": [d["id"] for d in source["devices"]],
        "expected_target_order": [d["id"] for d in target["devices"]]}
    arguments.update(overrides)
    return host[1].move_device(**arguments)


def test_moves_same_track_cross_track_and_host_adjustment(host):
    identifier = snapshot(host)["tracks"][0]["devices"][1]["id"]
    assert move(host, dry_run=True)["dry_run"] and not host[0].moves
    assert move(host)["verified"]
    assert snapshot(host)["tracks"][0]["devices"][2]["id"] == identifier
    assert host[0].moves[-1][3] == 3  # final index 2, pre-removal insertion index 3
    # Move it back, then into KIT.
    red = host[0].tracks[0]
    red.devices.insert(1, red.devices.pop(2))
    assert move(host, destination=snapshot(host)["tracks"][1], index=1)["verified"]
    assert snapshot(host)["tracks"][1]["devices"][1]["id"] == identifier
    assert not host[0].is_playing and host[0].undo_steps == 0


def test_nearest_position_and_conflicts_are_not_reported_as_exact_success(host):
    host[0].nearest = True
    assert move(host)["verified"] is False
    with pytest.raises(BridgeError) as exc:
        move(host, expected_source_order=[])
    assert exc.value.code == "conflict"


def test_nested_rack_chain_and_self_descendant_move(host):
    chain = Track("Vocal chain", [Device("Compressor")])
    rack = Device("Rack", [chain])
    host[0].tracks[0].devices.insert(1, rack)
    inspection = snapshot(host)
    child = inspection["tracks"][0]["devices"][1]["chains"][0]
    with pytest.raises(BridgeError, match="descendant"):
        move(host, destination=child, index=0)
    assert not host[0].moves


def test_duplicate_names_require_ids(host):
    host[0].tracks[1].name = "RED"
    inspection = snapshot(host)
    with pytest.raises(client.AbletonError, match="matched 2"):
        client.choose_track(inspection, "RED")
    assert client.choose_track(inspection, inspection["tracks"][1]["id"])["index"] == 1


def test_expired_pending_work_never_executes_and_duplicate_id_executes_once():
    calls = []
    dispatcher = Dispatcher(lambda op, args: calls.append((op, args)))
    job = dispatcher.submit("request-1234567890", "write", {}, timeout=.01)
    assert dispatcher.wait(job)["error"]["code"] == "expired"
    dispatcher.drain()
    assert not calls
    job = dispatcher.submit("request-abcdefghij", "write", {"x": 1})
    assert dispatcher.submit("request-abcdefghij", "write", {"x": 1}) is job
    with pytest.raises(BridgeError, match="different operation"):
        dispatcher.submit("request-abcdefghij", "write", {"x": 2})
    dispatcher.drain()
    assert dispatcher.wait(job)["ok"]
    dispatcher.drain()
    assert len(calls) == 1
    dispatcher.close()


def test_started_timeout_reports_uncertainty_without_second_execution():
    dispatcher = Dispatcher(lambda *_args: None)
    job = dispatcher.submit("request-abcdefghij", "write", {}, timeout=.001)
    job.state = "running"
    assert dispatcher.wait(job)["error"]["code"] == "outcome_unknown"
    assert dispatcher.submit("request-abcdefghij", "write", {}) is job
    dispatcher.close()


def test_queue_capacity_and_disconnect():
    dispatcher = Dispatcher(lambda *_args: None)
    jobs = [dispatcher.submit(f"request-{n:020d}", "inspect_session", {}) for n in range(32)]
    with pytest.raises(BridgeError) as exc:
        dispatcher.submit("request-more-123456", "inspect_session", {})
    assert exc.value.code == "busy"
    dispatcher.close()
    assert all(dispatcher.wait(job)["error"]["code"] == "disconnected" for job in jobs)


def pump(server, function):
    # HTTP in a worker, but all fake Live calls execute on THIS thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(function)
        deadline = time.monotonic() + 4
        while not future.done() and time.monotonic() < deadline:
            server.dispatcher.drain()
            time.sleep(.005)
        return future.result(timeout=1)


def test_real_http_client_discovery_and_main_thread_writes(host, tmp_path, monkeypatch):
    server = BridgeServer(host[1], tmp_path)
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://invalid.invalid:9")
        connected = pump(server, lambda: client.connect(folder=tmp_path))
        result = pump(server, lambda: client.set_parameter(connected, "RED", "Pro-Q 4", "Gain", -2))
        assert result["verified"]
        writes = host[0].tracks[0].devices[1].parameters[1].writes
        assert writes == [(threading.get_ident(), -2)]
        result = pump(server, lambda: client.move_device(connected, "RED", "Pro-Q 4", 2))
        assert result["verified"]
        inspection = pump(server, lambda: connected.call("inspect_session"))
        assert not inspection["transport"]["is_playing"]
    finally:
        server.close()
    assert not server.path.exists()


@pytest.mark.parametrize("headers,body,status", [
    ({}, b"{}", 403),
    ({"Origin": "https://evil.invalid"}, b"{}", 403),
    ({"Host": "evil.invalid"}, b"{}", 403),
    ({}, b'{"id":"1234567890123456","operation":"status","arguments":{"x":NaN}}', 400),
    ({}, b'{"id":"1234567890123456","operation":"status","arguments":{},"arguments":{}}', 400),
])
def test_http_rejects_bad_auth_origin_host_and_json(host, tmp_path, headers, body, status):
    server = BridgeServer(host[1], tmp_path)
    try:
        combined = {"Authorization": "Bearer " + server.server.token, **headers}
        if headers == {} and body == b"{}":
            combined.pop("Authorization")
        request = urllib.request.Request(f"http://127.0.0.1:{server.server.server_port}/v1/rpc",
                                         data=body, headers=combined)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(request, timeout=1)
        assert exc.value.code == status
        assert server.dispatcher.pending.empty()
    finally:
        server.close()


def test_json_rejects_nonfinite_and_duplicate_keys():
    for raw in ('{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}'):
        with pytest.raises(ValueError):
            decode_json(raw)


def test_installer_is_standalone_and_preserves_other_scripts(tmp_path):
    other = tmp_path / "Remote Scripts/OtherController"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("untouched")
    result = install(user_library=tmp_path, state_dir=tmp_path / "discovery")
    target = Path(result["installed"])
    bundle = target / "runtimes" / result["bundle_id"]
    for name in FILES:
        installed_file = (bundle if name in RUNTIME_FILES else target) / name
        source = installed_file.read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 7))
        assert "wcfi_tools" not in source
    assert json.loads((target / "runtime-current.json").read_text())["bundle_id"] == result["bundle_id"]
    assert all(not (target / name).exists() for name in RUNTIME_FILES)
    assert json.loads((target / "bridge-home.json").read_text())["state_dir"] == str(tmp_path / "discovery")
    assert install(user_library=tmp_path)["installed"] == str(target)
    assert (other / "__init__.py").read_text() == "untouched"


def test_installer_refuses_unowned_directory(tmp_path):
    existing = tmp_path / "Remote Scripts/WCFI_Bridge"
    existing.mkdir(parents=True)
    (existing / "__init__.py").write_text("user code")
    with pytest.raises(RuntimeError, match="not managed"):
        install(user_library=tmp_path)
    assert (existing / "__init__.py").read_text() == "user code"


def test_cli_commands_and_offline_error(tmp_path, monkeypatch, host):
    monkeypatch.setenv("WCFI_ABLETON_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["audio", "ableton", "status"])
    assert result.exit_code == 1 and "select WCFI_Bridge" in result.stdout
    fake = SimpleNamespace(call=lambda operation, arguments=None: host[1].execute(operation, arguments or {}))
    monkeypatch.setattr(client, "connect", lambda **_kwargs: fake)
    result = runner.invoke(app, ["audio", "ableton", "fader", "Master", "--db=-6", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"]
    result = runner.invoke(app, ["audio", "ableton", "set-parameter", "RED", "Pro-Q 4", "Gain", "--value=-3"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["verified"]
    result = runner.invoke(app, ["audio", "ableton", "move-device", "RED", "Pro-Q 4", "--index", "2"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["audio", "ableton", "inspect", "--output", str(tmp_path / "set.json")])
    assert result.exit_code == 0, result.output
    assert not json.loads((tmp_path / "set.json").read_text())["transport"]["is_playing"]
