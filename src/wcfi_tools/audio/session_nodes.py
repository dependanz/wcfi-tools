"""Bind Analysis Nodes by label and position for the Live process running now.

Node identities change with every Live process, so a shadow run never pins them in
advance. It inventories the published nodes with zero-frame metadata reads, binds each
policy role to exactly one fresh node of one process, and records the binding with the
digest the policies carry as their source-map revision. Nothing here writes to the host.
"""
from __future__ import annotations

import ctypes as ct
import hashlib
import json
from dataclasses import dataclass

import numpy as np

from .analysis_nodes import CAPACITY, Status

LABEL_ALIASES = {"ACGUITAR": "AC GUITAR"}
POSITIONS = {"pre": 0, "post": 1}
FRESH_MS = 500.0


class SessionNodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeSnapshot:
    identity: str
    pid: int
    epoch: int
    label: str
    role: str
    position: int
    channels: int
    sample_rate: float
    active: bool
    flags: int
    block_size: int
    written_frame: int
    callback_ns: int
    capture_age_ms: float | None
    receipt_qpc_seconds: float


def decode_label(value):
    if isinstance(value, bytes):
        value = value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return str(value)


def snapshot(identity, status, clock_ns):
    """One node's published metadata, aged against the native clock at receipt."""
    label = decode_label(status.label)
    callback_ns = int(status.callback_ns)
    age = (clock_ns - callback_ns) / 1e6 if callback_ns else None
    return NodeSnapshot(identity=identity, pid=int(status.pid), epoch=int(status.epoch),
                        label=label, role=LABEL_ALIASES.get(label, label),
                        position=int(status.position), channels=int(status.channels),
                        sample_rate=float(status.sample_rate), active=bool(status.active),
                        flags=int(status.flags), block_size=int(status.block_size),
                        written_frame=int(status.written_frame), callback_ns=callback_ns,
                        capture_age_ms=age, receipt_qpc_seconds=clock_ns / 1e9)


def read_node(bridge, handle, cursor, count=CAPACITY):
    """Read a node; return ``(code, block, status)`` where ``code < 0`` is the native failure.

    ``NodeBridge.read`` drops the native return code and always asks for CAPACITY frames.
    A bridge exposing ``read_with_code`` (fakes) or its native ``lib`` and ``buffer``
    (``NodeBridge``) keeps the code and honours ``count``; a zero ``count`` is the
    metadata-only read the October 25 collectors made as ``WcfiNodeRead(handle, 0, 0)``.
    """
    reader = getattr(bridge, "read_with_code", None)
    if reader is not None:
        return reader(handle, cursor, count)
    lib, buffer = getattr(bridge, "lib", None), getattr(bridge, "buffer", None)
    if lib is not None and buffer is not None:
        status = Status()
        code = lib.WcfiNodeRead(handle, int(cursor), min(int(count), len(buffer)),
                                buffer.ctypes.data_as(ct.POINTER(ct.c_float)), ct.byref(status))
        if code < 0:
            return int(code), None, None
        return int(code), buffer[:code].copy(), status
    block, status = bridge.read(handle, cursor)
    if status is None:
        return -1, None, None
    block = np.asarray(block)[:int(count)]
    return len(block), block, status


def inventory(bridge, *, skipped=None):
    """Open, metadata-read and close every published node.

    The read asks for zero frames: only the status matters here. An identity that does
    not open or read is skipped and, when `skipped` is a list, reported in it.
    """
    snapshots = []
    for identity in bridge.discover():
        handle = bridge.open(identity)
        if not handle:
            if skipped is not None:
                skipped.append({"node_id": identity, "reason": "open_failed"})
            continue
        try:
            code, _, status = read_node(bridge, handle, 0, 0)
            clock_ns = int(bridge.clock_ns())
        finally:
            bridge.close(handle)
        if code < 0 or status is None:
            if skipped is not None:
                skipped.append({"node_id": identity, "reason": "read_failed"})
            continue
        snapshots.append(snapshot(identity, status, clock_ns))
    return snapshots


def bind_roles(snapshots, roles, *, stage):
    """Map each role to its one fresh node at `stage`, all within one Live process."""
    if stage not in POSITIONS:
        raise SessionNodeError(f"Unknown stage {stage!r}; expected one of {sorted(POSITIONS)}")
    position = POSITIONS[stage]
    binding = {}
    for role in roles:
        found = [s for s in snapshots if s.role == role and s.position == position]
        if not found:
            raise SessionNodeError(f"No {stage} node is published for role {role!r}")
        if len(found) > 1:
            identities = ", ".join(sorted(s.identity for s in found))
            raise SessionNodeError(f"Role {role!r} is ambiguous at {stage}: {identities}")
        node, = found
        if not node.active:
            raise SessionNodeError(f"Node {node.identity} for role {role!r} is not active")
        if node.capture_age_ms is None or not 0 <= node.capture_age_ms < FRESH_MS:
            raise SessionNodeError(f"Node {node.identity} for role {role!r} is stale: "
                                   f"capture age {node.capture_age_ms} ms")
        binding[role] = {"node_id": node.identity, "pid": node.pid, "epoch": node.epoch,
                         "label": node.label, "position": node.position,
                         "channels": node.channels, "sample_rate": node.sample_rate}
    pids = sorted({entry["pid"] for entry in binding.values()})
    if len(pids) > 1:
        raise SessionNodeError(f"Bound nodes span more than one Live process: pids {pids}")
    return binding


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def source_map_revision(binding):
    return hashlib.sha256(canonical(binding).encode()).hexdigest()


def binding_record(binding, *, stage, roles):
    """JSON-able record of a binding; `epoch` is the shared epoch or None if nodes differ."""
    if list(binding) != list(roles):
        raise SessionNodeError("Binding roles do not match the requested roles in order")
    pids = {entry["pid"] for entry in binding.values()}
    if len(pids) != 1:
        raise SessionNodeError(f"Binding must name exactly one Live process, not {sorted(pids)}")
    epochs = {entry["epoch"] for entry in binding.values()}
    return {"stage": stage, "roles": list(roles),
            "binding": {role: dict(entry) for role, entry in binding.items()},
            "source_map_revision": source_map_revision(binding), "host_pid": pids.pop(),
            "epoch": epochs.pop() if len(epochs) == 1 else None}
