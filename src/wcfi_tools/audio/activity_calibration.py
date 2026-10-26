"""Causal, one-time fixed-gain calibration from completed energy observations.

This computes a suggestion only: no audio, host or parameter writes. Energy
activity is not verified speech. A result may affect only following samples;
applying it safely, including gain slew and peak protection, is the caller's job.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math


CALIBRATION_VERSION = "activity-calibration-v1"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class CalibrationSettings:
    bin_seconds: float = .1
    window_bins: int = 300
    required_active_bins: int = 50
    activity_dbfs: float = -55.
    target_dbfs: float = -26.
    max_boost_db: float = 6.
    max_cut_db: float = 6.

    def __post_init__(self):
        for name, (low, high) in {
            "bin_seconds": (.001, 1.), "activity_dbfs": (-90., -24.),
            "target_dbfs": (-36., -12.), "max_boost_db": (0., 24.),
            "max_cut_db": (0., 24.),
        }.items():
            value = getattr(self, name)
            if not _number(value) or not _finite(value) or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
        for name in ("window_bins", "required_active_bins"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError(f"Invalid {name}")
        if self.required_active_bins > self.window_bins:
            raise ValueError("required_active_bins exceeds window_bins")


class ActivityCalibration:
    """Wait for a quorum in a bounded window, then freeze its median and gain.

    Before readiness, invalid or missing observations clear support and discard
    that bin. Silence uses a slot in the rolling window but supplies no evidence.
    After readiness, every result preserves the calibration, even for invalid
    observations; validity still describes the current input and monotonic clock.
    ``support_bins`` then describes the frozen calibration evidence, not new data.
    """

    def __init__(self, settings=CalibrationSettings()):
        if not isinstance(settings, CalibrationSettings):
            raise TypeError("Expected validated CalibrationSettings")
        self._settings = settings
        self._history = deque(maxlen=settings.window_bins)
        self._last_index = -1
        self._ready_bin_index = None
        self._median_dbfs = self._gain_db = None

    @property
    def settings(self):
        return self._settings

    @property
    def ready(self):
        return self._ready_bin_index is not None

    @property
    def ready_bin_index(self):
        return self._ready_bin_index

    @property
    def gain_db(self):
        return self._gain_db

    def observe(self, raw_rms_dbfs: float | None, *, bin_index: int) -> dict:
        """Consume one completed bin, with zero-based consecutive bin indexes.

        ``None`` represents digital silence. Type/clock-range errors raise before
        mutation. Nonfinite levels or sequence discontinuities return invalid
        records instead of silently adding evidence or changing a frozen result.
        """
        if type(bin_index) is not int:
            raise TypeError("bin_index must be an integer")
        if bin_index < 0:
            raise ValueError("bin_index must be nonnegative")
        if raw_rms_dbfs is not None and not _number(raw_rms_dbfs):
            raise TypeError("raw_rms_dbfs must be numeric or None")
        settings = self._settings
        try:
            end_seconds = (bin_index + 1) * settings.bin_seconds
        except OverflowError as exc:
            raise ValueError("bin_index cannot be represented as a timestamp") from exc
        if not math.isfinite(end_seconds):
            raise ValueError("bin_index cannot be represented as a timestamp")
        finite = raw_rms_dbfs is None or _finite(raw_rms_dbfs)
        contiguous = bin_index == self._last_index + 1
        self._last_index = max(self._last_index, bin_index)
        valid = finite and contiguous
        active = valid and raw_rms_dbfs is not None and raw_rms_dbfs >= settings.activity_dbfs
        newly_ready = False
        if not valid:
            if not self.ready:
                self._history.clear()
            reason = "invalid_sequence" if not contiguous else "invalid_nonfinite"
        elif self.ready:
            reason = "hold_ready"
        else:
            self._history.append(float(raw_rms_dbfs) if active else None)
            evidence = sorted(value for value in self._history if value is not None)
            if len(evidence) >= settings.required_active_bins:
                middle = len(evidence) // 2
                self._median_dbfs = (evidence[middle] if len(evidence) % 2 else
                                     evidence[middle - 1] / 2. + evidence[middle] / 2.)
                self._gain_db = max(-settings.max_cut_db, min(settings.max_boost_db,
                    settings.target_dbfs - self._median_dbfs))
                self._ready_bin_index = bin_index
                newly_ready = True
                reason = "ready"
            else:
                reason = "waiting_support" if active else "waiting_activity"
        return {
            "calibration_version": CALIBRATION_VERSION,
            "bin_index": bin_index,
            "end_seconds": end_seconds,
            "raw_rms_dbfs": float(raw_rms_dbfs) if raw_rms_dbfs is not None and finite else None,
            "valid": valid,
            "active": active,
            "ready": self.ready,
            "newly_ready": newly_ready,
            "reason": reason,
            "support_bins": sum(value is not None for value in self._history),
            "window_bins": len(self._history),
            "ready_bin_index": self._ready_bin_index,
            "ready_end_seconds": ((self._ready_bin_index + 1) * settings.bin_seconds
                                  if self.ready else None),
            "median_dbfs": self._median_dbfs,
            "gain_db": self._gain_db,
        }
