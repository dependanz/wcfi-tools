"""Observe-only home shadow of the open Live set's Analysis Nodes.

The nodes of the Live process running now are bound by label and position, never by
identities pinned in advance. Once the host transport plays, bounded scalar windows and
100 ms bins are collected and three existing policies run in shadow: PublicationClockGainV2
on the post taps, MixCoordinator on the dry pre taps after a calibration window, and
ActivityCalibration on RED's pre tap. Every decision is logged; nothing is applied.

Every host call goes through the supplied ``inspect`` callable and is counted by operation
name; the shadow asks only for ``inspect_session``, so the summary's ``writes`` is measured
as zero rather than asserted. The native bridge is read-only shared memory. PCM lives in
bounded scratch buffers and is never written to disk.
"""
from __future__ import annotations

import contextlib
import copy
import ctypes as ct
import hashlib
import json
import math
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .activity_calibration import ActivityCalibration
from .analysis_nodes import CAPACITY
from .experimental_audio_gain import ROLES
from .experimental_live_input import PublicationClockGainV2
from .mix_coordinator import TRACKS, CoordinatorSettings, MixCoordinator
from .session_nodes import (
    SessionNodeError,
    bind_roles,
    binding_record,
    inventory,
    read_node,
    source_map_revision,
)

POLICIES = ("v2", "coordinator", "red")
STAGES = {"pre": "before_effects", "post": "after_effects_before_fader"}
FRESH_MS = 500.0
INSPECTION_INTERVAL = 1.0
INSPECTION_WORKER = True  # Tests run the inspection inline for deterministic simulated time.
INSPECT_SESSION = "inspect_session"
MAX_LAG_BINS = 10
CHANGED_PATHS_IN_STOP = 20
RECEIPT_CLOCK = "native_WcfiNodeClockNs_QPC_seconds"
STATE_TIME_INTERPRETATION = "response_receipt_with_query_start_freshness_bound"
FINGERPRINT_KEYS = ("name", "file_path", "tracks")
TIME_VARYING_KEYS = frozenset({"inspected_at", "transport", "display"})
AUTOMATION_PLAYING = 1
HASHED_MODULES = ("session_nodes.py", "home_shadow.py", "experimental_live_input.py",
                  "experimental_audio_gain.py", "experimental_audio_gain_v2.py",
                  "mix_coordinator.py", "mix_coordinator_trial.py", "activity_calibration.py")
# What the harness is built not to do. These are design statements, listed apart from the
# measured counts so a reader can tell the two kinds apart.
DECLARED = {"pcm_saved": False, "emulator_used": False, "live_eligible": False,
            "musical_quality_validated": False}
ROW_FILES = {"levels": "levels.jsonl", "groups": "window-groups.jsonl",
             "input": "input-status.jsonl", "decisions": "v2-decisions.jsonl",
             "proposals": "v2-proposals.jsonl", "bins": "coordinator-bins.jsonl",
             "red": "red-calibration.jsonl", "gaps": "gaps.jsonl", "events": "events.jsonl"}
_MISSING = object()


class ShadowStop(RuntimeError):
    """A bounded stop of the shadow; ``reason`` is the summary's stop reason."""

    def __init__(self, reason, detail=None):
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


@dataclass
class ShadowConfig:
    output: Path
    seconds: float
    wait_for_play: bool = True
    coordinator_stage: str = "pre"
    v2_stage: str = "post"
    red_stage: str = "pre"
    calibration_seconds: float = 30.0
    policies: tuple[str, ...] = ("v2", "coordinator", "red")
    poll_interval: float = 0.05
    transport_stop_grace_seconds: float = 5.0
    memory_limit_bytes: int = 256 * 1024 * 1024
    max_seconds: float = 2400.0  # Bounds the wait for Play plus the collection, from the start.
    fresh_wait_seconds: float = 30.0  # How long a playing host may keep a bound node stale.


class HostCalls:
    """The one path to the host.

    Every operation is counted by name before it is made, so the summary's ``host_calls``
    and ``writes`` (the calls other than ``inspect_session``) are measurements of what this
    process asked the bridge for, not statements of intent.
    """

    def __init__(self, call):
        self.call, self.counts = call, Counter()

    def __call__(self, operation=INSPECT_SESSION):
        self.counts[operation] += 1
        return self.call(operation)

    def writes(self):
        return sum(count for operation, count in self.counts.items() if operation != INSPECT_SESSION)


def utc_now():
    return datetime.now(UTC).isoformat()


def write_json(path, value, *, allow_nan=False):
    with Path(path).open("x", encoding="utf-8") as file:
        json.dump(value, file, indent=2, allow_nan=allow_nan)
        file.write("\n")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def frozen_state(snapshot):
    """Host state without the transport or time-varying fields.

    Tracks, devices, parameters, routing and faders are kept. A parameter whose
    automation is playing changes with the arrangement, so its value is left out while
    its identity and automation state stay in.
    """
    def prune(value):
        if isinstance(value, dict):
            playing = value.get("automation_state") == AUTOMATION_PLAYING
            return {key: prune(item) for key, item in value.items()
                    if key not in TIME_VARYING_KEYS and not (playing and key == "value")}
        if isinstance(value, list):
            return [prune(item) for item in value]
        return value
    return {key: prune(snapshot.get(key)) for key in FINGERPRINT_KEYS}


def changed_paths(before, after, path="", found=None, limit=200):
    """Dotted paths whose values differ, for the host-after diff summary."""
    found = [] if found is None else found
    if len(found) >= limit:
        return found
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            changed_paths(before.get(key, _MISSING), after.get(key, _MISSING),
                          f"{path}.{key}" if path else str(key), found, limit)
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, (b, a) in enumerate(zip(before, after)):
            changed_paths(b, a, f"{path}[{index}]", found, limit)
    elif before is _MISSING or after is _MISSING or before != after:
        found.append(path or "<root>")
    return found


def _label(value):
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return str(value)


def _age_ms(receipt_ns, callback_ns):
    """Capture age from integer native clock values; float seconds would round negative."""
    return (receipt_ns - callback_ns) / 1e6 if callback_ns else None


def _stereo(block, channels):
    block = np.asarray(block)
    if channels == 1 or block.shape[1] == 1:
        return np.repeat(block[:, :1], 2, axis=1)
    return block[:, :2]


class _MemoryCounters(ct.Structure):
    _fields_ = [("cb", ct.c_uint32), ("PageFaultCount", ct.c_uint32)] + [
        (name, ct.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
            "PeakPagefileUsage", "PrivateUsage")]


class ResourceGuard:
    """Idle priority, one allowed CPU and a memory ceiling, as the live diagnostics kept.

    Priority and affinity are best effort and reported; the memory ceiling and a changed
    constraint stop the run.
    """
    IDLE_PRIORITY = 0x40

    def __init__(self, memory_limit_bytes, *, restrict=True):
        self.limit = int(memory_limit_bytes)
        self.peak_bytes = 0  # Peak working set: the resident footprint the ceiling bounds.
        self.peak_private_bytes = 0  # Peak commit, reported only: BLAS reserves arenas at import.
        self.numerical_threads_env = {key: os.environ.get(key) for key in
                                      ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")}
        self.priority = "unverified"
        self.cpu_mask = None
        self.notes = []
        self.kernel = self.psapi = self.process = None
        if sys.platform != "win32":
            self.notes.append("process limits need Windows")
            return
        try:
            kernel = ct.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], ct.c_void_p
            kernel.SetPriorityClass.argtypes = [ct.c_void_p, ct.c_uint32]
            kernel.SetPriorityClass.restype = ct.c_int
            kernel.GetPriorityClass.argtypes = [ct.c_void_p]
            kernel.GetPriorityClass.restype = ct.c_uint32
            kernel.GetProcessAffinityMask.argtypes = [ct.c_void_p, ct.POINTER(ct.c_size_t),
                                                      ct.POINTER(ct.c_size_t)]
            kernel.GetProcessAffinityMask.restype = ct.c_int
            kernel.SetProcessAffinityMask.argtypes = [ct.c_void_p, ct.c_size_t]
            kernel.SetProcessAffinityMask.restype = ct.c_int
            psapi = ct.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [ct.c_void_p, ct.POINTER(_MemoryCounters),
                                                   ct.c_uint32]
            psapi.GetProcessMemoryInfo.restype = ct.c_int
        except (OSError, AttributeError) as exc:
            self.notes.append(f"process limits unavailable: {exc}")
            return
        self.kernel, self.psapi = kernel, psapi
        self.process = kernel.GetCurrentProcess()
        if restrict:
            if kernel.SetPriorityClass(self.process, self.IDLE_PRIORITY):
                self.priority = "idle"
            else:
                self.notes.append("could not set idle priority")
            allowed, system = ct.c_size_t(), ct.c_size_t()
            if kernel.GetProcessAffinityMask(self.process, ct.byref(allowed), ct.byref(system)) \
                    and allowed.value:
                mask = 1 << (allowed.value.bit_length() - 1)
                if kernel.SetProcessAffinityMask(self.process, mask):
                    self.cpu_mask = mask
                else:
                    self.notes.append("could not restrict to one allowed CPU")
            else:
                self.notes.append("could not determine the allowed CPU mask")
        self.check()

    def check(self):
        if self.psapi is None:
            return
        info = _MemoryCounters()
        info.cb = ct.sizeof(info)
        if not self.psapi.GetProcessMemoryInfo(self.process, ct.byref(info), info.cb):
            raise ShadowStop("memory_check_failed")
        self.peak_bytes = max(self.peak_bytes, info.PeakWorkingSetSize)
        self.peak_private_bytes = max(self.peak_private_bytes, info.PeakPagefileUsage,
                                      info.PrivateUsage)
        if self.peak_bytes > self.limit:
            raise ShadowStop("memory_limit_exceeded", f"{self.peak_bytes} > {self.limit} bytes")
        if self.priority == "idle" and \
                self.kernel.GetPriorityClass(self.process) != self.IDLE_PRIORITY:
            raise ShadowStop("resource_constraint_changed", "priority class")
        if self.cpu_mask is not None:
            mask, system = ct.c_size_t(), ct.c_size_t()
            if not self.kernel.GetProcessAffinityMask(self.process, ct.byref(mask),
                                                      ct.byref(system)) \
                    or mask.value != self.cpu_mask:
                raise ShadowStop("resource_constraint_changed", "CPU affinity")

    def describe(self):
        return {"peak_memory_bytes": self.peak_bytes, "memory_limit_bytes": self.limit,
                "memory_metric": "PeakWorkingSetSize",
                "peak_private_bytes": self.peak_private_bytes,
                "numerical_threads_env": dict(self.numerical_threads_env),
                "priority": self.priority, "cpu_mask": self.cpu_mask, "notes": list(self.notes)}


class WindowSource:
    """One node's one-second scalar windows, ported from the October 25 passive collector.

    Windows advance on the node's own frame counter from the collection start. Missing
    frames, read failures, stale reads and nonfinite samples invalidate their window;
    they are counted, never patched. The row is the shape the v2 policy validates.
    """

    def __init__(self, name, entry, handle, cursor, *, stage):
        self.name, self.identity, self.handle = name, entry["node_id"], handle
        self.pid, self.epoch = int(entry["pid"]), int(entry["epoch"])
        self.label, self.position = entry["label"], int(entry["position"])
        self.sample_rate = float(entry["sample_rate"])
        self.rate = round(self.sample_rate)
        self.channels = int(entry["channels"])
        self.stage = stage
        self.cursor = int(cursor)
        self.rows = 0
        self.total_read_failures = self.total_stale_reads = 0
        self.total_gaps = self.total_nonfinite = 0
        self.last_status = None
        self.on_row = None
        self.reset_bin(self.cursor)

    def reset_bin(self, first):
        self.first = first
        self.count = self.gaps = self.read_failures = self.stale_reads = 0
        self.squares = np.zeros(self.channels)
        self.peaks = np.zeros(self.channels)
        self.nonfinite = np.zeros(self.channels, np.int64)
        self.ages = []
        self.receipt_first = self.receipt_last = None
        self.receipt_first_qpc = self.receipt_last_qpc = None
        self.callback_first = self.callback_last = None
        self.clock_flags = set()
        self.last_stale_receipt = None

    def observe_status(self, status, age, receipt, receipt_qpc):
        self.last_status = {
            "active": bool(status.active), "flags": int(status.flags),
            "host_sample": int(status.host_sample), "host_system_ns": int(status.host_system_ns),
            "callback_ns": int(status.callback_ns), "written_frame": int(status.written_frame),
            "capture_age_ms": age, "block_size": int(status.block_size),
            "first_frame": int(status.first_frame), "epoch": int(status.epoch),
            "sample_rate": float(status.sample_rate)}
        self.clock_flags.add(int(status.flags))
        if self.receipt_first is None:
            self.receipt_first, self.callback_first = receipt, int(status.callback_ns)
            self.receipt_first_qpc = receipt_qpc
        self.receipt_last, self.callback_last = receipt, int(status.callback_ns)
        self.receipt_last_qpc = receipt_qpc
        if age is not None:
            self.ages.append(age)

    def mark_stale(self, receipt):
        # One native read can span several processing chunks in this window.
        if self.last_stale_receipt != receipt:
            self.stale_reads += 1
            self.last_stale_receipt = receipt

    def read_failed(self):
        self.read_failures += 1
        self.total_read_failures += 1

    def description(self, complete):
        valid = bool(complete and self.count == self.rate and not (
            self.gaps or self.read_failures or self.stale_reads or int(self.nonfinite.sum())))
        squares, peaks = self.squares.tolist(), self.peaks.tolist()
        levels = [20 * math.log10(math.sqrt(x / self.rate)) if x > 0 else None
                  for x in squares] if valid else [None] * self.channels
        peak_db = [20 * math.log10(x) if x > 0 else None for x in peaks] if valid \
            else [None] * self.channels
        return {"source": self.name, "node_id": self.identity, "pid": self.pid, "epoch": self.epoch,
                "stage": self.stage, "sample_rate": self.rate, "channels": self.channels,
                "window_start_local_frame": self.first,
                "window_end_local_frame": self.first + self.rate,
                "complete": complete, "measurement_valid": valid, "observed_frames": self.count,
                "missing_frames": self.gaps, "nonfinite_samples": self.nonfinite.tolist(),
                "read_failures": self.read_failures, "stale_reads": self.stale_reads,
                "rms_dbfs": levels, "peak_dbfs": peak_db,
                "zero_energy_channels": [bool(valid and x == 0) for x in squares],
                "capture_age_min_ms": min(self.ages) if self.ages else None,
                "capture_age_max_ms": max(self.ages) if self.ages else None,
                "receipt_first_monotonic": self.receipt_first,
                "receipt_last_monotonic": self.receipt_last,
                "callback_first_ns": self.callback_first, "callback_last_ns": self.callback_last,
                "clock_flags_observed": sorted(self.clock_flags), "last_status": self.last_status,
                "alignment": "unknown_independent_node_counters", "routing_and_state": "unknown",
                "controller_qualified": False, "source_window_index": self.rows + 1,
                "receipt_first_qpc_seconds": self.receipt_first_qpc,
                "receipt_last_qpc_seconds": self.receipt_last_qpc,
                "receipt_clock": RECEIPT_CLOCK}

    def emit(self):
        row = self.description(True)
        self.rows += 1
        if self.on_row is not None:
            self.on_row(row)
        self.reset_bin(self.first + self.rate)

    def skip(self, missing, status, age, receipt, receipt_qpc):
        """Account dropped frames; a window completed by a gap still emits, invalid."""
        self.total_gaps += missing
        while missing:
            amount = min(missing, self.first + self.rate - self.cursor)
            self.gaps += amount
            self.cursor += amount
            missing -= amount
            if self.cursor == self.first + self.rate:
                self.observe_status(status, age, receipt, receipt_qpc)
                self.emit()

    def consume(self, block, status, age, receipt, receipt_qpc, stale):
        offset, count = 0, len(block)
        while offset < count:
            amount = min(count - offset, self.first + self.rate - self.cursor)
            self.observe_status(status, age, receipt, receipt_qpc)
            if stale:
                self.mark_stale(receipt)
            part = np.asarray(block[offset:offset + amount, :self.channels], dtype=np.float64)
            finite = np.isfinite(part)
            clean = np.where(finite, part, 0.0)
            self.squares += (clean ** 2).sum(axis=0)
            self.peaks = np.maximum(self.peaks, np.abs(clean).max(axis=0))
            bad = (~finite).sum(axis=0)
            self.nonfinite += bad
            self.total_nonfinite += int(bad.sum())
            self.count += amount
            self.cursor += amount
            offset += amount
            if self.cursor == self.first + self.rate:
                self.emit()
        if not count:
            self.observe_status(status, age, receipt, receipt_qpc)
            if stale:
                self.mark_stale(receipt)

    def totals(self):
        return {"source": self.name, "node_id": self.identity, "stage": self.stage,
                "rows": self.rows, "total_read_failures": self.total_read_failures,
                "total_stale_reads": self.total_stale_reads,
                "total_missing_frames": self.total_gaps,
                "total_nonfinite_samples": self.total_nonfinite,
                "partial_window": self.description(False)}


class BinAssembler:
    """Assemble 100 ms ``[frames, tracks, 2]`` bins from independently polled sources.

    Positions are samples on one shared grid: host samples while every source's host
    clock is valid, otherwise each source's own counter from the collection start. A bin
    is complete once every track filled it exactly; a bin left behind by MAX_LAG_BINS
    while any track moved on is a gap. Pending bins are therefore bounded.
    """

    def __init__(self, tracks, bin_frames):
        self.tracks = tuple(tracks)
        self.index = {track: i for i, track in enumerate(self.tracks)}
        self.bin_frames = int(bin_frames)
        self.bins, self.filled = {}, {}
        self.next = 0
        self.last_end = {track: None for track in self.tracks}
        self.discontinuities = []
        self.late_frames = 0

    def add(self, track, position, block):
        count = len(block)
        if not count:
            return
        column = self.index[track]
        end = self.last_end[track]
        if end is not None and position != end:
            self.discontinuities.append({"track": track, "expected_position": end,
                                         "position": position})
        self.last_end[track] = position + count
        frames = self.bin_frames
        start, stop = position, position + count
        if start < self.next * frames:
            self.late_frames += min(count, self.next * frames - start)
        for k in range(max(start // frames, self.next), (stop - 1) // frames + 1):
            low, high = max(start, k * frames), min(stop, (k + 1) * frames)
            buffer = self.bins.get(k)
            if buffer is None:
                buffer = self.bins[k] = np.zeros((frames, len(self.tracks), 2), np.float32)
                self.filled[k] = np.zeros(len(self.tracks), np.int64)
            buffer[low - k * frames:high - k * frames, column, :] = block[low - start:high - start]
            self.filled[k][column] += high - low

    def pop(self):
        """Complete bins and gaps in grid order: ``("bin", index, audio)`` or ``("gap", ...)``."""
        ready = []
        while True:
            k = self.next
            if k in self.bins and bool((self.filled[k] == self.bin_frames).all()):
                ready.append(("bin", k, self.bins.pop(k)))
                self.filled.pop(k)
                self.next += 1
                continue
            furthest = max((end for end in self.last_end.values() if end is not None), default=0)
            if furthest >= (k + MAX_LAG_BINS + 1) * self.bin_frames:
                filled = self.filled.pop(k, None)
                self.bins.pop(k, None)
                missing = [track for track in self.tracks if filled is None
                           or filled[self.index[track]] != self.bin_frames]
                ready.append(("gap", k, {"missing_tracks": missing,
                                         "filled_frames": None if filled is None
                                         else dict(zip(self.tracks, filled.tolist()))}))
                self.next += 1
                continue
            return ready


class CoordinatorShadow:
    """Calibrate on the first complete bins, then shadow MixCoordinator on every bin."""

    def __init__(self, rate, calibration_bins, grid, *, write_row, write_plan):
        self.rate, self.calibration_bins, self.grid = int(rate), int(calibration_bins), dict(grid)
        self.alignment = self.grid["alignment"]
        self.write_row, self.write_plan = write_row, write_plan
        self.levels = []
        self.coordinator = self.settings = self.plan = self.error = None
        self.calibrated = self.plan_written = False
        self.bins = self.gap_notifications = 0
        self.event_types = Counter()
        self.last_corrections = None

    def gap(self):
        if self.coordinator is not None:
            self.coordinator.notify_gap()
            self.gap_notifications += 1

    def calibrate(self):
        self.calibrated = True
        try:
            from .mix_coordinator_trial import calibration
            plan = calibration(np.asarray(self.levels))
            accepted = {field.name for field in fields(CoordinatorSettings)}
            kwargs = {key: tuple(value) if key == "enabled_support_tracks" else value
                      for key, value in plan.items() if key in accepted}
            self.settings = CoordinatorSettings(**kwargs)
            self.coordinator = MixCoordinator(self.settings, self.rate)
            self.plan = plan
        except (ImportError, ValueError, TypeError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        self.write_calibration()

    def write_calibration(self):
        if self.plan_written:
            return
        self.plan_written = True
        # The caller adds the measured host write count; only live_eligible is declared here.
        self.write_plan({"alignment": self.alignment, "grid": dict(self.grid),
                         "calibration_bins_required": self.calibration_bins,
                         "calibration_bins_observed": len(self.levels), "calibrated": self.calibrated,
                         "plan": self.plan, "error": self.error,
                         "settings": self.settings.as_dict() if self.settings else None,
                         "sample_rate": self.rate, "declared": {"live_eligible": False}})

    def bin(self, index, audio):
        power = np.mean(np.asarray(audio, dtype=np.float64) ** 2, axis=(0, 2))
        levels = 10 * np.log10(np.maximum(power, 1e-16))
        row = {"bin_index": index, "alignment": self.alignment,
               "levels_dbfs": dict(zip(TRACKS, levels.tolist())),
               "current_correction_db": None, "target_correction_db": None, "events": []}
        if not self.calibrated:
            self.levels.append(levels)
            row.update(phase="calibration", calibration_bins=len(self.levels))
            if len(self.levels) >= self.calibration_bins:
                self.calibrate()
        elif self.coordinator is None:
            row["phase"] = "uncoordinated"
        else:
            self.coordinator.process(audio)  # The output is discarded: shadow only.
            self.bins += 1
            events = list(self.coordinator.events)
            self.coordinator.events.clear()  # Keep memory bounded; the rows hold them.
            for event in events:
                self.event_types[event.get("type", "unknown")] += 1
            self.last_corrections = self.coordinator.current_correction_db
            row.update(phase="coordinated", current_correction_db=self.last_corrections,
                       target_correction_db=self.coordinator.target_correction_db, events=events)
        self.write_row(row)

    def summary(self):
        return {"alignment": self.alignment, "grid": dict(self.grid),
                "calibration_bins_required": self.calibration_bins,
                "calibration_bins_observed": len(self.levels), "calibrated": self.calibrated,
                "calibration_error": self.error, "bins": self.bins,
                "gap_notifications": self.gap_notifications,
                "event_types": dict(self.event_types),
                "final_correction_db": self.last_corrections,
                "settings": self.settings.as_dict() if self.settings else None}


class RedShadow:
    """Feed RED's 100 ms RMS (mean power over channels) to ActivityCalibration."""

    def __init__(self, bin_frames, channels, *, write_row):
        self.calibration = ActivityCalibration()
        self.bin_frames, self.channels, self.write_row = int(bin_frames), int(channels), write_row
        self.index = 0
        self.last = None
        self.reset()

    def reset(self):
        self.squares = 0.0
        self.count = self.bad = 0

    def skip(self, missing):
        while missing:
            amount = min(missing, self.bin_frames - self.count)
            self.bad += amount * self.channels
            self.count += amount
            missing -= amount
            if self.count == self.bin_frames:
                self.finish()

    def consume(self, block):
        offset, count = 0, len(block)
        while offset < count:
            amount = min(count - offset, self.bin_frames - self.count)
            part = np.asarray(block[offset:offset + amount, :self.channels], dtype=np.float64)
            finite = np.isfinite(part)
            self.bad += int((~finite).sum())
            self.squares += float((np.where(finite, part, 0.0) ** 2).sum())
            self.count += amount
            offset += amount
            if self.count == self.bin_frames:
                self.finish()

    def finish(self):
        if self.bad:
            level = math.nan  # Missing or nonfinite audio is an invalid bin, never silence.
        else:
            power = self.squares / (self.bin_frames * self.channels)
            level = 10 * math.log10(power) if power > 0 else None
        self.last = self.calibration.observe(level, bin_index=self.index)
        self.write_row(dict(self.last, invalid_samples=self.bad))
        self.index += 1
        self.reset()

    def summary(self):
        return {"bins": self.index, "ready": self.calibration.ready,
                "ready_bin_index": self.calibration.ready_bin_index,
                "gain_db": self.calibration.gain_db,
                "median_dbfs": self.last["median_dbfs"] if self.last else None,
                "last_reason": self.last["reason"] if self.last else None}


class InspectionMonitor:
    """Inspect the host about once per second, freezing the first state.

    The main loop requests inspections on its own clock; the worker only ever calls
    ``inspect()``. The transport is recorded on every tick. Everything else is
    fingerprinted, and a change is reported, never adopted.
    """

    def __init__(self, inspect, clock_ns, path, *, threaded=True):
        self.inspect, self.clock_ns, self.path, self.threaded = inspect, clock_ns, path, threaded
        self.lock = threading.Lock()
        self.wake, self.stop_event = threading.Event(), threading.Event()
        self.pending = None
        self.latest = self.baseline = self.baseline_frozen = self.baseline_hash = self.error = None
        self.calls = 0
        self.thread = threading.Thread(target=self._run, name="wcfi-home-shadow-inspection",
                                       daemon=True)

    def start(self):
        Path(self.path).touch(exist_ok=False)
        if self.threaded:
            self.thread.start()

    def request(self, request_qpc, request_monotonic):
        """Start one inspection unless one is in flight; return whether it was accepted."""
        with self.lock:
            if self.pending is not None or self.error is not None:
                return False
            self.pending = (request_qpc, request_monotonic)
        if self.threaded:
            self.wake.set()
        else:
            self._inspect_once()
        return True

    def _run(self):
        try:
            while not self.stop_event.is_set():
                if self.wake.wait(0.1):
                    self.wake.clear()
                    self._inspect_once()
        finally:
            with self.lock:
                if not self.stop_event.is_set() and self.error is None:
                    self.error = "inspection worker exited unexpectedly"

    def _inspect_once(self):
        try:
            with self.lock:
                pending = self.pending
            if pending is None:
                return
            request_qpc, request_monotonic = pending
            self.calls += 1
            snapshot = self.inspect()
            receipt_qpc = self.clock_ns() / 1e9
            if receipt_qpc < request_qpc:
                raise RuntimeError("Native receipt clock moved backwards during inspection")
            if not isinstance(snapshot, dict):
                raise TypeError("Host inspection returned no snapshot")
            frozen = frozen_state(snapshot)
            digest = fingerprint(frozen)
            transport = snapshot.get("transport") or {}
            changed = self.baseline_hash is not None and digest != self.baseline_hash
            # The paths that moved are diffed against the frozen baseline now, so a stop names
            # what changed at the inspection that saw it, not at a later final snapshot.
            paths = changed_paths(self.baseline_frozen, frozen, limit=CHANGED_PATHS_IN_STOP) \
                if changed else []
            record = {"operation": INSPECT_SESSION, "sequence": self.calls,
                      "receipt_qpc_seconds": receipt_qpc, "state_query_started_at_qpc": request_qpc,
                      "state_observed_at_qpc": receipt_qpc,
                      "state_time_interpretation": STATE_TIME_INTERPRETATION,
                      "request_monotonic": request_monotonic, "state_fingerprint": digest,
                      "state_changed": changed, "changed_paths": paths,
                      "transport": {"is_playing": bool(transport.get("is_playing")),
                                    "record_mode": bool(transport.get("record_mode"))},
                      "session_id": snapshot.get("session_id")}
            with Path(self.path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, allow_nan=False) + "\n")
            with self.lock:
                if self.baseline_hash is None:
                    self.baseline_hash, self.baseline = digest, copy.deepcopy(snapshot)
                    self.baseline_frozen = frozen
                self.latest, self.pending = record, None
        except Exception as exc:  # noqa: BLE001 - the worker records any host failure instead of dying silently.
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.pending = None

    def current(self):
        with self.lock:
            if self.error is not None:
                raise ShadowStop("inspection_failed", self.error)
            return copy.deepcopy(self.latest)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.threaded and self.thread.is_alive():
            self.thread.join(5.0)
        return not self.thread.is_alive()


def _validate(config):
    if set(config.policies) - set(POLICIES) or not config.policies:
        raise ValueError(f"policies must be a nonempty subset of {POLICIES}")
    for name in ("coordinator_stage", "v2_stage", "red_stage"):
        if getattr(config, name) not in STAGES:
            raise ValueError(f"{name} must be one of {sorted(STAGES)}")
    for name in ("seconds", "poll_interval", "transport_stop_grace_seconds", "max_seconds",
                 "fresh_wait_seconds"):
        value = getattr(config, name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    if config.max_seconds < config.seconds:
        raise ValueError("max_seconds bounds the wait for Play plus the collection and must be "
                         "at least seconds")
    if not math.isfinite(config.calibration_seconds) or config.calibration_seconds < 0:
        raise ValueError("calibration_seconds must be a nonnegative finite number")
    if type(config.memory_limit_bytes) is not int or config.memory_limit_bytes <= 0:
        raise ValueError("memory_limit_bytes must be a positive integer")


def module_hashes():
    hashes = {}
    for name in HASHED_MODULES:
        path = Path(__file__).with_name(name)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return hashes


class _HomeShadow:
    def __init__(self, config, bridge_factory, inspect, clock, sleep):
        self.config, self.bridge_factory, self.host = config, bridge_factory, HostCalls(inspect)
        self.clock, self.sleep = clock, sleep
        self.output = Path(config.output)
        self.stack, self.files = contextlib.ExitStack(), {}
        self.bridge = self.monitor = self.guard = None
        self.handles, self.nodes = {}, {}
        self.bindings, self.records = {}, {}
        self.sources = {}
        self.policy = self.coordinator = self.assembler = self.red = None
        self.pending = {}
        self.groups = self.decisions = self.proposals = 0
        self.input_reasons, self.hold_reasons, self.gap_kinds = Counter(), Counter(), Counter()
        self.gaps = self.rebinds = self.unplaceable_frames = 0
        self.last_input_status = self.state = None
        self.next_inspection = 0.0
        self.began = self.collection_start = self.collection_end = self.play_observed = None
        self.alignment = self.anchor = self.source_revision = self.rate = None
        self.lockstep_verified = False
        self.base, self.coordinator_nodes, self.v2_nodes, self.red_node = {}, {}, set(), None
        self.host_before = self.host_after = self.inspected_pid = None

    # -- bookkeeping -------------------------------------------------------------------

    def row(self, name, value):
        stream = self.files[name]
        stream.write(json.dumps(value, allow_nan=False) + "\n")
        stream.flush()

    def event(self, detail):
        self.row("events", dict(detail, at_monotonic=self.clock()))

    def gap(self, detail):
        """Count and log a gap. The coordinator hears only of incomplete bins, from drain_bins,
        in grid order: a drop on a node it does not consume never resets it."""
        self.gaps += 1
        self.gap_kinds[detail["kind"]] += 1
        self.row("gaps", dict(detail, at_monotonic=self.clock()))

    def grid(self):
        """The coordinator's bin grid: the plugin-reported continuous host sample when every
        coordinator node's host clock advanced in lockstep with its frame counter, otherwise
        each node's own counter from the collection start."""
        return {"alignment": self.alignment, "anchor": self.anchor,
                "lockstep_verified": self.lockstep_verified}

    def tick(self, now):
        """Per-poll guard, deadline and host inspection; return the latest host record."""
        self.guard.check()
        if now - self.began >= self.config.max_seconds:
            raise ShadowStop("max_seconds_reached")
        if now >= self.next_inspection and self.monitor.request(self.bridge.clock_ns() / 1e9, now):
            self.next_inspection = now + INSPECTION_INTERVAL
        state = self.monitor.current()
        if state is not None and state["state_changed"]:
            raise ShadowStop("host_state_changed",
                             ", ".join(state["changed_paths"]) or state["state_fingerprint"])
        self.state = state
        return state

    # -- setup ---------------------------------------------------------------------------

    def await_first_inspection(self):
        while self.tick(self.clock()) is None:
            self.sleep(self.config.poll_interval)
        with self.monitor.lock:
            snapshot = copy.deepcopy(self.monitor.baseline)
        record = self.state
        self.host_before = {key: record[key] for key in (
            "receipt_qpc_seconds", "state_query_started_at_qpc", "state_observed_at_qpc",
            "state_time_interpretation", "state_fingerprint", "transport")}
        self.host_before["snapshot"] = snapshot
        # The CLI's inspector adds the bridge descriptor's pid, the Live process it talks to,
        # outside the fingerprinted keys. A snapshot without it leaves the pid unknown.
        pid = snapshot.get("host_pid")
        self.inspected_pid = int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
        write_json(self.output / "host-before.json", self.host_before, allow_nan=True)

    def bind(self, *, rebind=False):
        config = self.config
        self.close_handles()
        snapshots = inventory(self.bridge)
        wanted = {"v2": (ROLES, config.v2_stage), "coordinator": (TRACKS, config.coordinator_stage),
                  "red": (("RED",), config.red_stage)}
        bindings = {key: bind_roles(snapshots, roles, stage=stage)
                    for key, (roles, stage) in wanted.items() if key in config.policies}
        entries = [entry for binding in bindings.values() for entry in binding.values()]
        if len({entry["pid"] for entry in entries}) != 1:
            raise SessionNodeError("Bound nodes span more than one Live process")
        pid = int(entries[0]["pid"])
        if self.inspected_pid is not None and pid != self.inspected_pid:
            raise SessionNodeError(f"Bound nodes belong to pid {pid} but the inspected Live is "
                                   f"pid {self.inspected_pid}")
        if len({float(entry["sample_rate"]) for entry in entries}) != 1:
            raise SessionNodeError("Bound nodes publish more than one sample rate")
        rate = round(float(entries[0]["sample_rate"]))
        if rate % 10 or not 8000 <= rate <= 192000:
            raise SessionNodeError(f"Unsupported sample rate {rate}")
        nodes = {}
        for key, binding in bindings.items():
            for role, entry in binding.items():
                node = nodes.setdefault(entry["node_id"], {"role": role, "stage": wanted[key][1],
                                                           "entry": dict(entry), "uses": []})
                if node["role"] != role or node["stage"] != wanted[key][1]:
                    raise SessionNodeError(f"Node {entry['node_id']} is bound to two roles")
                node["uses"].append(key)
        baselines = []
        for node_id, node in nodes.items():
            handle = self.bridge.open(node_id)
            if not handle:
                raise SessionNodeError(f"Bound node is unavailable: {node['role']} {node_id}")
            self.handles[node_id] = handle
            code, _, status = read_node(self.bridge, handle, 0, 0)
            receipt_ns = int(self.bridge.clock_ns())
            receipt_qpc = receipt_ns / 1e9
            if code < 0 or status is None:
                raise SessionNodeError(f"Initial metadata read failed for {node['role']}: {code}")
            age = _age_ms(receipt_ns, int(status.callback_ns))
            baselines.append({"source": node["role"], "node_id": node_id, "uses": list(node["uses"]),
                "pid": int(status.pid), "epoch": int(status.epoch),
                "sample_rate": float(status.sample_rate), "channels": int(status.channels),
                "position": int(status.position), "label": _label(status.label),
                "active": bool(status.active), "cursor": int(status.written_frame),
                "flags": int(status.flags), "host_sample": int(status.host_sample),
                "host_system_ns": int(status.host_system_ns),
                "callback_ns": int(status.callback_ns), "block_size": int(status.block_size),
                "capture_age_ms": age, "receipt_qpc_seconds": receipt_qpc,
                "receipt_monotonic": self.clock()})
            node["status"] = self.clock_status(status, age, None)
        self.bindings, self.nodes, self.rate = bindings, nodes, rate
        self.records = {key: binding_record(binding, stage=wanted[key][1], roles=list(wanted[key][0]))
                        for key, binding in bindings.items()}
        self.v2_nodes = {entry["node_id"] for entry in bindings.get("v2", {}).values()}
        self.coordinator_nodes = {entry["node_id"]: track
                                  for track, entry in bindings.get("coordinator", {}).items()}
        self.red_node = bindings["red"]["RED"]["node_id"] if "red" in bindings else None
        if "v2" in bindings:
            self.source_revision = source_map_revision(bindings["v2"])
        if rebind:
            self.rebinds += 1  # The first rebind writes binding-rebind-1.json.
        suffix = f"-rebind-{self.rebinds}" if rebind else ""
        write_json(self.output / f"binding{suffix}.json", {
            "host_pid": pid, "inspected_pid": self.inspected_pid, "sample_rate": rate,
            "rebind": rebind, "bindings": self.records,
            "nodes": {node_id: {"role": n["role"], "stage": n["stage"], "uses": n["uses"]}
                      for node_id, n in nodes.items()}})
        write_json(self.output / f"node-baseline{suffix}.json", {
            "zero_frame_reads": baselines, "host_sample_alignment": False,
            "controller_qualified": False})
        if rebind:
            self.event({"kind": "rebind", "count": self.rebinds})

    def close_handles(self):
        if self.bridge is not None:
            for handle in self.handles.values():
                self.bridge.close(handle)
        self.handles.clear()

    @staticmethod
    def clock_status(status, age, previous):
        """A node's clock fields from one metadata read, with the Stream.append lockstep rule.

        ``lockstep`` is True when this read and the previous fresh one both flag a host clock
        and ``host_sample + block_size`` advanced by exactly what ``written_frame`` did, False
        when they disagree, and None when no judgement is possible yet (no earlier fresh
        read, or no new frames since it).
        """
        fresh = bool(status.active and age is not None and 0 <= age < FRESH_MS)
        current = {"written_frame": int(status.written_frame), "flags": int(status.flags),
                   "host_sample": int(status.host_sample), "block_size": int(status.block_size),
                   "capture_age_ms": age, "fresh": fresh, "lockstep": None}
        if previous is not None and previous["fresh"] and fresh:
            advance = current["written_frame"] - previous["written_frame"]
            host_advance = (current["host_sample"] + current["block_size"]
                            - previous["host_sample"] - previous["block_size"])
            if advance > 0:
                current["lockstep"] = bool(current["flags"] & previous["flags"] & 1
                                           and host_advance == advance)
        return current

    def nodes_fresh(self):
        """Metadata-read every bound node; return (identity problem, nodes that are not fresh).

        Each node not fresh is named with its reason (``read_failed``, ``inactive`` or
        ``stale``) so the wait can report it. Every successful read also updates the node's
        clock status for the lockstep judgement.
        """
        stale = []
        for node_id, node in self.nodes.items():
            code, _, status = read_node(self.bridge, self.handles[node_id], 0, 0)
            receipt_ns = int(self.bridge.clock_ns())
            if code < 0 or status is None:
                stale.append({"source": node["role"], "node_id": node_id, "reason": "read_failed",
                              "native_code": code, "capture_age_ms": None})
                continue
            if self.identity_mismatch(node["entry"], status):
                return "identity_changed", stale
            age = _age_ms(receipt_ns, int(status.callback_ns))
            if not status.active:
                stale.append({"source": node["role"], "node_id": node_id, "reason": "inactive",
                              "capture_age_ms": age})
            elif age is None or not 0 <= age < FRESH_MS:
                stale.append({"source": node["role"], "node_id": node_id, "reason": "stale",
                              "capture_age_ms": age})
            node["status"] = self.clock_status(status, age, node.get("status"))
        return None, stale

    def lockstep_pending(self):
        """Whether a coordinator node still lacks a lockstep judgement (one more read needed)."""
        return any(self.nodes[node_id]["status"]["lockstep"] is None
                   for node_id in self.coordinator_nodes)

    @staticmethod
    def identity_mismatch(entry, status):
        actual = (int(status.pid), int(status.epoch), _label(status.label), int(status.position),
                  float(status.sample_rate), int(status.channels))
        expected = (int(entry["pid"]), int(entry["epoch"]), entry["label"], int(entry["position"]),
                    float(entry["sample_rate"]), int(entry["channels"]))
        return None if actual == expected else {"expected": expected, "observed": actual}

    def wait_for_play(self):
        """Wait until the transport plays (unless told not to) and every bound node is fresh.

        A node that stays inactive, stale or unreadable while the host plays is named in a
        ``waiting_for_fresh_nodes`` event at most once per inspection interval and, after
        ``fresh_wait_seconds``, ends the run with ``nodes_not_fresh``. A changed identity
        rebinds. When the coordinator runs, one more fresh read is taken if needed so that
        each of its nodes has a lockstep judgement before the grid is chosen.
        """
        config = self.config
        unfresh_since = last_report = None
        while True:
            now = self.clock()
            state = self.tick(now)
            playing = bool(state["transport"]["is_playing"])
            if playing and self.play_observed is None:
                self.play_observed = now
            if playing or not config.wait_for_play:
                problem, stale = self.nodes_fresh()
                if problem == "identity_changed":
                    self.event({"kind": "node_identity_changed_before_collection"})
                    self.bind(rebind=True)
                    unfresh_since = None
                elif not stale:
                    unfresh_since = None
                    if not self.lockstep_pending():
                        return
                else:
                    unfresh_since = now if unfresh_since is None else unfresh_since
                    if last_report is None or now - last_report >= INSPECTION_INTERVAL:
                        last_report = now
                        self.event({"kind": "waiting_for_fresh_nodes", "nodes": stale,
                                    "waiting_since_monotonic": unfresh_since,
                                    "transport_playing": playing})
                    if now - unfresh_since >= config.fresh_wait_seconds:
                        raise ShadowStop("nodes_not_fresh", "; ".join(
                            f"{entry['source']} {entry['reason']}" for entry in stale))
            self.sleep(config.poll_interval)

    def start_collection(self, now):
        config = self.config
        self.collection_start = now
        for node_id, node in self.nodes.items():
            source = WindowSource(node["role"], node["entry"], self.handles[node_id],
                                  node["status"]["written_frame"], stage=STAGES[node["stage"]])
            source.on_row = self.on_row
            self.sources[node_id] = source
        if "v2" in self.bindings:
            expected = {role: {"node_id": entry["node_id"], "pid": entry["pid"],
                               "epoch": entry["epoch"]}
                        for role, entry in self.bindings["v2"].items()}
            self.policy = PublicationClockGainV2(expected_sources=expected)
        if self.coordinator_nodes:
            statuses = [self.nodes[node_id]["status"] for node_id in self.coordinator_nodes]
            # The plugin flags a continuous host sample whether or not the transport plays;
            # the grid trusts it only once every coordinator node advanced it in lockstep
            # with its frame counter across two fresh reads (the Stream.append rule).
            self.lockstep_verified = all(status["lockstep"] is True for status in statuses)
            if self.lockstep_verified:
                self.alignment = "host_sample"
                self.anchor = max(s["host_sample"] + s["block_size"] for s in statuses)
            else:
                self.alignment = "approximate_publication_clock"
                self.base = {node_id: self.nodes[node_id]["status"]["written_frame"]
                             for node_id in self.coordinator_nodes}
            self.assembler = BinAssembler(TRACKS, self.rate // 10)
            self.coordinator = CoordinatorShadow(
                self.rate, round(config.calibration_seconds * 10), self.grid(),
                write_row=lambda row: self.row("bins", row),
                write_plan=lambda plan: write_json(self.output / "coordinator-calibration.json",
                                                   dict(plan, host_writes=self.host.writes())))
        if self.red_node is not None:
            self.red = RedShadow(self.rate // 10, self.nodes[self.red_node]["entry"]["channels"],
                                 write_row=lambda row: self.row("red", row))
        self.event(dict({"kind": "collection_started"}, **self.grid(),
                        play_observed_at_monotonic=self.play_observed,
                        cursors={node_id: source.cursor for node_id, source in self.sources.items()}))

    # -- collection --------------------------------------------------------------------

    def grid_position(self, source, first, status):
        if self.alignment == "host_sample":
            if not int(status.flags) & 1:
                return None
            offset = int(status.host_sample) + int(status.block_size) - int(status.written_frame)
            return first + offset - self.anchor
        return first - self.base[source.identity]

    def poll_source(self, source):
        code, block, status = read_node(self.bridge, source.handle, source.cursor)
        receipt_ns = int(self.bridge.clock_ns())
        receipt_qpc = receipt_ns / 1e9
        receipt = self.clock()
        if code < 0:
            source.read_failed()
            self.event({"kind": "read_failure", "source": source.name, "node_id": source.identity,
                        "native_code": code, "cursor": source.cursor,
                        "receipt_qpc_seconds": receipt_qpc})
            return
        count = len(block)
        if count > CAPACITY:
            raise ShadowStop("invalid_frame_count", f"{source.name}: {count}")
        mismatch = self.identity_mismatch(source_entry(source), status)
        if mismatch:
            raise ShadowStop("node_identity_changed", f"{source.name}: {mismatch!r}")
        first, written = int(status.first_frame), int(status.written_frame)
        if first < source.cursor or written < first + count:
            raise ShadowStop("non_monotonic_counter", source.name)
        age = _age_ms(receipt_ns, int(status.callback_ns))
        stale = not status.active or age is None or not 0 <= age < FRESH_MS
        if stale:
            source.total_stale_reads += 1
        missing = first - source.cursor
        if missing:
            self.gap({"kind": "dropped_frames", "source": source.name, "node_id": source.identity,
                      "cursor": source.cursor, "first_frame": first, "missing_frames": missing,
                      "receipt_qpc_seconds": receipt_qpc})
            if source.identity == self.red_node and self.red is not None:
                self.red.skip(missing)
            source.skip(missing, status, age, receipt, receipt_qpc)
        if source.identity in self.coordinator_nodes and count:
            position = self.grid_position(source, first, status)
            if position is None:
                self.unplaceable_frames += count
            else:
                self.assembler.add(self.coordinator_nodes[source.identity], position,
                                   _stereo(block, source.channels))
        if source.identity == self.red_node and self.red is not None and count:
            self.red.consume(block)
        source.consume(block, status, age, receipt, receipt_qpc, stale)

    def drain_bins(self):
        if self.assembler is None:
            return
        for kind, index, payload in self.assembler.pop():
            if kind == "bin":
                self.coordinator.bin(index, payload)
            else:
                self.gap(dict({"kind": "incomplete_bin", "bin_index": index}, **payload))
                self.coordinator.gap()  # In grid order, after the complete bins before it.
        for detail in self.assembler.discontinuities:
            self.event(dict({"kind": "grid_discontinuity", "alignment": self.alignment}, **detail))
        self.assembler.discontinuities.clear()

    def on_row(self, row):
        """Every completed one-second window is a levels row; post rows also form v2 groups."""
        self.row("levels", row)
        if row["node_id"] in self.v2_nodes:
            self.on_post_row(row)

    def on_post_row(self, row):
        index = row["source_window_index"]
        members = self.pending.setdefault(index, {})
        if row["source"] in members:
            raise ShadowStop("duplicate_source_window", f"{row['source']} {index}")
        members[row["source"]] = row
        if len(members) != len(ROLES):
            return
        del self.pending[index]
        if index != self.groups + 1:
            self.event({"kind": "group_order", "index": index, "complete_groups": self.groups})
        state = self.state
        frame = {"time_seconds": index, "sequence": index,
                 "received_at_qpc": self.bridge.clock_ns() / 1e9,
                 "state_observed_at_qpc": state["state_observed_at_qpc"],
                 "state_query_started_at_qpc": state["state_query_started_at_qpc"],
                 "inspection_received_at_qpc": state["receipt_qpc_seconds"],
                 "state_time_interpretation": state["state_time_interpretation"],
                 "source_map_revision": self.source_revision,
                 "routing_revision": self.monitor.baseline_hash,
                 "state_revision": self.monitor.baseline_hash,
                 "host_sample_alignment": False, "windows": members}
        self.row("groups", frame)
        emitted = self.policy.feed(frame)
        self.last_input_status = self.policy.input_status
        self.row("input", {"observation_group_index": index,
                           "group_received_at_qpc": frame["received_at_qpc"],
                           "input_status": self.last_input_status})
        self.input_reasons[str(self.last_input_status.get("reason") or "qualified")] += 1
        for decision in emitted:
            self.decisions += 1
            self.row("decisions", decision)
            self.hold_reasons[str(decision.get("reason", "unknown"))] += 1
            if decision.get("delta_db", 0) != 0:
                self.proposals += 1
                self.row("proposals", {"shadow_only": True, "applied": False,
                                       "group_received_at_qpc": frame["received_at_qpc"],
                                       "decision": decision})
        self.groups += 1

    def collect(self):
        config = self.config
        now = self.clock()
        self.start_collection(now)
        deadline, next_poll, stopped_since = now + config.seconds, now, None
        while True:
            now = self.clock()
            if now >= deadline:
                return "collection_seconds_elapsed"
            state = self.tick(now)
            if state["transport"]["is_playing"]:
                stopped_since = None
                if self.play_observed is None:
                    self.play_observed = now
            elif self.play_observed is not None:
                # The stop grace is armed only once Play was seen: a run that did not wait
                # for the transport collects its whole window while stopped.
                stopped_since = now if stopped_since is None else stopped_since
                if now - stopped_since > config.transport_stop_grace_seconds:
                    raise ShadowStop("transport_stopped")
            for source in list(self.sources.values()):
                self.poll_source(source)
            self.drain_bins()
            next_poll += config.poll_interval
            now = self.clock()
            if next_poll <= now:
                next_poll = now + config.poll_interval  # Never perform a catch-up burst.
            self.sleep(max(0.0, min(next_poll, deadline) - now))

    # -- lifecycle ---------------------------------------------------------------------

    def run(self):
        config = self.config
        _validate(config)
        self.began, cpu_began = self.clock(), time.process_time()
        self.output.mkdir(parents=True, exist_ok=False)
        write_json(self.output / "started.json", {
            "started_at": utc_now(),
            "config": {name: str(value) if isinstance(value, Path) else value
                       for name, value in vars(config).items()},
            "inspection_worker": INSPECTION_WORKER,
            "declared": dict(DECLARED, host_mutations=0),
            "qualification": "home_shadow_observe_only"})
        outcome, reason, detail = "failed", "setup_incomplete", None
        try:
            self.guard = ResourceGuard(config.memory_limit_bytes)
            self.files = {name: self.stack.enter_context((self.output / file).open("x", encoding="utf-8"))
                          for name, file in ROW_FILES.items()}
            self.bridge = self.bridge_factory()
            self.monitor = InspectionMonitor(self.host, self.bridge.clock_ns,
                                             self.output / "bridge-inspections.jsonl",
                                             threaded=INSPECTION_WORKER)
            self.monitor.start()
            self.await_first_inspection()
            self.bind()
            self.wait_for_play()
            reason = self.collect()
            outcome = "completed"
        except ShadowStop as exc:
            reason, detail = exc.reason, exc.detail
            if self.collection_start is None:
                outcome = "failed"  # Nothing was collected: the output holds no evidence.
            else:
                outcome = "completed" if reason == "transport_stopped" else "stopped"
        except SessionNodeError as exc:
            reason, detail = "bind_failed", str(exc)
        except KeyboardInterrupt:
            reason = "interrupted"
        except Exception as exc:  # noqa: BLE001 - the summary must record the actual failure.
            reason, detail = type(exc).__name__, str(exc)
        finally:
            if self.collection_start is not None:
                self.collection_end = self.clock()
            summary = self.finish(outcome, reason, detail, cpu_began)
        return summary

    def finish(self, outcome, reason, detail, cpu_began):
        if self.monitor is not None and not self.monitor.close():
            self.event({"kind": "inspection_worker_still_running"})
        self.close_handles()
        if self.coordinator is not None:
            self.coordinator.write_calibration()
        host_after = {"error": None, "snapshot": None, "diff": None}
        if self.monitor is not None:
            try:
                snapshot = self.host()
                receipt_qpc = self.bridge.clock_ns() / 1e9
                before = self.host_before["snapshot"] if self.host_before else None
                host_after.update(snapshot=snapshot, receipt_qpc_seconds=receipt_qpc,
                                  state_fingerprint=fingerprint(frozen_state(snapshot)),
                                  transport=snapshot.get("transport"))
                if before is not None:
                    paths = changed_paths(frozen_state(before), frozen_state(snapshot))
                    host_after["diff"] = {
                        "state_changed": host_after["state_fingerprint"] != self.host_before[
                            "state_fingerprint"],
                        "changed_paths": paths, "changed_count": len(paths),
                        "transport_before": self.host_before["transport"],
                        "transport_after": snapshot.get("transport")}
            except Exception as exc:  # noqa: BLE001 - a failed final inspection is recorded, never fatal.
                host_after["error"] = f"{type(exc).__name__}: {exc}"
            try:
                write_json(self.output / "host-after.json", host_after, allow_nan=True)
            except OSError as exc:
                host_after["error"] = f"{type(exc).__name__}: {exc}"
        self.stack.close()
        # Every host call went through self.host and was counted by operation name.
        host_calls = dict(self.host.counts)
        now = self.clock()
        summary = {
            "finished_at": utc_now(), "outcome": outcome, "reason": reason, "detail": detail,
            "stop_reason": reason, "wall_seconds": now - self.began,
            "cpu_seconds": time.process_time() - cpu_began,
            "collection_wall_seconds": (self.collection_end - self.collection_start
                                        if self.collection_start is not None else 0.0),
            "collection": {"started_at_seconds": self.collection_start,
                           "ended_at_seconds": self.collection_end,
                           "play_observed_at_seconds": self.play_observed,
                           "seconds_requested": self.config.seconds,
                           "max_seconds": self.config.max_seconds,
                           "grid": self.grid(), "sample_rate": self.rate},
            "host_pid": next(iter(self.records.values()))["host_pid"] if self.records else None,
            "inspected_pid": self.inspected_pid,
            "bindings": self.records, "rebinds": self.rebinds,
            "scalar_rows": sum(source.rows for source in self.sources.values()),
            "complete_groups": self.groups, "incomplete_group_indices": sorted(self.pending),
            "controller_evaluations": self.decisions, "proposed_moves": self.proposals,
            "decision_reason_counts": dict(self.hold_reasons),
            "input_reason_counts": dict(self.input_reasons),
            "last_input_status": self.last_input_status,
            "final_virtual_offset_db": self.policy.offset_db if self.policy is not None else None,
            "coordinator": self.coordinator.summary() if self.coordinator is not None else None,
            "coordinator_bins": self.coordinator.bins if self.coordinator is not None else 0,
            "gaps": self.gaps, "gap_kinds": dict(self.gap_kinds),
            "unplaceable_frames": self.unplaceable_frames,
            "late_frames": self.assembler.late_frames if self.assembler is not None else 0,
            "read_failures": sum(s.total_read_failures for s in self.sources.values()),
            "stale_reads": sum(s.total_stale_reads for s in self.sources.values()),
            "missing_frames": sum(s.total_gaps for s in self.sources.values()),
            "red": self.red.summary() if self.red is not None else None,
            "red_lock": ({"ready": self.red.calibration.ready,
                          "ready_bin_index": self.red.calibration.ready_bin_index,
                          "gain_db": self.red.calibration.gain_db}
                         if self.red is not None else None),
            "reason_counts": {"stop": {reason: 1}, "input_status": dict(self.input_reasons),
                              "decisions": dict(self.hold_reasons), "gaps": dict(self.gap_kinds)},
            "inspections": host_calls.get(INSPECT_SESSION, 0), "host_calls": host_calls,
            "writes": self.host.writes(), "host_mutations": self.host.writes(),
            "declared": dict(DECLARED),
            # The run's qualification statement, as the October 25 summaries carry it: no
            # window fed to V2 was host-sample aligned. The coordinator's grid is reported
            # under collection.grid and coordinator.grid.
            "host_sample_alignment": False,
            "state_fingerprint": self.monitor.baseline_hash if self.monitor is not None else None,
            "host_after": {key: host_after.get(key) for key in ("error", "diff")},
            "resources": self.guard.describe() if self.guard is not None else None,
            "sha256": module_hashes(),
            "files": sorted(path.name for path in self.output.iterdir()) + ["summary.json"],
            "sources": [source.totals() for source in self.sources.values()]}
        write_json(self.output / "summary.json", summary)
        return summary


def source_entry(source):
    return {"pid": source.pid, "epoch": source.epoch, "label": source.label,
            "position": source.position, "sample_rate": source.sample_rate,
            "channels": source.channels}


def run_home_shadow(config, *, bridge_factory, inspect, clock=time.monotonic, sleep=time.sleep):
    """Run the observe-only shadow; return the summary also written to ``output/summary.json``.

    ``bridge_factory()`` returns a NodeBridge-like object. ``inspect(operation)`` performs
    that bridge operation; the shadow asks only for ``inspect_session`` (its default), whose
    snapshot may carry the inspected Live's pid as ``host_pid``, and it counts every call by
    name. Nothing here issues Play, writes a parameter or fader, or stores PCM. The output
    directory must not exist yet.
    """
    return _HomeShadow(config, bridge_factory, inspect, clock, sleep).run()
