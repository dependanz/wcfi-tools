"""Bounded, authenticated PCM copies for local node auditioning.

Each HTTP reader owns its native handle and scratch buffer. Nothing is routed
back to the DAW and no audio device, recorder or transport is controlled here.
"""
from __future__ import annotations

import re
import struct
import time

import numpy as np

from .analysis_nodes import CAPACITY, NodeBridge

HEADER = struct.Struct("<4sIIIIQQdI")


def read_node_audio(identity, *, cursor=None, epoch=None, bridge_factory=NodeBridge):
    if not isinstance(identity, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", identity):
        raise ValueError("Choose an Analysis Node from the current session")
    if cursor is not None and (type(cursor) is not int or not 0 <= cursor <= 2**53 - 1):
        raise ValueError("Invalid audio cursor")
    if epoch is not None and (type(epoch) is not int or not 0 <= epoch < 2**32):
        raise ValueError("Invalid audio epoch")
    if (cursor is None) != (epoch is None):
        raise ValueError("Audio cursor and epoch must be supplied together")
    bridge = bridge_factory()
    handle = bridge.open(identity)
    if not handle:
        raise RuntimeError("This Analysis Node is no longer available")

    def read(start):
        for _ in range(3):
            block, status = bridge.read(handle, start)
            if status is not None:
                return block, status
            time.sleep(.002)  # Only this HTTP worker waits; never the audio callback.
        raise RuntimeError("The Analysis Node is busy; try Listen again")

    try:
        block, status = read(cursor or 0)
        if not 8000 <= status.sample_rate <= 192000 or status.channels not in (1, 2):
            raise RuntimeError("Unsupported Analysis Node audio format")
        age = (bridge.clock_ns() - status.callback_ns) / 1e6 if status.callback_ns else -1
        if not status.active or not 0 <= age < 500:
            raise RuntimeError("This Analysis Node is not receiving fresh audio")
        reset = (cursor is None or epoch != status.epoch or cursor < status.first_frame
                 or cursor > status.written_frame)
        if not reset:
            # Pace sequential network pulls here, rather than relying on a browser
            # timer that may be throttled when the desktop window loses focus.
            deadline = time.monotonic() + .06
            while len(block) < round(status.sample_rate * .04) and time.monotonic() < deadline:
                time.sleep(.006)
                block, status = read(cursor)
                reset = (epoch != status.epoch or cursor < status.first_frame
                         or cursor > status.written_frame)
                if reset or not status.active:
                    break
        if reset:
            # Start near now, rather than replaying the observation ring's history.
            block, status = read(max(0, status.written_frame - round(status.sample_rate * .12)))
        age = (bridge.clock_ns() - status.callback_ns) / 1e6 if status.callback_ns else -1
        if (not status.active or not 0 <= age < 500 or status.channels not in (1, 2)
                or not 8000 <= status.sample_rate <= 192000):
            raise RuntimeError("This Analysis Node changed or stopped; try Listen again")
        limit = min(CAPACITY, round(status.sample_rate * .20))
        dropped = max(0, len(block) - limit)
        reset = reset or dropped > 0
        block = block[dropped:, :status.channels]
        invalid = bool(np.any(~np.isfinite(block)))
        clean = np.nan_to_num(block, nan=0, posinf=0, neginf=0).astype('<f4', copy=False)
        first = status.first_frame + dropped
        flags = int(reset) | (2 if invalid else 0)
        return HEADER.pack(b'WCFA', 1, round(status.sample_rate), status.channels,
                           status.epoch, first, first + len(clean), age, flags) + clean.tobytes()
    finally:
        bridge.close(handle)
