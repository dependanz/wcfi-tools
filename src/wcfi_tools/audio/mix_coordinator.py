"""Offline, gain-only coordination around an explicitly supplied starting mix.

This module has no device, host, filesystem, or network access. Energy evidence
does not identify singing, speech, bleed, or musical intent. The reference mix
and thresholds are experiment inputs, not universal musical targets.

Automatic changes only attenuate supporting tracks, by at most 3 dB, relative
to their supplied baseline. RED is the fixed lead reference. Completed 100 ms
bins qualify relative drift; a bin can only affect subsequent audio. By default,
30 qualified bins within the last 50 bins support a median estimate. These are
three seconds of observations, not three seconds of continuously positive drift.
Pauses, invalid observations, and abrupt level changes cancel unfinished ramps.
The dry summed peak veto permits or rejects recovery; it is NOT a limiter or a
clipping guarantee (even attenuation can increase a sum through cancellation).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

import numpy as np


TRACKS = ("RED", "YELLOW", "GREEN", "EGUITAR", "AC GUITAR", "KEYS", "KIT")
SUPPORT_TRACKS = TRACKS[1:]
EXCLUDED_TRACKS = ("LAPEL", "MDMIC Talkback", "BASS")


def _mapping(value, keys, name, lower, upper):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{name} must contain exactly {', '.join(keys)}")
    result = {key: float(value[key]) for key in keys}
    if any(not math.isfinite(x) or not lower <= x <= upper for x in result.values()):
        raise ValueError(f"{name} values must be finite in [{lower}, {upper}]")
    return MappingProxyType(result)


@dataclass(frozen=True)
class CoordinatorSettings:
    baseline_gains_db: Mapping[str, float]
    reference_support_to_lead_db: Mapping[str, float]
    activity_thresholds_dbfs: Mapping[str, float] | None = None
    enabled_support_tracks: tuple[str, ...] = SUPPORT_TRACKS
    max_cut_db: float = 3.0
    slew_db_per_second: float = 0.5
    evidence_seconds: float = 3.0
    evidence_window_seconds: float = 5.0
    unsupported_reset_seconds: float = 1.0
    drift_deadband_db: float = 2.0
    recovery_hysteresis_db: float = 0.5
    accent_step_db: float = 6.0
    master_peak_ceiling_dbfs: float = -3.0

    def __post_init__(self):
        object.__setattr__(self, "baseline_gains_db", _mapping(
            self.baseline_gains_db, TRACKS, "baseline_gains_db", -60, 24))
        object.__setattr__(self, "reference_support_to_lead_db", _mapping(
            self.reference_support_to_lead_db, SUPPORT_TRACKS,
            "reference_support_to_lead_db", -80, 40))
        thresholds = self.activity_thresholds_dbfs
        if thresholds is None:
            thresholds = {track: -50.0 if track == "RED" else -55.0 for track in TRACKS}
        object.__setattr__(self, "activity_thresholds_dbfs", _mapping(
            thresholds, TRACKS, "activity_thresholds_dbfs", -120, 0))
        enabled = tuple(self.enabled_support_tracks)
        if len(enabled) != len(set(enabled)) or any(t not in SUPPORT_TRACKS for t in enabled):
            raise ValueError("enabled_support_tracks must be a unique subset of supporting tracks")
        object.__setattr__(self, "enabled_support_tracks", enabled)
        ranges = {"max_cut_db": (0, 3), "slew_db_per_second": (0.01, 1),
                  "evidence_seconds": (1, 30), "drift_deadband_db": (0.1, 12),
                  "evidence_window_seconds": (1, 60), "unsupported_reset_seconds": (0.1, 5),
                  "recovery_hysteresis_db": (0, 6), "accent_step_db": (0.1, 30),
                  "master_peak_ceiling_dbfs": (-30, 0)}
        for name, (lower, upper) in ranges.items():
            value = getattr(self, name)
            if not isinstance(value, (float, int)) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{name} must be finite in [{lower}, {upper}]")
        if self.recovery_hysteresis_db >= self.drift_deadband_db:
            raise ValueError("recovery_hysteresis_db must be less than drift_deadband_db")
        for name in ("evidence_seconds", "evidence_window_seconds", "unsupported_reset_seconds"):
            value = getattr(self, name)
            if abs(value * 10 - round(value * 10)) > 1e-9:
                raise ValueError(f"{name} must be a multiple of 0.1")
        if self.evidence_window_seconds < self.evidence_seconds:
            raise ValueError("evidence_window_seconds must cover evidence_seconds")

    def as_dict(self):
        """Independent JSON-safe snapshot for an offline experiment manifest."""
        return {name: dict(value) if isinstance(value, Mapping) else
                list(value) if isinstance(value, tuple) else value
                for name, value in vars(self).items()}


class MixCoordinator:
    """Process [frames, seven TRACKS in order, mono/stereo channels] PCM.

    Baseline gains apply from the first sample. Reference ratios and activity
    thresholds describe raw inputs AFTER these fixed baseline gains, before
    coordinator corrections. Automatic targets are absolute corrections, never
    accumulated commands. All gain motion is smoothed at the configured slew.

    set_manual(track, dB) owns that channel with a bounded +/-3 dB correction.
    set_manual(track, None) freezes its applied gain and clears evidence. Support
    channels requalify before automatic movement. RED has no automatic policy,
    so a released manual RED correction remains held; explicitly set it to zero
    to return RED to baseline. notify_gap() also freezes and clears evidence.
    """
    live_eligible = False

    def __init__(self, settings: CoordinatorSettings, sample_rate: int):
        if not isinstance(settings, CoordinatorSettings):
            raise TypeError("settings must be CoordinatorSettings")
        if (not isinstance(sample_rate, (int, np.integer)) or isinstance(sample_rate, bool)
                or not 8000 <= sample_rate <= 192000 or sample_rate % 10):
            raise ValueError("sample_rate must be an integer divisible by 10 in [8000, 192000]")
        self.settings = settings
        self.sample_rate = int(sample_rate)
        self.bin_frames = self.sample_rate // 10
        self.required_bins = round(settings.evidence_seconds * 10)
        self.window_bins = round(settings.evidence_window_seconds * 10)
        self.unsupported_reset_bins = round(settings.unsupported_reset_seconds * 10)
        self._baseline = np.array([settings.baseline_gains_db[t] for t in TRACKS], dtype=np.float64)
        self._thresholds = np.array([settings.activity_thresholds_dbfs[t] for t in TRACKS])
        self._reference = np.array([0.] + [settings.reference_support_to_lead_db[t] for t in SUPPORT_TRACKS])
        self._current = np.zeros(len(TRACKS), dtype=np.float64)
        self._target = self._current.copy()
        self._ramp_start = self._current.copy()
        self._ramp_origin = self._current.copy()
        self._manual = {track: None for track in TRACKS}
        self._history = [deque(maxlen=self.window_bins) for _ in TRACKS]
        self._unsupported = [0 for _ in TRACKS]
        self._completed_bins = 0
        self._skip_bin = [False for _ in TRACKS]
        self._previous_levels = None
        self._buffer = None
        self._channels = None
        self._position = 0
        self._bin_invalid = False
        self._bin_peak = 0.0
        self.frames_processed = 0
        self.invalid_samples = 0
        self.events: list[dict] = []

    @property
    def current_correction_db(self):
        return dict(zip(TRACKS, self._current.tolist()))

    @property
    def target_correction_db(self):
        return dict(zip(TRACKS, self._target.tolist()))

    def _clear(self, index=None):
        for i in range(len(TRACKS)) if index is None else (index,):
            self._history[i].clear()
            self._unsupported[i] = 0

    def _anchor(self, index=None):
        for i in range(len(TRACKS)) if index is None else (index,):
            self._ramp_start[i] = self._current[i]
            self._ramp_origin[i] = self._position

    def set_manual(self, track: str, correction_db: float | None):
        if track not in TRACKS:
            raise ValueError("manual track is not in the allowed coordinator tracks")
        i = TRACKS.index(track)
        if correction_db is not None:
            correction_db = float(correction_db)
            if not math.isfinite(correction_db) or not -3 <= correction_db <= 3:
                raise ValueError("manual correction must be finite in [-3, 3] dB")
        self._manual[track] = correction_db
        self._target[i] = self._current[i] if correction_db is None else correction_db
        self._clear(i)
        self._skip_bin[i] = self._position > 0
        self._anchor(i)
        self.events.append({"type": "manual", "frame": self.frames_processed,
                            "track": track, "requested_db": correction_db,
                            "applied_db": float(self._current[i]),
                            "target_db": float(self._target[i])})

    def notify_gap(self):
        """Discard partial measurement; freeze gain until fresh evidence qualifies."""
        self._target[:] = self._current
        self._clear()
        self._skip_bin = [False for _ in TRACKS]
        self._previous_levels = None
        self._position = 0
        self._bin_invalid = False
        self._bin_peak = 0.0
        self._anchor()
        self.events.append({"type": "gap", "frame": self.frames_processed,
                            "applied_db": self.current_correction_db})

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples)
        if (samples.ndim != 3 or samples.shape[1] != len(TRACKS)
                or samples.shape[2] not in (1, 2) or samples.dtype.kind not in "fi"):
            raise ValueError("samples must be numeric [frames, 7 allowed tracks, 1 or 2 channels]")
        if self._channels is None:
            self._channels = samples.shape[2]
            self._buffer = np.empty((self.bin_frames, len(TRACKS), self._channels), dtype=np.float64)
        if samples.shape[2] != self._channels:
            raise ValueError("channel count cannot change within a coordinator stream")
        out = np.empty(samples.shape, dtype=np.float64)
        cursor = 0
        while cursor < len(samples):
            count = min(len(samples) - cursor, self.bin_frames - self._position)
            block = np.asarray(samples[cursor:cursor + count], dtype=np.float64)
            # A bound prevents absurd finite values from overflowing measurement;
            # ordinary overs are retained. Invalid samples never enter the output.
            bad = ~np.isfinite(block) | (np.abs(block) > 1e6)
            if np.any(bad):
                self._bin_invalid = True
                self.invalid_samples += int(np.count_nonzero(bad))
                block = np.where(bad, 0.0, block)
            self._buffer[self._position:self._position + count] = block
            steps = np.arange(self._position + 1, self._position + count + 1, dtype=np.float64)[:, None] - self._ramp_origin
            difference = self._target - self._ramp_start
            motion = np.minimum(np.abs(difference), steps * (self.settings.slew_db_per_second / self.sample_rate))
            corrections = self._ramp_start + np.sign(difference) * motion
            result = block * np.power(10.0, (self._baseline + corrections)[:, :, None] / 20.0)
            out[cursor:cursor + count] = result
            self._current[:] = corrections[-1]
            self._bin_peak = max(self._bin_peak, float(np.max(np.abs(np.sum(result, axis=1)))))
            self._position += count
            self.frames_processed += count
            cursor += count
            if self._position == self.bin_frames:
                self._finish_bin()
                self._position = 0
                self._bin_invalid = False
                self._bin_peak = 0.0
                self._anchor()
        return out

    def _finish_bin(self):
        self._completed_bins += 1
        for history in self._history:
            while history and history[0][0] <= self._completed_bins - self.window_bins:
                history.popleft()
        levels = 10 * np.log10(np.maximum(np.mean(self._buffer ** 2, axis=(0, 2)), 1e-16)) + self._baseline
        peak_db = 20 * math.log10(max(self._bin_peak, 1e-8))
        errors = levels - levels[0] - self._reference
        reasons = ["lead_fixed"] + ["hold_deadband"] * len(SUPPORT_TRACKS)
        medians = [None for _ in TRACKS]
        if self._bin_invalid:
            self._target[:] = self._current
            self._clear()
            self._previous_levels = None
            reasons = ["hold_invalid"] * len(TRACKS)
        else:
            lead_active = levels[0] >= self._thresholds[0]
            for i, track in enumerate(TRACKS):
                manual = self._manual[track]
                if manual is not None:
                    self._target[i] = manual
                    reasons[i] = "manual"
                    self._clear(i)
                    continue
                # Holds cancel unfinished ramps, not merely future commands.
                self._target[i] = self._current[i]
                if i == 0:
                    continue
                reason = None
                if track not in self.settings.enabled_support_tracks:
                    reason = "hold_disabled"
                elif self._skip_bin[i]:
                    reason = "hold_manual_requalify"
                elif not lead_active:
                    reason = "hold_lead_inactive"
                elif levels[i] < self._thresholds[i]:
                    reason = "hold_support_inactive"
                elif (self._previous_levels is not None and
                      max(abs(levels[0] - self._previous_levels[0]),
                          abs(levels[i] - self._previous_levels[i])) > self.settings.accent_step_db):
                    reason = "hold_accent"
                if reason:
                    reasons[i] = reason
                    if reason in ("hold_disabled", "hold_manual_requalify"):
                        self._clear(i)
                    else:
                        unsupported = min(self.unsupported_reset_bins, self._unsupported[i] + 1)
                        if unsupported >= self.unsupported_reset_bins:
                            self._clear(i)
                        self._unsupported[i] = unsupported
                    continue
                error = float(errors[i])
                self._unsupported[i] = 0
                # Keep every qualified ratio, including baseline/negative ratios.
                # Selecting only positive-error bins would bias the estimate.
                self._history[i].append((self._completed_bins, error))
                median_error = float(np.median([value for _, value in self._history[i]]))
                medians[i] = median_error
                if len(self._history[i]) < self.required_bins:
                    reasons[i] = "collect_evidence"
                    continue
                recovery_edge = self.settings.drift_deadband_db - self.settings.recovery_hysteresis_db
                if median_error > self.settings.drift_deadband_db:
                    if error <= self.settings.drift_deadband_db:
                        reasons[i] = "hold_current_cut_veto"
                        continue
                    desired = -min(self.settings.max_cut_db,
                                   max(0., min(median_error, error) - self.settings.drift_deadband_db))
                    # Stale high history cannot deepen a cut beyond the current
                    # observed excess, nor turn attenuation into recovery.
                    self._target[i] = min(self._current[i], desired)
                    reasons[i] = "attenuate" if desired < self._current[i] - 1e-12 else "hold_cut"
                elif abs(self._current[i]) > 1e-12 and median_error <= recovery_edge:
                    if error > recovery_edge:
                        reasons[i] = "hold_current_recovery_veto"
                    elif peak_db > self.settings.master_peak_ceiling_dbfs and self._current[i] < 0:
                        reasons[i] = "hold_master_headroom"
                    else:
                        self._target[i] = 0.0
                        reasons[i] = "recover"
            self._previous_levels = levels.copy()
        self._skip_bin = [False for _ in TRACKS]
        self.events.append({
            "type": "bin", "frame": self.frames_processed,
            "time_seconds": self.frames_processed / self.sample_rate,
            "live_eligible": False, "invalid": self._bin_invalid,
            "dry_sum_peak_dbfs": peak_db,
            "levels_after_baseline_dbfs": dict(zip(TRACKS, levels.tolist())),
            "raw_relative_error_db": dict(zip(SUPPORT_TRACKS, errors[1:].tolist())),
            "rolling_median_error_db": dict(zip(SUPPORT_TRACKS, medians[1:])),
            "current_correction_db": self.current_correction_db,
            "target_correction_db": self.target_correction_db,
            "evidence_bins": dict(zip(TRACKS, [len(x) for x in self._history])),
            "consecutive_unsupported_bins": dict(zip(TRACKS, self._unsupported)),
            "reasons": dict(zip(TRACKS, reasons)),
        })
