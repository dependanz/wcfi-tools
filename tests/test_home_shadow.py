"""Home shadow with fakes only: no Live, no bridge DLL, no host writes, no PCM on disk."""
from __future__ import annotations

import copy
import json
import math
import sys
import time
from collections import Counter
from typing import ClassVar

import numpy as np
import pytest

from wcfi_tools.audio import home_shadow
from wcfi_tools.audio.analysis_nodes import CAPACITY, Status
from wcfi_tools.audio.experimental_audio_gain import ROLES
from wcfi_tools.audio.home_shadow import (
    HostCalls,
    InspectionMonitor,
    ResourceGuard,
    ShadowConfig,
    ShadowStop,
    run_home_shadow,
)
from wcfi_tools.audio.mix_coordinator import TRACKS

RATE, BLOCK, PID, EPOCH = 48000, 480, 4242, 4
START = 100.0
PRE_LEVELS = {"RED": .1, "YELLOW": .02, "GREEN": .03, "EGUITAR": .04, "AC GUITAR": .05,
              "KEYS": .06, "KIT": .07}
POST_LEVELS = {"RED": .08, "AC GUITAR": .03, "KIT": .05, "BASS": .04, "KEYS": .035, "EGUITAR": .045}
PUBLISHED_LABELS = {"AC GUITAR": "ACGUITAR"}  # The node label has no space; the role does.
ROW_KEYS = {
    "source", "node_id", "pid", "epoch", "stage", "sample_rate", "channels",
    "window_start_local_frame", "window_end_local_frame", "complete", "measurement_valid",
    "observed_frames", "missing_frames", "nonfinite_samples", "read_failures", "stale_reads",
    "rms_dbfs", "peak_dbfs", "zero_energy_channels", "capture_age_min_ms", "capture_age_max_ms",
    "receipt_first_monotonic", "receipt_last_monotonic", "callback_first_ns", "callback_last_ns",
    "clock_flags_observed", "last_status", "alignment", "routing_and_state",
    "controller_qualified", "source_window_index", "receipt_first_qpc_seconds",
    "receipt_last_qpc_seconds", "receipt_clock"}
STATUS_KEYS = {"active", "flags", "host_sample", "host_system_ns", "callback_ns", "written_frame",
               "capture_age_ms", "block_size", "first_frame", "epoch", "sample_rate"}
FRAME_KEYS = {"time_seconds", "sequence", "received_at_qpc", "state_observed_at_qpc",
              "state_query_started_at_qpc", "inspection_received_at_qpc",
              "state_time_interpretation", "source_map_revision", "routing_revision",
              "state_revision", "host_sample_alignment", "windows"}


class FakeClock:
    def __init__(self, start=START):
        self.now = start
        self.sleeps = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now = round(self.now + seconds, 9)  # Keep 50 ms steps on exact instants.
        self.sleeps += 1


class FakeGuard:
    """Stands in for ResourceGuard so tests never change pytest's priority or affinity."""
    instances: ClassVar[list] = []

    def __init__(self, limit, *, restrict=True):
        self.limit, self.restrict, self.checks = limit, restrict, 0
        FakeGuard.instances.append(self)

    def check(self):
        self.checks += 1

    def describe(self):
        return {"peak_memory_bytes": 0, "memory_limit_bytes": self.limit, "priority": "fake",
                "cpu_mask": None, "notes": []}


def parameter(name, value, index, automation_state=0):
    return {"id": f"{name}-{index}", "name": name, "value": value, "min": 0., "max": 1.,
            "display": f"{value:.2f}", "is_enabled": True, "is_quantized": False,
            "automation_state": automation_state, "value_items": [], "value_items_count": 0}


def track(name, index):
    return {"id": f"track-{index}", "name": name, "kind": "track", "index": index,
            "is_group": False, "group_id": None, "mute": False, "solo": False,
            "routing": {"input_routing_type": "Ext. In", "input_routing_channel": str(index + 1),
                        "output_routing_type": "Master", "output_routing_channel": None},
            "devices": [{"id": f"device-{index}", "name": "WCFI Analysis Node", "index": 0,
                         "class_name": "PluginDevice", "type": "audio_effect",
                         "parameters": [parameter("Device On", 1., index)], "chains": []}],
            "volume": parameter("Track Volume", .85, index), "pan": parameter("Track Pan", .5, index),
            "sends": []}


class FakeHost:
    """The whole host surface is inspect(); its transport follows the simulated clock.

    Like the CLI's inspector it is called with the operation name and adds the inspected
    Live's pid to the snapshot as ``host_pid``.
    """

    def __init__(self, clock, playing=(0., math.inf), *, host_pid=PID):
        self.clock, self.playing, self.host_pid = clock, playing, host_pid
        self.calls = Counter()
        self.mutations = []
        self.errors = []  # Raised by the next inspect() calls, in order.
        self.snapshot = {"session_id": "f" * 32, "live_version": "11.3.35", "bridge_revision": 8,
                         "name": "NEWGDN-AutoMixTest-v2", "file_path": "C:/sets/NEWGDN.als",
                         "tracks": [track(name, index) for index, name in enumerate(TRACKS)]}

    def is_playing(self):
        start, stop = self.playing
        return start <= self.clock.now < stop

    def inspect(self, operation="inspect_session"):
        self.calls[operation] += 1
        assert operation == "inspect_session", operation
        if self.errors:
            raise self.errors.pop(0)
        for at, change in self.mutations:
            if self.clock.now >= at:
                change(self.snapshot)
        return {**copy.deepcopy(self.snapshot), "inspected_at": self.clock.now, "host_pid": self.host_pid,
                "transport": {"is_playing": self.is_playing(), "record_mode": False}}


class FakeNode:
    def __init__(self, identity, role, position, amplitude, *, origin=0, channels=2):
        self.identity, self.role, self.position = identity, role, position
        self.label = PUBLISHED_LABELS.get(role, role)
        self.amplitude, self.origin, self.channels = amplitude, origin, channels
        self.drops = []     # (trigger_frame, lost): the ring's oldest frame jumps past them
        self.failures = []  # (start_seconds, stop_seconds, native_code)
        self.frozen_from = None  # Seconds after which the node stops publishing (counters freeze).


class FakeBridge:
    """Read-only NodeBridge fake over synthetic PCM shared by one simulated Live process.

    As the plugin does, a node with a host clock flags it and advances ``host_sample`` with
    its frame counter whether or not the transport plays.
    """

    def __init__(self, clock, nodes, host, *, host_clock=True, order=None):
        self.clock, self.host, self.host_clock = clock, host, host_clock
        self.nodes = {node.identity: node for node in nodes}
        self.order = list(order or self.nodes)
        self.origin = clock.now
        self.handles, self.next_handle = {}, 1
        self.opened = self.closed = self.reads = 0

    def discover(self):
        return list(self.order)

    def open(self, identity):
        if identity not in self.nodes:
            return 0
        handle, self.next_handle = self.next_handle, self.next_handle + 1
        self.handles[handle] = identity
        self.opened += 1
        return handle

    def close(self, handle):
        self.handles.pop(handle)
        self.closed += 1

    def clock_ns(self):
        return round(self.clock.now * 1e9)

    def produced(self, at=None):
        now = self.clock.now if at is None else at
        return math.floor((now - self.origin) * RATE + 1e-6) // BLOCK * BLOCK

    def read(self, handle, cursor):
        code, block, status = self.read_with_code(handle, cursor, CAPACITY)
        return (None, None) if code < 0 else (block, status)

    def read_with_code(self, handle, cursor, count):
        self.reads += 1
        node = self.nodes[self.handles[handle]]
        now = self.clock.now
        for start, stop, code in node.failures:
            if start <= now < stop:
                return code, None, None
        produced = self.produced(now if node.frozen_from is None else min(now, node.frozen_from))
        written = node.origin + produced
        first = max(cursor, written - CAPACITY, node.origin)
        for trigger, lost in node.drops:
            if written >= trigger + lost:
                first = max(first, trigger + lost)
        first = min(first, written)
        frames = min(count, written - first)
        block = np.full((frames, 2), node.amplitude, np.float32)
        status = Status(version=1, pid=PID, channels=node.channels, block_size=BLOCK,
                        position=node.position, active=1, epoch=EPOCH,
                        flags=1 if self.host_clock else 0,
                        sample_rate=float(RATE), written_frame=written, first_frame=first,
                        callback_ns=round((self.origin + produced / RATE) * 1e9),
                        host_sample=produced - BLOCK + 10_000 if self.host_clock else 0,
                        host_system_ns=0, label=node.label.encode())
        return frames, block, status


def band(pre_levels=PRE_LEVELS, post_levels=POST_LEVELS):
    nodes = [FakeNode(f"pre{index:02d}" + "a" * 27, role, 0, amplitude, origin=index * 960)
             for index, (role, amplitude) in enumerate(pre_levels.items())]
    nodes += [FakeNode(f"post{index:02d}" + "b" * 26, role, 1, amplitude, origin=index * 480 + 7)
              for index, (role, amplitude) in enumerate(post_levels.items())]
    nodes += [FakeNode("master0" + "c" * 25, "Master", 0, .2), FakeNode("master1" + "c" * 25, "Master", 1, .2)]
    return nodes


def node(nodes, role, position):
    return next(n for n in nodes if n.role == role and n.position == position)


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def shadow(tmp_path, *, seconds, playing=(0., math.inf), nodes=None, host_clock=True,
           host=None, order=None, name="run", **overrides):
    clock = FakeClock()
    host = host or FakeHost(clock, playing)
    host.clock = clock
    bridge = FakeBridge(clock, nodes or band(), host, host_clock=host_clock, order=order)
    factories = []
    config = ShadowConfig(output=tmp_path / name, seconds=seconds, **overrides)

    def factory():
        factories.append(bridge)
        return bridge
    summary = run_home_shadow(config, bridge_factory=factory, inspect=host.inspect,
                              clock=clock, sleep=clock.sleep)
    assert factories == [bridge]
    assert bridge.opened == bridge.closed and not bridge.handles
    assert json.loads((config.output / "summary.json").read_text(encoding="utf-8")) == summary
    return summary, host, bridge, clock, config.output


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(home_shadow, "INSPECTION_WORKER", False)
    monkeypatch.setattr(home_shadow, "ResourceGuard", FakeGuard)
    FakeGuard.instances.clear()


def test_window_rows_carry_the_exact_key_set_for_both_stages(tmp_path):
    nodes = band()
    summary, _, _, _, output = shadow(tmp_path, seconds=3.2, nodes=nodes)
    assert summary["outcome"] == "completed" and summary["reason"] == "collection_seconds_elapsed"
    levels = rows(output / "levels.jsonl")
    assert levels and summary["scalar_rows"] == len(levels) == 13 * 3
    by_id = {n.identity: n for n in nodes}
    for row in levels:
        assert set(row) == ROW_KEYS and set(row["last_status"]) == STATUS_KEYS
        source = by_id[row["node_id"]]
        assert row["source"] == source.role
        assert row["stage"] == ("after_effects_before_fader" if source.position else "before_effects")
        assert (row["pid"], row["epoch"], row["sample_rate"], row["channels"]) == (PID, EPOCH, RATE, 2)
        assert row["window_end_local_frame"] - row["window_start_local_frame"] == RATE
        assert row["complete"] and row["measurement_valid"] and row["observed_frames"] == RATE
        assert row["rms_dbfs"] == pytest.approx([20 * math.log10(source.amplitude)] * 2)
        assert row["zero_energy_channels"] == [False, False]
        assert 0 <= row["capture_age_min_ms"] <= row["capture_age_max_ms"] < 250
        assert row["receipt_clock"] == "native_WcfiNodeClockNs_QPC_seconds"
        assert row["last_status"]["callback_ns"] == row["callback_last_ns"]
    assert {row["source_window_index"] for row in levels} == {1, 2, 3}
    assert summary["writes"] == 0 and summary["declared"]["live_eligible"] is False
    assert set(summary["sha256"]) == set(home_shadow.HASHED_MODULES)
    assert {"experimental_audio_gain.py", "mix_coordinator_trial.py"} <= set(summary["sha256"])
    assert all(summary["sha256"].values())
    assert FakeGuard.instances and FakeGuard.instances[0].checks > 0


def test_real_v2_policy_qualifies_after_warmup_when_ages_are_sane(tmp_path):
    summary, _, _, _, output = shadow(tmp_path, seconds=12.2)
    statuses = rows(output / "input-status.jsonl")
    assert [s["observation_group_index"] for s in statuses] == list(range(1, 13))
    for status in statuses[:8]:
        assert status["input_status"]["reason"] == "publication_timing_warmup"
        assert status["input_status"]["policy_started"] is False
    for status in statuses[8:]:
        assert status["input_status"]["reason"] is None
        assert status["input_status"]["publication_windows_qualified"] is True
        assert status["input_status"]["policy_started"] is True
        assert status["input_status"]["host_sample_alignment"] is False
    assert statuses[8]["input_status"]["policy_endpoint"] == 1
    groups = rows(output / "window-groups.jsonl")
    assert len(groups) == 12 and all(set(frame) == FRAME_KEYS for frame in groups)
    for frame in groups:
        assert set(frame["windows"]) == set(ROLES)
        assert frame["host_sample_alignment"] is False
        assert frame["routing_revision"] == frame["state_revision"] == summary["state_fingerprint"]
        assert frame["source_map_revision"] == summary["bindings"]["v2"]["source_map_revision"]
        assert frame["state_query_started_at_qpc"] <= frame["state_observed_at_qpc"] <= frame["received_at_qpc"]
        assert frame["received_at_qpc"] - frame["state_query_started_at_qpc"] <= 2.5
    decisions = rows(output / "v2-decisions.jsonl")
    assert decisions and summary["controller_evaluations"] == len(decisions)
    assert all(row["live_eligible"] is False for row in decisions)
    proposals = rows(output / "v2-proposals.jsonl")
    assert summary["proposed_moves"] == len(proposals)
    assert all(row["shadow_only"] is True and row["applied"] is False for row in proposals)
    assert summary["input_reason_counts"] == {"publication_timing_warmup": 8, "qualified": 4}
    assert summary["complete_groups"] == 12 and summary["incomplete_group_indices"] == []


@pytest.mark.parametrize("host_clock, alignment", [(True, "host_sample"),
                                                   (False, "approximate_publication_clock")])
def test_coordinator_bins_follow_tracks_order_and_dropped_frames_notify_gap(tmp_path, host_clock, alignment):
    nodes = band()
    keys = node(nodes, "KEYS", 0)
    keys.drops.append((keys.origin + round(8.5 * RATE), 2400))
    scrambled = [n.identity for n in reversed(nodes)]  # Order comes from the binding, not discovery.
    summary, _, _, _, output = shadow(tmp_path, seconds=12., nodes=nodes, order=scrambled,
                                      host_clock=host_clock, calibration_seconds=6.)
    grid = summary["collection"]["grid"]
    assert grid["alignment"] == alignment and grid["lockstep_verified"] is host_clock
    assert (grid["anchor"] is not None) is host_clock
    assert summary["coordinator"]["grid"] == grid
    assert summary["host_sample_alignment"] is False  # The run's V2 qualification, never the grid.
    bins = rows(output / "coordinator-bins.jsonl")
    assert bins and [row["bin_index"] for row in bins] == sorted(row["bin_index"] for row in bins)
    expected = {role: 20 * math.log10(PRE_LEVELS[role]) for role in TRACKS}
    for row in bins:
        assert list(row["levels_dbfs"]) == list(TRACKS) and row["alignment"] == alignment
        assert row["levels_dbfs"] == pytest.approx(expected, abs=1e-6)
    calibration = json.loads((output / "coordinator-calibration.json").read_text(encoding="utf-8"))
    assert calibration["calibrated"] and calibration["error"] is None
    assert set(calibration["plan"]["baseline_gains_db"]) == set(TRACKS)
    assert calibration["settings"]["enabled_support_tracks"]
    assert calibration["grid"] == grid and calibration["host_writes"] == 0
    assert calibration["declared"] == {"live_eligible": False}
    phases = Counter(row["phase"] for row in bins)
    assert phases["calibration"] == 60 and phases["coordinated"] >= 30
    coordinated = [row for row in bins if row["phase"] == "coordinated"]
    assert all(set(row["current_correction_db"]) == set(TRACKS) for row in coordinated)
    assert all(abs(v) <= 3 for row in coordinated for v in row["current_correction_db"].values())
    gaps = rows(output / "gaps.jsonl")
    dropped = [gap for gap in gaps if gap["kind"] == "dropped_frames"]
    assert len(dropped) == 1 and dropped[0]["source"] == "KEYS"
    assert dropped[0]["missing_frames"] >= 2400
    incomplete = [gap for gap in gaps if gap["kind"] == "incomplete_bin"]
    assert incomplete and all(gap["missing_tracks"] == ["KEYS"] for gap in incomplete)
    # The coordinator hears of each incomplete bin, in grid order; the read-time drop itself
    # is logged but never notified, so one drop is not counted twice.
    assert summary["coordinator"]["gap_notifications"] == len(incomplete) < len(gaps) == summary["gaps"]
    assert summary["coordinator"]["event_types"]["gap"] == len(incomplete)
    gap_events = [event for row in coordinated for event in row["events"] if event["type"] == "gap"]
    assert gap_events and gap_events[0]["applied_db"] == {role: 0. for role in TRACKS}
    complete_before_gap = [row["bin_index"] for row in coordinated
                           if row["bin_index"] < incomplete[0]["bin_index"]]
    assert complete_before_gap == list(range(60, incomplete[0]["bin_index"]))
    assert summary["coordinator_bins"] == summary["coordinator"]["bins"] == phases["coordinated"]
    assert summary["late_frames"] == 0 and summary["unplaceable_frames"] == 0


def test_a_drop_on_a_node_the_coordinator_does_not_consume_never_resets_it(tmp_path):
    nodes = band()
    bass = node(nodes, "BASS", 1)  # Post tap: fed to V2 only.
    bass.drops.append((bass.origin + round(8.5 * RATE), 2400))
    summary, _, _, _, output = shadow(tmp_path, seconds=12., nodes=nodes, calibration_seconds=6.)
    assert summary["outcome"] == "completed"
    gaps = rows(output / "gaps.jsonl")
    assert [gap["kind"] for gap in gaps] == ["dropped_frames"] and gaps[0]["source"] == "BASS"
    assert summary["gaps"] == 1 and summary["gap_kinds"] == {"dropped_frames": 1}
    assert summary["coordinator"]["gap_notifications"] == 0
    assert "gap" not in summary["coordinator"]["event_types"]
    bins = rows(output / "coordinator-bins.jsonl")
    assert bins and not any(event["type"] == "gap" for row in bins for event in row["events"])
    assert summary["coordinator"]["bins"] >= 30
    bass_rows = [row for row in rows(output / "levels.jsonl") if row["node_id"] == bass.identity]
    assert sum(not row["measurement_valid"] for row in bass_rows) == 1


def test_calibration_failure_is_logged_and_the_run_continues(tmp_path):
    quiet = dict(PRE_LEVELS, RED=1e-6)
    summary, _, _, _, output = shadow(tmp_path, seconds=8., nodes=band(pre_levels=quiet),
                                      calibration_seconds=5.)
    assert summary["outcome"] == "completed"
    coordinator = summary["coordinator"]
    assert coordinator["calibrated"] and "lead energy" in coordinator["calibration_error"]
    assert coordinator["bins"] == summary["coordinator_bins"] == 0
    calibration = json.loads((output / "coordinator-calibration.json").read_text(encoding="utf-8"))
    assert calibration["plan"] is None and "lead energy" in calibration["error"]
    phases = Counter(row["phase"] for row in rows(output / "coordinator-bins.jsonl"))
    assert phases["calibration"] == 50 and phases["uncoordinated"] >= 25
    red = rows(output / "red-calibration.jsonl")
    assert red and all(row["reason"] == "waiting_activity" for row in red)
    assert summary["red_lock"]["ready"] is False
    assert summary["complete_groups"] >= 7


def test_red_calibration_rows_lock_on_the_pre_tap(tmp_path):
    summary, _, _, _, output = shadow(tmp_path, seconds=6.2, policies=("red",))
    red = rows(output / "red-calibration.jsonl")
    assert [row["bin_index"] for row in red] == list(range(len(red))) and len(red) >= 60
    assert all(row["valid"] and row["invalid_samples"] == 0 for row in red)
    assert all(row["raw_rms_dbfs"] == pytest.approx(-20.) for row in red)
    assert red[49]["newly_ready"] and red[49]["support_bins"] == 50
    assert summary["red_lock"] == {"ready": True, "ready_bin_index": 49, "gain_db": -6.}
    assert summary["red"]["median_dbfs"] == pytest.approx(-20.)
    assert summary["coordinator"] is None and summary["complete_groups"] == 0
    assert len(rows(output / "levels.jsonl")) == 6  # RED pre only: one source, six windows.


def test_run_waits_for_play_and_ends_when_the_transport_stops(tmp_path):
    summary, host, _, clock, output = shadow(
        tmp_path, seconds=60., playing=(START + 2., START + 7.), transport_stop_grace_seconds=1.)
    assert summary["reason"] == summary["stop_reason"] == "transport_stopped"
    assert summary["outcome"] == "completed"
    assert summary["collection"]["play_observed_at_seconds"] >= START + 2.
    assert summary["collection"]["started_at_seconds"] >= START + 2.
    assert START + 8. < clock.now < START + 8.5
    levels = rows(output / "levels.jsonl")
    assert levels and all(row["receipt_first_monotonic"] >= START + 2. for row in levels)
    assert max(row["source_window_index"] for row in levels) == 6
    inspections = rows(output / "bridge-inspections.jsonl")
    assert [record["transport"]["is_playing"] for record in inspections][:3] == [False, False, True]
    assert all(record["state_changed"] is False for record in inspections)
    before = json.loads((output / "host-before.json").read_text(encoding="utf-8"))
    after = json.loads((output / "host-after.json").read_text(encoding="utf-8"))
    assert before["transport"]["is_playing"] is False and before["snapshot"]["name"] == "NEWGDN-AutoMixTest-v2"
    assert after["diff"] == {"state_changed": False, "changed_paths": [], "changed_count": 0,
                             "transport_before": {"is_playing": False, "record_mode": False},
                             "transport_after": {"is_playing": False, "record_mode": False}}
    assert host.calls == {"inspect_session": summary["inspections"]}
    assert summary["host_calls"] == {"inspect_session": summary["inspections"]}
    assert summary["writes"] == 0 and summary["host_mutations"] == 0
    assert summary["declared"] == {"pcm_saved": False, "emulator_used": False, "live_eligible": False,
                                   "musical_quality_validated": False}
    assert summary["host_sample_alignment"] is False
    assert summary["host_pid"] == summary["inspected_pid"] == PID
    started = json.loads((output / "started.json").read_text(encoding="utf-8"))
    assert started["declared"] == {"pcm_saved": False, "emulator_used": False, "live_eligible": False,
                                   "musical_quality_validated": False, "host_mutations": 0}
    assert started["config"]["max_seconds"] == 2400 and started["config"]["fresh_wait_seconds"] == 30
    binding = json.loads((output / "binding.json").read_text(encoding="utf-8"))
    assert binding["host_pid"] == binding["inspected_pid"] == PID
    assert set(binding["bindings"]) == {"v2", "coordinator", "red"}
    assert binding["bindings"]["v2"]["binding"]["AC GUITAR"]["label"] == "ACGUITAR"
    baseline = json.loads((output / "node-baseline.json").read_text(encoding="utf-8"))
    assert len(baseline["zero_frame_reads"]) == 13
    assert "summary.json" in summary["files"] and "levels.jsonl" in summary["files"]


@pytest.mark.parametrize("host_clock, alignment", [(True, "host_sample"),
                                                   (False, "approximate_publication_clock")])
def test_no_wait_collects_the_whole_window_while_stopped(tmp_path, host_clock, alignment):
    # Longer than the five-second stop grace: the grace is armed only once Play was seen.
    summary, _, _, _, _ = shadow(tmp_path, seconds=7.2, playing=(math.inf, math.inf),
                                 wait_for_play=False, host_clock=host_clock)
    assert summary["outcome"] == "completed" and summary["reason"] == "collection_seconds_elapsed"
    assert summary["collection"]["play_observed_at_seconds"] is None
    assert summary["collection"]["ended_at_seconds"] >= START + 7.2
    assert summary["scalar_rows"] == 13 * 7
    # The plugin publishes its continuous host sample while stopped too; the grid follows it
    # once the lockstep check passed, and not otherwise.
    assert summary["collection"]["grid"]["alignment"] == alignment
    assert summary["collection"]["grid"]["lockstep_verified"] is host_clock


def test_bound_nodes_must_belong_to_the_inspected_live_process(tmp_path):
    clock = FakeClock()
    summary, host, bridge, _, output = shadow(tmp_path, seconds=2., host=FakeHost(clock, host_pid=PID + 1))
    assert summary["outcome"] == "failed" and summary["reason"] == "bind_failed"
    assert summary["detail"] == f"Bound nodes belong to pid {PID} but the inspected Live is pid {PID + 1}"
    assert summary["inspected_pid"] == PID + 1 and summary["host_pid"] is None
    assert summary["scalar_rows"] == 0 and not (output / "binding.json").exists()
    assert host.calls["inspect_session"] >= 1 and not bridge.handles


def test_a_snapshot_without_a_pid_leaves_the_inspected_pid_unknown(tmp_path):
    clock = FakeClock()
    summary, _, _, _, _ = shadow(tmp_path, seconds=1.2, host=FakeHost(clock, host_pid=None))
    assert summary["outcome"] == "completed" and summary["host_pid"] == PID
    assert summary["inspected_pid"] is None


def test_failed_first_inspection_is_a_setup_failure_not_an_early_stop(tmp_path):
    clock = FakeClock()
    host = FakeHost(clock)
    host.errors.append(RuntimeError("session_too_large"))
    summary, host, bridge, _, output = shadow(tmp_path, seconds=3., host=host)
    assert summary["outcome"] == "failed" and summary["reason"] == "inspection_failed"
    assert summary["detail"] == "RuntimeError: session_too_large"
    assert summary["scalar_rows"] == 0 and summary["collection"]["started_at_seconds"] is None
    assert not (output / "host-before.json").exists() and not (output / "binding.json").exists()
    assert bridge.opened == 0
    # The failed read and the final inspection are both counted: an attempt is a call.
    assert host.calls == {"inspect_session": 2} and summary["host_calls"] == {"inspect_session": 2}
    assert summary["host_after"]["error"] is None and summary["host_after"]["diff"] is None


def test_max_seconds_bounds_the_wait_for_play_plus_the_collection(tmp_path):
    summary, _, _, _, _ = shadow(tmp_path, seconds=4., playing=(START + 1., math.inf),
                                 max_seconds=6., name="fits")
    assert summary["outcome"] == "completed" and summary["reason"] == "collection_seconds_elapsed"
    assert summary["collection"]["max_seconds"] == 6.
    summary, _, _, clock, _ = shadow(tmp_path, seconds=4., playing=(START + 3., math.inf),
                                     max_seconds=6., name="late")
    assert summary["outcome"] == "stopped" and summary["reason"] == "max_seconds_reached"
    assert summary["collection"]["started_at_seconds"] == START + 3. and clock.now == START + 6.
    summary, _, _, clock, _ = shadow(tmp_path, seconds=4., playing=(math.inf, math.inf),
                                     max_seconds=6., name="never")
    assert summary["outcome"] == "failed" and summary["reason"] == "max_seconds_reached"
    assert summary["scalar_rows"] == 0 and clock.now == START + 6.


def test_a_node_that_stops_publishing_is_named_and_ends_the_wait(tmp_path):
    nodes = band()
    kit = node(nodes, "KIT", 0)
    kit.frozen_from = START + 1.  # After the bind, before Play.
    summary, _, _, clock, output = shadow(tmp_path, seconds=30., nodes=nodes,
                                          playing=(START + 2., math.inf), fresh_wait_seconds=2.)
    assert summary["outcome"] == "failed" and summary["reason"] == "nodes_not_fresh"
    assert summary["detail"] == "KIT stale"
    assert summary["scalar_rows"] == 0 and summary["collection"]["started_at_seconds"] is None
    assert summary["collection"]["play_observed_at_seconds"] == START + 2.
    assert clock.now == START + 4.
    waits = [event for event in rows(output / "events.jsonl") if event["kind"] == "waiting_for_fresh_nodes"]
    assert [event["at_monotonic"] for event in waits] == [START + 2., START + 3., START + 4.]
    for event in waits:
        assert [(entry["source"], entry["node_id"], entry["reason"]) for entry in event["nodes"]] == [
            ("KIT", kit.identity, "stale")]
        assert event["nodes"][0]["capture_age_ms"] >= 1000.
        assert event["waiting_since_monotonic"] == START + 2. and event["transport_playing"] is True


def test_host_state_change_stops_the_run_and_is_diffed(tmp_path):
    clock = FakeClock()
    host = FakeHost(clock)

    def nudge(snapshot):
        snapshot["tracks"][0]["volume"]["value"] = .5
    host.mutations.append((START + 3., nudge))
    summary, _, _, _, output = shadow(tmp_path, seconds=30., host=host)
    assert summary["reason"] == "host_state_changed" and summary["outcome"] == "stopped"
    assert summary["detail"] == "tracks[0].volume.value"  # The stop names what moved.
    assert summary["scalar_rows"] >= 13 * 2
    after = json.loads((output / "host-after.json").read_text(encoding="utf-8"))
    assert after["diff"]["state_changed"] is True
    assert after["diff"]["changed_paths"] == ["tracks[0].volume.value"]
    assert summary["host_after"]["diff"]["changed_count"] == 1
    inspections = rows(output / "bridge-inspections.jsonl")
    assert inspections[-1]["state_changed"] is True and inspections[0]["state_changed"] is False
    assert inspections[-1]["changed_paths"] == ["tracks[0].volume.value"]
    assert all(record["changed_paths"] == [] for record in inspections[:-1])


def test_a_stop_before_play_is_a_failure_because_nothing_was_collected(tmp_path):
    clock = FakeClock()
    host = FakeHost(clock, playing=(START + 10., math.inf))

    def nudge(snapshot):
        snapshot["tracks"][2]["devices"][0]["parameters"][0]["value"] = 0.
    host.mutations.append((START + 2., nudge))
    summary, _, _, clock, _ = shadow(tmp_path, seconds=30., host=host)
    assert summary["outcome"] == "failed" and summary["reason"] == "host_state_changed"
    assert summary["detail"] == "tracks[2].devices[0].parameters[0].value"
    assert summary["scalar_rows"] == 0 and clock.now == START + 2.


def test_host_calls_are_counted_by_operation_and_writes_are_the_rest():
    seen = []
    calls = HostCalls(lambda operation: seen.append(operation) or {"operation": operation})
    assert calls() == {"operation": "inspect_session"}
    assert calls("inspect_session") == {"operation": "inspect_session"}
    assert calls.writes() == 0 and dict(calls.counts) == {"inspect_session": 2}
    assert calls("status") == {"operation": "status"}
    assert calls.writes() == 1 and dict(calls.counts) == {"inspect_session": 2, "status": 1}
    assert seen == ["inspect_session", "inspect_session", "status"]


def test_playing_automation_values_are_not_state_changes_but_fader_moves_are():
    base = FakeHost(FakeClock()).inspect()
    automated = copy.deepcopy(base)
    automated["tracks"][1]["volume"]["automation_state"] = 1
    moved = copy.deepcopy(automated)
    moved["tracks"][1]["volume"]["value"] = .3
    moved["transport"]["is_playing"] = True
    moved["inspected_at"] += 1
    assert home_shadow.fingerprint(home_shadow.frozen_state(automated)) == \
        home_shadow.fingerprint(home_shadow.frozen_state(moved))
    assert home_shadow.fingerprint(home_shadow.frozen_state(base)) != \
        home_shadow.fingerprint(home_shadow.frozen_state(automated))
    plain = copy.deepcopy(base)
    plain["tracks"][1]["volume"]["value"] = .3
    assert home_shadow.changed_paths(home_shadow.frozen_state(base), home_shadow.frozen_state(plain)) == [
        "tracks[1].volume.value"]
    assert "transport" not in home_shadow.frozen_state(base)


def test_read_failures_log_the_native_code_and_invalidate_their_window(tmp_path):
    nodes = band()
    kit = node(nodes, "KIT", 1)
    kit.failures.append((START + 1.2, START + 1.4, -7))
    summary, _, _, _, output = shadow(tmp_path, seconds=3.2, nodes=nodes)
    failures = [event for event in rows(output / "events.jsonl") if event["kind"] == "read_failure"]
    assert len(failures) == 4 and {event["native_code"] for event in failures} == {-7}
    assert {event["source"] for event in failures} == {"KIT"}
    kit_rows = [row for row in rows(output / "levels.jsonl") if row["node_id"] == kit.identity]
    assert [row["read_failures"] for row in kit_rows] == [0, 4, 0]
    assert [row["measurement_valid"] for row in kit_rows] == [True, False, True]
    assert [row["missing_frames"] for row in kit_rows] == [0, 0, 0]
    assert summary["read_failures"] == 4 and summary["gaps"] == 0
    statuses = rows(output / "input-status.jsonl")
    # A failure inside the fixed nine-group warmup is a startup no-go, as the policy defines.
    assert statuses[1]["input_status"]["reason"] == "startup_no_go:invalid_or_gapped_source_window"
    assert statuses[0]["input_status"]["reason"] == "publication_timing_warmup"


def test_capture_age_comes_from_integer_clock_values_and_never_rounds_negative():
    # Seconds-times-1000 minus ns/1e6 can round a coincident callback to -1.5e-11 ms, which
    # the freshness rule reads as stale; the integer difference is exact.
    callback_ns = 129_000_000_000
    assert home_shadow._age_ms(callback_ns, callback_ns) == 0.
    assert home_shadow._age_ms(callback_ns + 20_000_000, callback_ns) == 20.
    assert home_shadow._age_ms(callback_ns, 0) is None


def test_threaded_monitor_inspects_once_per_request_and_records_transport(tmp_path):
    clock = FakeClock()
    host = FakeHost(clock, playing=(START + 1.5, math.inf))
    monitor = InspectionMonitor(host.inspect, time.monotonic_ns, tmp_path / "i.jsonl",
                                threaded=True)
    monitor.start()
    try:
        for expected in (False, True):
            clock.now += 1.
            assert monitor.request(time.monotonic(), clock.now)
            deadline = time.monotonic() + 5.
            while monitor.current() is None or monitor.current()["transport"]["is_playing"] is not expected:
                assert time.monotonic() < deadline, monitor.error
                time.sleep(.005)
        assert monitor.calls == 2 and host.calls == {"inspect_session": 2}
        assert monitor.current()["state_changed"] is False
    finally:
        assert monitor.close()
    assert [record["transport"]["is_playing"] for record in rows(tmp_path / "i.jsonl")] == [False, True]


def test_output_directory_must_be_new_and_policies_validated(tmp_path):
    (tmp_path / "run").mkdir()
    clock = FakeClock()
    host = FakeHost(clock)
    with pytest.raises(FileExistsError):
        run_home_shadow(ShadowConfig(output=tmp_path / "run", seconds=1.),
                        bridge_factory=lambda: None, inspect=host.inspect, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError, match="policies"):
        run_home_shadow(ShadowConfig(output=tmp_path / "other", seconds=1., policies=("fader",)),
                        bridge_factory=lambda: None, inspect=host.inspect, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError, match="max_seconds"):
        run_home_shadow(ShadowConfig(output=tmp_path / "other", seconds=10., max_seconds=5.),
                        bridge_factory=lambda: None, inspect=host.inspect, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError, match="fresh_wait_seconds"):
        run_home_shadow(ShadowConfig(output=tmp_path / "other", seconds=1., fresh_wait_seconds=0.),
                        bridge_factory=lambda: None, inspect=host.inspect, clock=clock, sleep=clock.sleep)
    assert host.calls == {} and not (tmp_path / "other").exists()


def test_bind_failure_is_reported_without_collection(tmp_path):
    nodes = [n for n in band() if not (n.role == "BASS" and n.position == 1)]
    summary, host, _, _, output = shadow(tmp_path, seconds=2., nodes=nodes)
    assert summary["outcome"] == "failed" and summary["reason"] == "bind_failed"
    assert "BASS" in summary["detail"] and summary["scalar_rows"] == 0
    assert host.calls["inspect_session"] >= 1 and (output / "host-before.json").exists()


def test_module_is_observe_only():
    with open(home_shadow.__file__, encoding="utf-8") as file:
        source = file.read()
    assert "from ..ableton" not in source and "wcfi_tools.ableton" not in source
    for forbidden in ("set_fader", "set_parameter", "insert_device", "move_device", "remove_device",
                      "reload_bridge", "start_playing", "stop_playing", "is_playing = True"):
        assert forbidden not in source
    for method in ("set_fader", "set_parameter", "insert", "move", "remove", "reload"):
        assert not hasattr(home_shadow, method)


@pytest.mark.skipif(sys.platform != "win32", reason="process limits are Windows-only")
def test_resource_guard_enforces_the_memory_ceiling_without_restricting_the_test_process():
    with pytest.raises(ShadowStop) as stop:
        ResourceGuard(1, restrict=False)
    assert stop.value.reason == "memory_limit_exceeded"
    guard = ResourceGuard(2 ** 40, restrict=False)
    assert guard.describe()["priority"] == "unverified" and guard.describe()["cpu_mask"] is None
    assert guard.peak_bytes > 0
    described = guard.describe()
    assert described["memory_metric"] == "PeakWorkingSetSize"
    assert described["peak_private_bytes"] > 0 and described["peak_memory_bytes"] > 0
    assert set(described["numerical_threads_env"]) == {
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"}
