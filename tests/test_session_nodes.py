"""Role binding by label and position against a fake node bridge; no native bridge."""

import ctypes as ct
import json

import numpy as np
import pytest

from wcfi_tools.audio.analysis_nodes import CAPACITY, Status
from wcfi_tools.audio.session_nodes import (
    LABEL_ALIASES,
    SessionNodeError,
    bind_roles,
    binding_record,
    inventory,
    read_node,
    source_map_revision,
)

CLOCK_NS = 10_000_000_000
ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")


def status(label, position, *, pid=4242, epoch=4, active=1, age_ms=20.0, callback_ns=None):
    if callback_ns is None:
        callback_ns = CLOCK_NS - round(age_ms * 1e6)
    return Status(version=1, pid=pid, channels=2, block_size=256, position=position,
                  active=active, epoch=epoch, flags=1, sample_rate=48000.0,
                  written_frame=480000 + position, first_frame=0, callback_ns=callback_ns,
                  host_sample=1000, host_system_ns=5, label=label.encode())


class FakeBridge:
    def __init__(self, nodes, *, unopenable=(), unreadable=()):
        self.nodes, self.unopenable, self.unreadable = dict(nodes), set(unopenable), set(unreadable)
        self.handles, self.opened, self.closed, self.reads = {}, [], [], []

    def discover(self):
        return list(self.nodes)

    def open(self, identity):
        if identity in self.unopenable:
            return 0
        handle = len(self.opened) + 1
        self.handles[handle] = identity
        self.opened.append(identity)
        return handle

    def read(self, handle, cursor):
        identity = self.handles[handle]
        self.reads.append((identity, cursor))
        if identity in self.unreadable:
            return None, None
        return np.empty((0, 2), np.float32), self.nodes[identity]

    def close(self, handle):
        self.closed.append(self.handles.pop(handle))

    def clock_ns(self):
        return CLOCK_NS


def band(**overrides):
    nodes = {}
    for index, label in enumerate(("RED", "ACGUITAR", "KIT", "BASS", "KEYS", "EGUITAR")):
        for position in (0, 1):
            nodes[f"{index:02d}{position}" + "a" * 29] = status(label, position)
    nodes.update(overrides)
    return nodes


def test_inventory_reads_metadata_once_per_node_and_maps_aliases():
    bridge = FakeBridge(band())
    snapshots = inventory(bridge)
    assert len(snapshots) == 12
    assert bridge.opened == bridge.closed == list(bridge.nodes) and not bridge.handles
    assert bridge.reads == [(identity, 0) for identity in bridge.nodes]
    guitar = [s for s in snapshots if s.label == "ACGUITAR"]
    assert {s.role for s in guitar} == {"AC GUITAR"} and LABEL_ALIASES["ACGUITAR"] == "AC GUITAR"
    assert {s.role for s in snapshots if s.label == "RED"} == {"RED"}
    first = snapshots[0]
    assert first.pid == 4242 and first.epoch == 4 and first.active and first.channels == 2
    assert first.sample_rate == 48000.0 and first.block_size == 256 and first.flags == 1
    assert first.capture_age_ms == pytest.approx(20.0)
    assert first.receipt_qpc_seconds == pytest.approx(10.0)
    assert first.written_frame == 480000 and first.callback_ns == CLOCK_NS - 20_000_000


def test_inventory_skips_and_reports_identities_that_fail_to_open_or_read():
    nodes = band()
    bad_open, bad_read = list(nodes)[2], list(nodes)[5]
    bridge = FakeBridge(nodes, unopenable=[bad_open], unreadable=[bad_read])
    skipped = []
    snapshots = inventory(bridge, skipped=skipped)
    assert {s.identity for s in snapshots} == set(nodes) - {bad_open, bad_read}
    assert skipped == [{"node_id": bad_open, "reason": "open_failed"},
                       {"node_id": bad_read, "reason": "read_failed"}]
    assert bad_open not in bridge.closed and bad_read in bridge.closed and not bridge.handles
    assert inventory(FakeBridge(nodes, unopenable=nodes)) == []


def test_bind_roles_selects_stage_by_position_in_role_order():
    snapshots = inventory(FakeBridge(band()))
    pre = bind_roles(snapshots, ROLES, stage="pre")
    post = bind_roles(snapshots, ROLES, stage="post")
    assert list(pre) == list(post) == list(ROLES)
    assert {entry["position"] for entry in pre.values()} == {0}
    assert {entry["position"] for entry in post.values()} == {1}
    assert pre["AC GUITAR"]["label"] == "ACGUITAR" and pre["AC GUITAR"]["node_id"].startswith("010")
    assert post["RED"]["node_id"].startswith("001")
    assert pre["KIT"] == {"node_id": pre["KIT"]["node_id"], "pid": 4242, "epoch": 4, "label": "KIT",
                          "position": 0, "channels": 2, "sample_rate": 48000.0}
    with pytest.raises(SessionNodeError, match="stage"):
        bind_roles(snapshots, ROLES, stage="mid")


def test_bind_roles_refuses_missing_and_ambiguous_roles():
    snapshots = inventory(FakeBridge(band()))
    with pytest.raises(SessionNodeError, match="LAPEL"):
        bind_roles(snapshots, ROLES + ("LAPEL",), stage="pre")
    without_post_kit = [s for s in snapshots if not (s.role == "KIT" and s.position == 1)]
    assert bind_roles(without_post_kit, ROLES, stage="pre")
    with pytest.raises(SessionNodeError, match="No post node.*KIT"):
        bind_roles(without_post_kit, ROLES, stage="post")
    duplicate = band(**{"d" * 32: status("RED", 0)})
    snapshots = inventory(FakeBridge(duplicate))
    with pytest.raises(SessionNodeError, match="ambiguous.*" + "d" * 32):
        bind_roles(snapshots, ROLES, stage="pre")
    assert bind_roles(snapshots, ROLES, stage="post")  # The duplicate is pre only.


def test_bind_roles_refuses_nodes_spanning_two_processes():
    nodes = band()
    nodes[list(nodes)[6]] = status("BASS", 0, pid=9999)
    snapshots = inventory(FakeBridge(nodes))
    with pytest.raises(SessionNodeError, match=r"more than one Live process.*4242, 9999"):
        bind_roles(snapshots, ROLES, stage="pre")
    assert bind_roles(snapshots, ROLES, stage="post")


@pytest.mark.parametrize("broken, message", [
    ({"active": 0}, "not active"),
    ({"age_ms": 500.0}, "stale.*500"),
    ({"age_ms": -1.0}, "stale"),
    ({"callback_ns": 0}, "stale.*None"),
])
def test_bind_roles_refuses_inactive_and_stale_nodes(broken, message):
    nodes = band()
    nodes[list(nodes)[8]] = status("KEYS", 0, **broken)
    snapshots = inventory(FakeBridge(nodes))
    with pytest.raises(SessionNodeError, match="KEYS.*" + message):
        bind_roles(snapshots, ROLES, stage="pre")
    assert bind_roles(snapshots, ROLES, stage="post")
    nodes[list(nodes)[8]] = status("KEYS", 0, age_ms=499.0)
    assert bind_roles(inventory(FakeBridge(nodes)), ROLES, stage="pre")["KEYS"]


class FakeLib:
    """Stands in for the bridge DLL: fills the caller's Status and buffer as WcfiNodeRead does."""

    def __init__(self, status, frames):
        self.status, self.frames, self.calls = status, frames, []

    def WcfiNodeRead(self, handle, cursor, count, pointer, status_ref):
        self.calls.append((handle, cursor, count))
        if self.frames < 0:
            return self.frames
        target = ct.cast(status_ref, ct.POINTER(Status)).contents
        ct.memmove(ct.byref(target), ct.byref(self.status), ct.sizeof(Status))
        frames = min(count, self.frames)
        if frames:
            np.ctypeslib.as_array(pointer, shape=(frames * 2,))[:] = .25
        return frames


class NativeBridge:
    """The NodeBridge surface read_node uses when no read_with_code exists: lib and buffer."""

    def __init__(self, lib):
        self.lib, self.buffer = lib, np.empty((CAPACITY, 2), np.float32)

    def discover(self):
        return ["n" * 32]

    def open(self, identity):
        return 77

    def close(self, handle):
        pass

    def clock_ns(self):
        return CLOCK_NS


def test_read_node_native_path_honours_the_frame_count_and_keeps_the_code():
    bridge = NativeBridge(FakeLib(status("KIT", 1, age_ms=10.), 600))
    code, block, got = read_node(bridge, 77, 7, 0)
    assert (code, block.shape, got.pid, got.label) == (0, (0, 2), 4242, b"KIT")
    assert bridge.lib.calls[-1] == (77, 7, 0)
    code, block, got = read_node(bridge, 77, 7)
    assert (code, block.shape, got.epoch) == (600, (600, 2), 4)
    assert bridge.lib.calls[-1] == (77, 7, CAPACITY)
    bridge.buffer[:] = 0.
    assert float(block[0, 0]) == .25  # The block is a copy, not a view of the shared buffer.
    snapshots = inventory(bridge)
    assert [(s.identity, s.role, s.capture_age_ms) for s in snapshots] == [("n" * 32, "KIT", 10.)]
    assert bridge.lib.calls[-1] == (77, 0, 0)
    assert read_node(NativeBridge(FakeLib(status("KIT", 1), -3)), 77, 0, 0) == (-3, None, None)
    assert inventory(NativeBridge(FakeLib(status("KIT", 1), -3))) == []


def test_binding_record_is_json_and_revision_ignores_key_order():
    binding = bind_roles(inventory(FakeBridge(band())), ROLES, stage="post")
    record = binding_record(binding, stage="post", roles=ROLES)
    assert json.loads(json.dumps(record, allow_nan=False)) == record
    assert record["stage"] == "post" and record["roles"] == list(ROLES)
    assert record["host_pid"] == 4242 and record["epoch"] == 4
    assert record["binding"] == binding and record["binding"] is not binding
    assert record["source_map_revision"] == source_map_revision(binding)
    assert len(record["source_map_revision"]) == 64
    reordered = {role: dict(reversed(list(binding[role].items()))) for role in reversed(ROLES)}
    assert list(reordered) != list(binding)
    assert source_map_revision(reordered) == record["source_map_revision"]
    changed = {**binding, "RED": {**binding["RED"], "epoch": 5}}
    assert source_map_revision(changed) != record["source_map_revision"]
    assert binding_record(changed, stage="post", roles=ROLES)["epoch"] is None
    with pytest.raises(SessionNodeError, match="order"):
        binding_record(binding, stage="post", roles=tuple(reversed(ROLES)))
    with pytest.raises(SessionNodeError, match="exactly one"):
        binding_record({**binding, "RED": {**binding["RED"], "pid": 1}}, stage="post", roles=ROLES)
