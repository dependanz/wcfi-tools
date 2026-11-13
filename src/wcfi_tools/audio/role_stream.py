"""Bounded rolling pre/post windows, shared by node capture and recorded replay."""
from __future__ import annotations

import copy
import threading
import time

import numpy as np

from .role_analysis import analyze_pair, revision
from .roles import get_profile


class SampleHistory:
    def __init__(self, capacity, channels):
        self.data = np.empty((capacity, channels), np.float32)
        self.start = self.end = None

    def clear(self):
        self.start = self.end = None

    def append(self, audio, start):
        audio = np.asarray(audio, np.float32)
        if audio.ndim != 2 or audio.shape[1] != self.data.shape[1] or not np.isfinite(audio).all():
            raise ValueError("Invalid stream block")
        if type(start) is not int or start < 0:
            raise ValueError("A nonnegative shared sample position is required")
        if not len(audio):
            return False
        gap = self.end is not None and start != self.end
        if gap:
            self.clear()
        end = start + len(audio)
        count = min(len(audio), len(self.data))
        audio = audio[-count:]
        first = end - count
        index = first % len(self.data)
        split = min(count, len(self.data) - index)
        self.data[index:index + split] = audio[:split]
        self.data[:count - split] = audio[split:]
        self.start = max(first if self.start is None else self.start, end - len(self.data))
        self.end = end
        return gap

    def read(self, start, end):
        if self.start is None or start < self.start or end > self.end or start >= end:
            raise ValueError("Requested audio is outside the contiguous history")
        count = end - start
        index = start % len(self.data)
        split = min(count, len(self.data) - index)
        return np.concatenate((self.data[index:index + split], self.data[:count - split])).copy()


class RoleAnalysisStream:
    """Capture can continue while a copied window is analysed on another thread.

    Positions must be a common host sample clock, not unrelated per-node counts.
    Gaps, node epochs and observed chain changes invalidate accumulated history.
    This object never changes a plug-in, fader or routing.
    """
    def __init__(self, profile, rate, channels, *, seconds=10, interval=5):
        if (type(rate) is not int or not 32000 <= rate <= 96000 or channels not in (1, 2)
                or not .5 <= seconds <= 30 or not .1 <= interval <= 30):
            raise ValueError("Invalid stream format, window or interval")
        self.profile = get_profile(profile)
        self.rate, self.frames, self.step = rate, round(seconds * rate), round(interval * rate)
        self.history = {stage: SampleHistory(self.frames + rate, channels) for stage in ("pre_fx", "post_fx")}
        self.epochs = {}
        self.chain_state = None
        self.chain_revision = None
        self.last_end = None
        self.resets = 0
        self.last_reset_reason = None
        self.lock = threading.RLock()

    def _reset(self, reason):
        for ring in self.history.values():
            ring.clear()
        self.last_end = None
        self.resets += 1
        self.last_reset_reason = reason

    def set_chain_state(self, state):
        fingerprint = revision(state)
        with self.lock:
            if fingerprint != self.chain_revision:
                self._reset("chain_state_changed")
                self.chain_state, self.chain_revision = copy.deepcopy(state), fingerprint

    def push(self, stage, audio, start_frame, *, epoch=0, valid=True):
        if stage not in self.history:
            raise ValueError("Choose pre_fx or post_fx")
        with self.lock:
            if not valid:
                self._reset("invalid_clock_or_audio")
                return
            if stage in self.epochs and self.epochs[stage] != epoch:
                self._reset("node_epoch_changed")
            self.epochs[stage] = epoch
            ring = self.history[stage]
            if ring.end is not None and start_frame != ring.end:
                self._reset("observation_gap")
            try:
                ring.append(audio, start_frame)
            except ValueError:
                self._reset("invalid_audio")
                raise

    def window(self):
        with self.lock:
            a, b = self.history.values()
            if a.end is None or b.end is None:
                return None
            end = min(a.end, b.end)
            start = end - self.frames
            if start < max(a.start, b.start) or (self.last_end is not None and end - self.last_end < self.step):
                return None
            self.last_end = end
            return {"before": a.read(start, end), "after": b.read(start, end),
                    "start_frame": start, "end_frame": end, "chain_state": copy.deepcopy(self.chain_state),
                    "chain_revision": self.chain_revision, "resets": self.resets,
                    "last_reset_reason": self.last_reset_reason,
                    "copied_at_monotonic": time.monotonic()}

    def analyze(self, window, *, aligned=False, context=None, estimate_alignment=False):
        report = analyze_pair(window["before"], window["after"], self.rate, self.profile,
                              chain_state=window["chain_state"], aligned=aligned, context=context,
                              estimate_alignment=estimate_alignment)
        report["stream"] = {k: window[k] for k in ("start_frame", "end_frame", "resets", "last_reset_reason")}
        # Reject a completed-but-stale proposal after concurrent manual changes.
        with self.lock:
            current = (self.chain_revision == window["chain_revision"]
                       and self.resets == window["resets"])
        report["state_current_at_completion"] = current
        if not current:
            report["proposals"] = []
        return report
