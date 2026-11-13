"""Home-shadow CLI checks with fakes only: no Live, no bridge DLL, no audio, no host state."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from wcfi_tools.ableton import client as ableton_client
from wcfi_tools.audio import analysis_nodes, home_shadow, session_nodes
from wcfi_tools.audio.analysis_nodes import Status
from wcfi_tools.audio.session_nodes import SessionNodeError
from wcfi_tools.cli import app
from wcfi_tools.commands import shadow_home

runner = CliRunner()
DEFAULT_ROLES = ["RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR"]
SNAPSHOT = {"name": "NEWGDN-AutoMixTest-v2", "file_path": "C:/sets/NEWGDN-AutoMixTest-v2.als",
            "transport": {"is_playing": False, "record_mode": False}, "tracks": []}
# The keys the run command reads from run_home_shadow's summary, with a finished window's values.
SUMMARY = {"outcome": "completed", "reason": "collection_seconds_elapsed", "detail": None,
           "stop_reason": "collection_seconds_elapsed", "complete_groups": 12, "proposed_moves": 1,
           "coordinator_bins": 300, "gaps": 2,
           "red_lock": {"ready": True, "ready_bin_index": 49, "gain_db": -6.}}
PUBLISHED_LABELS = ("RED", "YELLOW", "GREEN", "LAPEL", "EGUITAR", "ACGUITAR", "KEYS", "BASS", "KIT",
                    "Master", "RETURN-INSTVERB", "RETURN-VOCALVERB", "RETURN-TESTBUS")
CLOCK_NS = 7_000_000_000
LIVE_PID = 31337
DESCRIPTOR = {"protocol": 1, "port": 40123, "pid": LIVE_PID, "token": "t" * 32, "instance_id": "a" * 32}


class FakeBridge:
    """Twenty-six fresh nodes of one Live process, as the open set publishes them; reads return
    the status only, so the CLI's bind runs through the real session_nodes without PCM."""

    def __init__(self):
        self.nodes = {}
        for index, label in enumerate(PUBLISHED_LABELS):
            for position in (0, 1):
                self.nodes[f"{index:02d}{position}" + "f" * 29] = Status(
                    version=1, pid=31337, channels=2, block_size=256, position=position, active=1,
                    epoch=9, flags=1, sample_rate=48000., written_frame=96000, first_frame=0,
                    callback_ns=CLOCK_NS - 12_000_000, host_sample=0, host_system_ns=0,
                    label=label.encode())
        self.handles, self.reads, self.counter = {}, [], 0

    def discover(self):
        return list(self.nodes)

    def open(self, identity):
        self.counter += 1
        self.handles[self.counter] = identity
        return self.counter

    def read_with_code(self, handle, cursor, count):
        self.reads.append((self.handles[handle], cursor, count))
        return 0, (), self.nodes[self.handles[handle]]

    def close(self, handle):
        self.handles.pop(handle)

    def clock_ns(self):
        return CLOCK_NS


@pytest.fixture
def bridge_class(monkeypatch):
    """NodeBridge replacement; constructing the real one would load the native DLL."""
    fake = Mock(name="NodeBridge")
    monkeypatch.setattr(analysis_nodes, "NodeBridge", fake)
    return fake


@pytest.fixture
def nodes(monkeypatch, bridge_class):
    """The real session_nodes module with its three entry points replaced by recorders."""
    calls = {}

    def inventory(bridge):
        calls["inventory"] = bridge
        return ["snapshot-a", "snapshot-b"]

    def bind_roles(snapshots, roles, *, stage):
        calls["bind_roles"] = (snapshots, list(roles), stage)
        return {role: {"node_id": f"node-{role}", "pid": 4242, "epoch": 3} for role in roles}

    def binding_record(binding, *, stage, roles):
        calls["binding_record"] = (binding, stage, list(roles))
        return {"stage": stage, "roles": list(roles), "binding": binding,
                "source_map_revision": "ab" * 32, "host_pid": 4242, "epoch": 3}

    monkeypatch.setattr(session_nodes, "inventory", inventory)
    monkeypatch.setattr(session_nodes, "bind_roles", bind_roles)
    monkeypatch.setattr(session_nodes, "binding_record", binding_record)
    return SimpleNamespace(calls=calls, bridge_class=bridge_class)


@pytest.fixture
def shadow(monkeypatch, bridge_class):
    """The real home_shadow module with run_home_shadow replaced; ShadowConfig stays real."""
    calls = {}
    summary = dict(SUMMARY)

    def run_home_shadow(config, *, bridge_factory, inspect):
        calls.update(config=config, bridge_factory=bridge_factory, inspect=inspect)
        return summary

    client = Mock(name="AbletonClient")
    client.descriptor = dict(DESCRIPTOR)
    client.call.side_effect = lambda operation, *args, **kwargs: (
        dict(SNAPSHOT) if operation == "inspect_session" else {"operation": operation})
    connect = Mock(name="connect", return_value=client)
    monkeypatch.setattr(ableton_client, "connect", connect)
    monkeypatch.setattr(home_shadow, "run_home_shadow", run_home_shadow)
    return SimpleNamespace(calls=calls, summary=summary, bridge_class=bridge_class, client=client,
                           connect=connect)


def test_commands_are_discoverable_without_loading_audio_modules():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["audio", "--help"])
    assert result.exit_code == 0 and "shadow-home" in result.output
    result = runner.invoke(app, ["audio", "shadow-home", "--help"])
    assert result.exit_code == 0
    assert "bind" in result.output and "run" in result.output
    for name in ("session_nodes", "home_shadow", "NodeBridge", "np", "numpy"):
        assert name not in vars(shadow_home)


def test_command_source_names_no_write_or_transport_operation():
    source = Path(shadow_home.__file__).read_text(encoding="utf-8")
    for name in ("set_fader", "set_parameter", "insert_device", "move_device", "remove_inserted",
                 "reload_bridge", "set_eq8", "start_playing", "stop_playing", "record_mode"):
        assert name not in source, name
    assert "inspect_session" in source


def test_bind_prints_binding_record_for_default_roles(nodes):
    result = runner.invoke(app, ["audio", "shadow-home", "bind"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["stage"] == "post" and record["roles"] == DEFAULT_ROLES
    assert record["host_pid"] == 4242 and set(record["binding"]) == set(DEFAULT_ROLES)
    nodes.bridge_class.assert_called_once_with()
    assert nodes.calls["inventory"] is nodes.bridge_class.return_value
    assert nodes.calls["bind_roles"] == (["snapshot-a", "snapshot-b"], DEFAULT_ROLES, "post")
    assert nodes.calls["binding_record"][1:] == ("post", DEFAULT_ROLES)


def test_bind_passes_stage_and_roles(nodes):
    result = runner.invoke(app, ["audio", "shadow-home", "bind", "--stage", "pre",
                                 "--roles", "RED, AC GUITAR"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["stage"] == "pre" and record["roles"] == ["RED", "AC GUITAR"]
    assert nodes.calls["bind_roles"][1:] == (["RED", "AC GUITAR"], "pre")


def test_bind_resolves_the_open_set_through_the_real_session_nodes(bridge_class):
    bridge = bridge_class.return_value = FakeBridge()
    result = runner.invoke(app, ["audio", "shadow-home", "bind"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["stage"] == "post" and record["roles"] == DEFAULT_ROLES
    assert record["host_pid"] == 31337 and record["epoch"] == 9
    assert list(record["binding"]) == DEFAULT_ROLES
    assert record["binding"]["AC GUITAR"]["label"] == "ACGUITAR"
    assert {entry["position"] for entry in record["binding"].values()} == {1}
    assert len({entry["node_id"] for entry in record["binding"].values()}) == 6
    assert record["source_map_revision"] == session_nodes.source_map_revision(record["binding"])
    # Every published node is metadata-read once with a zero frame count, then closed.
    assert len(bridge.reads) == 26 and {read[1:] for read in bridge.reads} == {(0, 0)}
    assert not bridge.handles
    result = runner.invoke(app, ["audio", "shadow-home", "bind", "--roles", "RED,NOPE"])
    assert result.exit_code == 1
    assert "NOPE" in result.output and not bridge.handles


@pytest.mark.parametrize("options", [["--stage", "mid"], ["--roles", "RED,,KIT"],
                                     ["--roles", "RED,RED"]])
def test_bind_rejects_invalid_options_before_touching_nodes(nodes, options):
    result = runner.invoke(app, ["audio", "shadow-home", "bind", *options])
    assert result.exit_code == 1
    nodes.bridge_class.assert_not_called()
    assert "inventory" not in nodes.calls


def test_bind_reports_binding_failure(monkeypatch, nodes):
    def bind_roles(snapshots, roles, *, stage):
        raise SessionNodeError("Role KIT is ambiguous: two post candidates")

    monkeypatch.setattr(session_nodes, "bind_roles", bind_roles)
    result = runner.invoke(app, ["audio", "shadow-home", "bind"])
    assert result.exit_code == 1
    assert "Role KIT is ambiguous" in result.output
    assert "binding_record" not in nodes.calls


def test_run_builds_config_and_read_only_inspector(shadow, tmp_path):
    output = tmp_path / "home-shadow-01"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 0, result.output
    config = shadow.calls["config"]
    assert isinstance(config, home_shadow.ShadowConfig)
    assert config.output == output and config.seconds == 600
    assert config.wait_for_play is True and config.coordinator_stage == "pre"
    assert config.calibration_seconds == 30 and config.policies == ("v2", "coordinator", "red")
    assert config.v2_stage == "post" and config.red_stage == "pre"
    assert config.max_seconds == 600 + shadow_home.MAX_WAIT_SECONDS == 2400 and config.poll_interval == .05
    assert config.fresh_wait_seconds == 30
    assert shadow.calls["bridge_factory"] is shadow.bridge_class
    shadow.bridge_class.assert_not_called()
    shadow.connect.assert_called_once_with(instance=None)
    # The snapshot carries the descriptor's pid, the Live process the bridge talks to, outside
    # the fingerprinted keys; the operation name is passed through for the shadow's counting.
    inspect = shadow.calls["inspect"]
    assert inspect() == {**SNAPSHOT, "host_pid": LIVE_PID}
    assert inspect("inspect_session") == {**SNAPSHOT, "host_pid": LIVE_PID}
    assert inspect("status") == {"operation": "status"}
    assert shadow.client.method_calls == [("call", ("inspect_session",), {})] * 2 + [
        ("call", ("status",), {})]
    assert not output.exists()  # run_home_shadow creates it; the CLI only refuses an existing one.
    assert "outcome: completed" in result.output
    assert "reason: collection_seconds_elapsed" in result.output
    assert "v2 groups: 12  proposals: 1" in result.output
    assert "coordinator bins: 300  gaps: 2" in result.output
    assert "red lock: locked at bin 49, gain -6.0 dB" in result.output
    assert str(output.resolve()) in result.output


def test_run_maps_options_including_no_wait_for_play(shadow, tmp_path):
    output = tmp_path / "home-shadow-02"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output),
                                 "--no-wait-for-play", "--seconds", "90", "--coordinator-stage",
                                 "post", "--calibration-seconds", "10", "--policies", "v2, red",
                                 "--instance", "abc123"])
    assert result.exit_code == 0, result.output
    config = shadow.calls["config"]
    assert config.wait_for_play is False and config.seconds == 90
    assert config.coordinator_stage == "post" and config.calibration_seconds == 10
    assert config.policies == ("v2", "red")
    shadow.connect.assert_called_once_with(instance="abc123")


def test_run_refuses_existing_output(shadow, tmp_path):
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(tmp_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert not shadow.calls
    shadow.connect.assert_not_called()


@pytest.mark.parametrize("options", [
    ["--policies", "v2,howler"],
    ["--policies", "v2,v2"],
    ["--coordinator-stage", "mid"],
    ["--seconds", "0"],
    ["--seconds", "2400.5"],
    ["--calibration-seconds", "-1"],
    ["--seconds", "20", "--calibration-seconds", "30"],
    ["--wait-minutes", "0"],
    ["--wait-minutes", "721"],
])
def test_run_rejects_invalid_options_before_connecting(shadow, tmp_path, options):
    output = tmp_path / "home-shadow-03"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output), *options])
    assert result.exit_code == 1, result.output
    assert not shadow.calls
    shadow.connect.assert_not_called()
    assert not output.exists()


def test_run_allows_long_calibration_when_coordinator_is_not_selected(shadow, tmp_path):
    output = tmp_path / "home-shadow-04"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output),
                                 "--seconds", "20", "--calibration-seconds", "30",
                                 "--policies", "v2,red"])
    assert result.exit_code == 0, result.output
    assert shadow.calls["config"].policies == ("v2", "red")


def test_run_gives_the_longest_window_a_wait_allowance_on_top(shadow, tmp_path):
    output = tmp_path / "home-shadow-12"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output),
                                 "--seconds", "2400"])
    assert result.exit_code == 0, result.output
    config = shadow.calls["config"]
    assert config.seconds == 2400 and config.max_seconds == 2400 + shadow_home.MAX_WAIT_SECONDS
    assert shadow_home.MAX_WAIT_SECONDS == 1800
    assert config.max_seconds >= config.seconds  # run_home_shadow refuses the reverse.


def test_run_wait_minutes_extends_the_bound_for_an_unattended_arm(shadow, tmp_path):
    output = tmp_path / "home-shadow-13"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output),
                                 "--seconds", "600", "--wait-minutes", "480"])
    assert result.exit_code == 0, result.output
    config = shadow.calls["config"]
    assert config.seconds == 600 and config.max_seconds == 600 + 480 * 60
    assert shadow_home.MAX_WAIT_MINUTES == 720


def test_run_exits_two_when_host_state_changed(shadow, tmp_path):
    shadow.summary.update(outcome="stopped", reason="host_state_changed",
                          stop_reason="host_state_changed", detail="9f" * 32)
    output = tmp_path / "home-shadow-05"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 2
    assert "outcome: stopped" in result.output
    assert f"reason: host_state_changed: {'9f' * 32}" in result.output
    assert str(output.resolve()) in result.output


@pytest.mark.parametrize("reason", ["inspection_failed", "memory_limit_exceeded",
                                    "node_identity_changed", "max_seconds_reached"])
def test_run_exits_two_on_every_early_stop(shadow, tmp_path, reason):
    shadow.summary.update(outcome="stopped", reason=reason, stop_reason=reason, detail=None,
                          red_lock={"ready": False, "ready_bin_index": None, "gain_db": None})
    output = tmp_path / "home-shadow-06"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 2
    assert f"reason: {reason}\n" in result.output and "red lock: not locked" in result.output


def test_run_treats_a_transport_stop_as_a_finished_window(shadow, tmp_path):
    shadow.summary.update(outcome="completed", reason="transport_stopped",
                          stop_reason="transport_stopped", red_lock=None)
    output = tmp_path / "home-shadow-07"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert "reason: transport_stopped" in result.output and "red lock: not run" in result.output


def test_run_exits_one_when_the_shadow_reports_a_failed_setup(shadow, tmp_path):
    shadow.summary.update(outcome="failed", reason="bind_failed", stop_reason="bind_failed",
                          detail="No post node is published for role 'BASS'", complete_groups=0,
                          proposed_moves=0, coordinator_bins=0, gaps=0, red_lock=None)
    output = tmp_path / "home-shadow-08"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 1
    assert "outcome: failed" in result.output
    assert "reason: bind_failed: No post node is published for role 'BASS'" in result.output
    assert "v2 groups: 0  proposals: 0" in result.output


@pytest.mark.parametrize("reason, detail", [("inspection_failed", "AbletonError: session_too_large"),
                                            ("nodes_not_fresh", "KIT stale"),
                                            ("max_seconds_reached", None)])
def test_run_exits_one_when_a_stop_came_before_anything_was_collected(shadow, tmp_path, reason, detail):
    shadow.summary.update(outcome="failed", reason=reason, stop_reason=reason, detail=detail,
                          complete_groups=0, proposed_moves=0, coordinator_bins=0, gaps=0,
                          red_lock=None)
    output = tmp_path / "home-shadow-13"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 1
    assert "outcome: failed" in result.output
    expected = f"reason: {reason}" if detail is None else f"reason: {reason}: {detail}"
    assert f"{expected}\n" in result.output


def test_run_prints_not_reported_for_missing_counts(shadow, tmp_path):
    shadow.summary.clear()
    shadow.summary.update(outcome="completed")
    output = tmp_path / "home-shadow-09"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert "reason: not reported" in result.output
    assert "v2 groups: not reported  proposals: not reported" in result.output
    assert "red lock: not reported" in result.output


def test_run_reports_missing_bridge_as_setup_failure(shadow, tmp_path):
    shadow.connect.side_effect = ableton_client.AbletonError("Ableton bridge is not connected.")
    output = tmp_path / "home-shadow-10"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 1
    assert "Ableton bridge is not connected" in result.output
    assert not shadow.calls


def test_run_reports_shadow_failure_as_setup_failure(monkeypatch, shadow, tmp_path):
    def run_home_shadow(config, *, bridge_factory, inspect):
        raise RuntimeError("Role BASS is not active in the current Live process")

    monkeypatch.setattr(home_shadow, "run_home_shadow", run_home_shadow)
    output = tmp_path / "home-shadow-11"
    result = runner.invoke(app, ["audio", "shadow-home", "run", "--output", str(output)])
    assert result.exit_code == 1
    assert "Role BASS is not active" in result.output
