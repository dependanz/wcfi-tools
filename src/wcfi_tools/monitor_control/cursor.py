"""Temporary cursor suppression owned by the input worker's Windows thread.

Attached input queues share the sum of their cursor visibility counters. Only
our thread's counter is changed; detaching removes its contribution, including
when Windows tears down the thread. No system cursor images/settings are changed.
"""

from __future__ import annotations

import ctypes


class ScopedCursor:
    def __init__(self, user32, kernel32):
        self.user32, self.kernel32 = user32, kernel32
        self.attached = None
        self.decrements = 0

    def hide_at(self, point):
        from .input import _MSG, _POINT, _windows_error

        # During a drag/menu loop the capturing window owns the cursor even if
        # its coordinates cross a different application's window.
        hwnd = self.user32.GetCapture() if self.attached is not None else None
        hwnd = hwnd or self.user32.WindowFromPoint(_POINT(*point))
        target = self.user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        if not target:
            raise RuntimeError("Windows could not identify the pointer's target window.")
        owner = self.kernel32.GetCurrentThreadId()
        if target != self.attached:
            self.restore()
            # Ensure the routing worker has a message queue before attaching.
            self.user32.PeekMessageW(ctypes.byref(_MSG()), None, 0, 0, 0)
            if target != owner:
                if not self.user32.AttachThreadInput(owner, target, True):
                    raise _windows_error("Windows could not hide the target pointer.")
                self.attached = target
            # Bound the work even if another application has an unusual counter.
            for _ in range(32):
                count = self.user32.ShowCursor(False)
                self.decrements += 1
                if count < 0:
                    break
            else:
                raise RuntimeError("The target application kept its pointer visible.")

    def restore(self):
        from .input import _windows_error

        # Undo exactly OUR calls, not the target application's visibility state.
        while self.decrements:
            self.user32.ShowCursor(True)
            self.decrements -= 1
        if self.attached is not None:
            target = self.attached
            # A destroyed target thread has already detached automatically.
            if not self.user32.AttachThreadInput(
                self.kernel32.GetCurrentThreadId(), target, False,
            ) and ctypes.get_last_error() not in (87, 1444):
                raise _windows_error("Windows could not detach pointer visibility.")
            self.attached = None
