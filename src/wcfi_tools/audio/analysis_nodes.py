"""Passive VST3 observation, independent of the DAW's selected audio interface.

All native reads and analysis happen on a service worker. Dropping observation
frames cannot backpressure a plugin's audio callback. Audio stays on this PC.
"""
from __future__ import annotations

import copy
import ctypes as ct
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .alignment import estimate_delay

CAPACITY = 32768


class Info(ct.Structure):
    _fields_ = [("id", ct.c_char * 40), ("pid", ct.c_uint32)]


class Status(ct.Structure):
    _fields_ = [(key, ct.c_uint32) for key in
               ("version", "pid", "channels", "block_size", "position", "active", "epoch", "flags")] + [
        ("sample_rate", ct.c_double), ("written_frame", ct.c_uint64), ("first_frame", ct.c_uint64),
        ("callback_ns", ct.c_uint64), ("host_sample", ct.c_int64), ("host_system_ns", ct.c_int64),
        ("label", ct.c_char * 128)]


def bridge_path():
    return Path(__file__).resolve().parents[3] / "native/wcfi_analysis_node/build/bin/WcfiAnalysisBridge.dll"


class NodeBridge:
    def __init__(self, path=None):
        if sys.platform != "win32":
            raise RuntimeError("Analysis Node currently requires Windows x64")
        self.lib = ct.CDLL(str(Path(path or bridge_path()).resolve(strict=True)))
        self.lib.WcfiNodesEnumerate.argtypes = [ct.POINTER(Info), ct.c_uint32]
        self.lib.WcfiNodesEnumerate.restype = ct.c_int
        self.lib.WcfiNodeOpen.argtypes = [ct.c_char_p]
        self.lib.WcfiNodeOpen.restype = ct.c_void_p
        self.lib.WcfiNodeClose.argtypes = [ct.c_void_p]
        self.lib.WcfiNodeClose.restype = None
        self.lib.WcfiNodeRead.argtypes = [ct.c_void_p, ct.c_uint64, ct.c_uint32,
                                         ct.POINTER(ct.c_float), ct.POINTER(Status)]
        self.lib.WcfiNodeRead.restype = ct.c_int
        self.lib.WcfiNodeClockNs.argtypes = []
        self.lib.WcfiNodeClockNs.restype = ct.c_uint64
        self.buffer = np.empty((CAPACITY, 2), np.float32)

    def discover(self):
        items = (Info * 128)()
        count = self.lib.WcfiNodesEnumerate(items, len(items))
        return [item.id.decode("ascii") for item in items[:max(0, count)]]

    def open(self, identity):
        return self.lib.WcfiNodeOpen(identity.encode("ascii"))

    def close(self, handle):
        self.lib.WcfiNodeClose(handle)

    def read(self, handle, cursor):
        status = Status()
        count = self.lib.WcfiNodeRead(handle, cursor, CAPACITY,
                                    self.buffer.ctypes.data_as(ct.POINTER(ct.c_float)), ct.byref(status))
        return (None, None) if count < 0 else (self.buffer[:count].copy(), status)

    def clock_ns(self):
        return self.lib.WcfiNodeClockNs()


class Stream:
    def __init__(self, handle):
        self.handle, self.cursor, self.epoch = handle, 0, None
        self.status = None
        self.ring = None
        self.count = 0
        self.gaps = 0
        self.seen = time.monotonic()
        self.clock_valid = False

    def append(self, block, status):
        reset = self.epoch != status.epoch or self.ring is None or len(self.ring) != round(status.sample_rate * 3)
        previous = self.status
        if reset:
            self.ring = np.zeros((round(status.sample_rate * 3), 2), np.float32)
            self.count, self.gaps = 0, 0
        elif status.first_frame > self.cursor:
            self.gaps += status.first_frame - self.cursor
            self.count = 0  # Never splice across dropped observation audio.
        if len(block):
            self.clock_valid = bool(not reset and previous and status.flags & previous.flags & 1 and
                                    status.host_sample + status.block_size - previous.host_sample - previous.block_size ==
                                    status.written_frame - previous.written_frame)
            block = block[-len(self.ring):]
            start = self.count % len(self.ring)
            first = min(len(block), len(self.ring) - start)
            self.ring[start:start + first] = block[:first]
            self.ring[:len(block) - first] = block[first:]
            self.count += len(block)
        elif reset:
            self.clock_valid = False
        self.status, self.epoch = status, status.epoch
        self.cursor = status.first_frame + len(block)

    def recent(self, frames):
        count = min(frames, self.count, len(self.ring))
        start = (self.count - count) % len(self.ring)
        if start + count <= len(self.ring):
            return self.ring[start:start + count].copy()
        first = self.ring[start:]
        return np.concatenate((first, self.ring[:count - len(first)]))


def db(value):
    return max(-120., float(20 * np.log10(max(float(value), 1e-6))))


def waveform_envelope(wave):
    """Keep the 60-bucket display extrema without thousands of Python reductions."""
    low, high = np.zeros((60, 2)), np.zeros((60, 2))
    if len(wave) >= 60:
        width, remainder = divmod(len(wave), 60)
        buckets = np.arange(60)
        starts = buckets * width + np.minimum(buckets, remainder)
        low = np.minimum.reduceat(wave, starts, axis=0)
        high = np.maximum.reduceat(wave, starts, axis=0)
    elif len(wave):
        low[:len(wave)] = high[:len(wave)] = wave
    return [[{"min": minimum, "max": maximum}
             for minimum, maximum in zip(mins, maxes)]
            for mins, maxes in zip(low.T.tolist(), high.T.tolist())]


class AnalysisNodes:
    def __init__(self, bridge_factory=NodeBridge):
        self.factory, self.bridge = bridge_factory, None
        self.streams = {}
        self.next_discovery = self.next_view = self.next_alignment = self.next_retry = 0.
        self.lock = threading.Lock()
        self.view = {"available": False, "streams": [], "latencies": [], "sequence": 0,
                     "compute_ms": 0., "error": None}
        self.view_at = 0.
        self.latencies = []

    def tick(self):
        now = time.monotonic()
        if not self.bridge:
            if now < self.next_retry:
                return
            self.next_retry = now + 5
            self.bridge = self.factory()
        if now >= self.next_discovery:
            self.next_discovery = now + .5
            for identity in self.bridge.discover():
                if identity not in self.streams:
                    handle = self.bridge.open(identity)
                    if not handle:
                        continue
                    self.streams[identity] = Stream(handle)
                self.streams[identity].seen = now
            for identity in list(self.streams):
                if now - self.streams[identity].seen > 3:
                    self.bridge.close(self.streams.pop(identity).handle)
        for stream in self.streams.values():
            block, status = self.bridge.read(stream.handle, stream.cursor)
            if status is None:
                continue
            if stream.epoch is not None and stream.epoch != status.epoch:
                block, status = self.bridge.read(stream.handle, 0)
                if status is None:
                    continue
            if not 8000 <= status.sample_rate <= 192000 or status.channels not in (1, 2):
                continue
            stream.append(block, status)
        if now >= self.next_view:
            self.next_view = now + .1
            self.update()

    def update(self):
        began = time.monotonic()
        clock = self.bridge.clock_ns()
        views, groups = [], {}
        for identity, stream in self.streams.items():
            s = stream.status
            if s is None:
                continue
            age = (clock - s.callback_ns) / 1e6 if s.callback_ns else None
            fresh = bool(s.active and age is not None and 0 <= age < 500)
            recent = stream.recent(max(1, round(s.sample_rate / 20)))
            invalid = int(np.sum(~np.isfinite(recent[:, :s.channels])))
            clean = np.nan_to_num(recent, nan=0, posinf=0, neginf=0)
            peak = np.max(np.abs(clean), axis=0) if len(clean) else [0, 0]
            rms = np.sqrt(np.mean(clean.astype(float) ** 2, axis=0)) if len(clean) else [0, 0]
            wave = np.nan_to_num(stream.recent(round(s.sample_rate / 2)), nan=0, posinf=0, neginf=0)
            label = s.label.decode("utf-8", errors="replace") or "Unassigned"
            views.append({"id": identity, "pid": s.pid, "label": label, "stage": "After effects" if s.position else "Before effects",
                          "position": s.position, "active": fresh, "signal": bool(fresh and max(peak) > 1e-5),
                          "peak_dbfs": [db(v) if fresh else -120 for v in peak],
                          "rms_dbfs": [db(v) if fresh else -120 for v in rms],
                          "full_scale_samples": int(np.sum(np.abs(clean[:, :s.channels]) >= 1)), "invalid_samples": invalid,
                          "waveform": waveform_envelope(wave) if fresh else [[], []],
                          "sample_rate": s.sample_rate, "channels": s.channels, "epoch": s.epoch,
                          "capture_age_ms": age, "observation_gaps": stream.gaps,
                          "block_ms": s.block_size / s.sample_rate * 1000})
            if fresh and label != "Unassigned":
                groups.setdefault((s.pid, label), []).append(stream)
        if began >= self.next_alignment:
            self.next_alignment = began + .5
            self.latencies = []
            for (pid, label), items in groups.items():
                before = [x for x in items if x.status.position == 0]
                after = [x for x in items if x.status.position == 1]
                result = {"available": False, "reason": "Need exactly one before and one after node"}
                if len(before) == len(after) == 1:
                    a, b = before[0], after[0]
                    result = self.align(a, b)
                self.latencies.append({"id": f"{pid}:{label}", "label": f"{label} · before → after", **result})
        with self.lock:
            self.view = {"available": True, "streams": sorted(views, key=lambda s: (s["label"], s["position"], s["id"])),
                         "latencies": self.latencies, "sequence": self.view["sequence"] + 1,
                         "compute_ms": (time.monotonic() - began) * 1000, "error": None}
            self.view_at = time.monotonic()

    @staticmethod
    def align(a, b):
        if a.status.sample_rate != b.status.sample_rate or not a.clock_valid or not b.clock_valid:
            return {"available": False, "reason": "Waiting for a shared, advancing host sample clock"}
        # Align captures by the host's continuous sample positions, not UI receipt
        # time or independently started plugin counters. Refuse ambiguous clocks.
        end_a = a.status.host_sample + a.status.block_size
        end_b = b.status.host_sample + b.status.block_size
        end = min(end_a, end_b)
        rate = a.status.sample_rate
        count = min(round(rate * 1.5), a.count - (end_a - end), b.count - (end_b - end))
        if count < rate // 2 or max(end_a, end_b) - end > rate / 2:
            return {"available": False, "reason": "Collecting overlapping audio"}
        x = a.recent(count + end_a - end)
        y = b.recent(count + end_b - end)
        return estimate_delay(x[:count, 0], y[:count, 0], rate=rate)

    def failure(self, exc):
        with self.lock:
            self.view = {**self.view, "error": str(exc)}

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.view)
            result["view_age_ms"] = (time.monotonic() - self.view_at) * 1000 if self.view_at else None
            return result

    def close(self):
        if self.bridge:
            for stream in self.streams.values():
                self.bridge.close(stream.handle)
        self.streams.clear()
