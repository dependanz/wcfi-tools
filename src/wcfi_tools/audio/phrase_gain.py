"""Causal, offline phrase-level gain candidate; no audio or host I/O.

Inputs are completed, non-overlapping raw RMS bins. A returned gain is for the
following audio, never the bin that supplied its evidence. Activity is an energy
rule for a dedicated source, not a speech or musical-intent classifier.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math


POLICY_VERSION = "lapel-phrase-gain-v1"
_EPSILON = 1e-12


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class PhraseSettings:
    bin_seconds: float = .1
    history_bins: int = 20
    min_active_bins: int = 5
    expire_inactive_bins: int = 10
    trim_db: float = 18.
    target_dbfs: float = -26.
    max_boost_db: float = 6.
    max_cut_db: float = 6.
    activity_dbfs: float = -55.
    slew_db_per_second: float = 1.
    deadband_db: float = 1.

    def __post_init__(self):
        bounds = {
            "bin_seconds": (.001, 1.), "trim_db": (-24., 24.),
            "target_dbfs": (-36., -12.), "max_boost_db": (0., 24.),
            "max_cut_db": (0., 24.), "activity_dbfs": (-90., -24.),
            "slew_db_per_second": (.001, 6.), "deadband_db": (0., 12.),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if not _number(value) or not _finite(value) or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
        for name in ("history_bins", "min_active_bins", "expire_inactive_bins"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError(f"Invalid {name}")
        if self.min_active_bins > self.history_bins:
            raise ValueError("min_active_bins exceeds history_bins")


class PhraseGain:
    """Small virtual controller with a monotonic, zero-based bin clock.

    An invalid or missing bin discards evidence but retains the current offset.
    The first observed bin after a forward gap is discarded too. Normal evidence
    acquisition then requires fresh support; no offset reset, implicit baseline
    change, or replay of an earlier bin is performed. Returned dictionaries do
    not alias internal state.
    """

    def __init__(self, settings=PhraseSettings()):
        if not isinstance(settings, PhraseSettings):
            raise TypeError("Expected validated PhraseSettings")
        self._settings = settings
        self._history = deque(maxlen=settings.history_bins)
        self._last_index = -1
        self._inactive_bins = 0
        self._offset_db = 0.

    @property
    def settings(self):
        return self._settings

    @property
    def offset_db(self):
        return self._offset_db

    def observe(self, raw_rms_dbfs: float | None, *, bin_index: int) -> dict:
        """Observe one completed bin and return the gain for following audio.

        ``None`` denotes digital silence. Nonfinite numeric input and temporal
        discontinuity yield explicit HOLD rows. Invalid types and negative bin
        indexes raise before any state mutation.
        """
        if type(bin_index) is not int:
            raise TypeError("bin_index must be an integer")
        if bin_index < 0:
            raise ValueError("bin_index must be nonnegative")
        if raw_rms_dbfs is not None and not _number(raw_rms_dbfs):
            raise TypeError("raw_rms_dbfs must be numeric or None")
        settings = self._settings
        # Prevent unrepresentable timestamps without mutating the chronology.
        try:
            end_seconds = (bin_index + 1) * settings.bin_seconds
        except OverflowError as exc:
            raise ValueError("bin_index cannot be represented as a timestamp") from exc
        if not math.isfinite(end_seconds):
            raise ValueError("bin_index cannot be represented as a timestamp")
        before = self._offset_db
        finite = raw_rms_dbfs is None or _finite(raw_rms_dbfs)
        contiguous = bin_index == self._last_index + 1
        self._last_index = max(self._last_index, bin_index)
        active = False
        estimate = desired = predicted = None
        valid = finite and contiguous

        if not valid:
            self._history.clear()
            self._inactive_bins = 0
            reason = "invalid_sequence" if not contiguous else "invalid_nonfinite"
        else:
            active = raw_rms_dbfs is not None and raw_rms_dbfs >= settings.activity_dbfs
            if raw_rms_dbfs is not None:
                predicted = raw_rms_dbfs + settings.trim_db + before
            self._history.append(float(raw_rms_dbfs) if active else None)
            self._inactive_bins = 0 if active else self._inactive_bins + 1
            if self._inactive_bins >= settings.expire_inactive_bins:
                self._history.clear()
                reason = "hold_history_expired"
            elif not active:
                reason = "hold_inactive"
            else:
                supported = sorted(value for value in self._history if value is not None)
                if len(supported) < settings.min_active_bins:
                    reason = "hold_insufficient_support"
                else:
                    middle = len(supported) // 2
                    estimate = (supported[middle] if len(supported) % 2 else
                                supported[middle - 1] / 2. + supported[middle] / 2.)
                    lower = max(-settings.max_cut_db, -24. - settings.trim_db)
                    upper = min(settings.max_boost_db, 24. - settings.trim_db)
                    desired = max(lower, min(upper, settings.target_dbfs - estimate - settings.trim_db))
                    error = desired - before
                    if abs(error) <= settings.deadband_db + _EPSILON:
                        reason = "hold_deadband"
                    elif ((error > 0 and predicted >= settings.target_dbfs - settings.deadband_db - _EPSILON)
                          or (error < 0 and predicted <= settings.target_dbfs + settings.deadband_db + _EPSILON)):
                        reason = "hold_recent_disagreement"
                    else:
                        maximum_step = settings.slew_db_per_second * settings.bin_seconds
                        step = max(-maximum_step, min(maximum_step, error))
                        self._offset_db = max(lower, min(upper, before + step))
                        reason = "adjust_boost" if step > 0 else "adjust_cut"

        return {
            "policy_version": POLICY_VERSION,
            "bin_index": bin_index,
            "end_seconds": end_seconds,
            "raw_rms_dbfs": float(raw_rms_dbfs) if raw_rms_dbfs is not None and finite else None,
            "valid": valid,
            "active": active,
            "support_bins": sum(value is not None for value in self._history),
            "history_bins": len(self._history),
            "inactive_bins": self._inactive_bins,
            "estimate_dbfs": estimate,
            "current_predicted_dbfs": predicted,
            "desired_offset_db": desired,
            "reason": reason,
            "before_offset_db": before,
            "after_offset_db": self._offset_db,
            "delta_db": self._offset_db - before,
            "next_total_gain_db": settings.trim_db + self._offset_db,
        }
