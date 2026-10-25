"""Preview deadlines and a scoped Windows timer-resolution request."""

from __future__ import annotations

import ctypes
import math
import sys


class FramePacer:
    """Keep a steady cadence without adding a fixed sleep after every capture."""

    def __init__(self, fps: int):
        self.period = 1 / fps
        self.deadline: float | None = None

    def delay_ms(self, started: float, finished: float) -> int:
        if self.deadline is None or started - self.deadline > self.period:
            self.deadline = started + self.period
        else:
            self.deadline += self.period
        # If capture takes the whole budget, yield to Tk instead of queueing
        # missed frames or scheduling a burst of catch-up work.
        self.deadline = max(self.deadline, finished)
        # Ignore sub-nanosecond float noise at an exact millisecond boundary.
        return max(1, math.ceil((self.deadline - finished) * 1000 - 1e-6))


class PreviewTimer:
    """Request 1 ms timer granularity only while the preview is visible.

    Windows' default timer granularity can turn a short Tk wait into ~16 ms.
    Always pair successful timeBeginPeriod calls with timeEndPeriod, including
    on minimize/close. Failure simply leaves the default system timing in use.
    """

    def __init__(self):
        self.active = False
        self._api = None
        self._unavailable = False

    def set_active(self, active: bool):
        if not active:
            self.close()
            return
        if self.active or self._unavailable:
            return
        if self._api is None:
            if sys.platform != "win32":
                self._unavailable = True
                return
            try:
                self._api = ctypes.WinDLL("winmm")
                for name in ("timeBeginPeriod", "timeEndPeriod"):
                    function = getattr(self._api, name)
                    function.argtypes = [ctypes.c_uint]
                    function.restype = ctypes.c_uint
            except (OSError, AttributeError):
                self._unavailable = True
                return
        self.active = self._api.timeBeginPeriod(1) == 0
        self._unavailable = not self.active

    def close(self):
        if self.active:
            self._api.timeEndPeriod(1)
            self.active = False
