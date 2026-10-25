"""Discover and position ordinary application windows on the visible desktop."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from functools import lru_cache

from .capture import _libraries, top_level_hwnd

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class AppWindow:
    hwnd: int
    pid: int
    title: str = field(compare=False)
    minimized: bool = field(default=False, compare=False)

    @property
    def key(self) -> tuple[int, int]:
        return self.hwnd, self.pid


@lru_cache(maxsize=1)
def _api():
    user32, _ = _libraries()
    signatures = {
        "IsWindow": ([wintypes.HWND], wintypes.BOOL),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "IsIconic": ([wintypes.HWND], wintypes.BOOL),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                                     wintypes.DWORD),
        "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_int16),
        "GetLastActivePopup": ([wintypes.HWND], wintypes.HWND),
        "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        "MonitorFromWindow": ([wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
        "GetShellWindow": ([], wintypes.HWND),
        "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
        "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
        "SetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "SetLayeredWindowAttributes": ([wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte,
                                         wintypes.DWORD], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(user32, name)
        function.argtypes, function.restype = args, result
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwm.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    return user32, dwm


def _pid(user32, hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def idle_pointer(viewer_hwnd: int):
    """Read an unredirected pointer and actual topmost hit, only while idle."""
    user32, _ = _api()
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise RuntimeError("Windows could not read the pointer position.")
    hit = user32.WindowFromPoint(point)
    over_viewer = bool(hit and top_level_hwnd(hit) == top_level_hwnd(viewer_hwnd))
    held = any(user32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 5, 6))
    return (point.x, point.y), over_viewer, held


def enumerate_windows() -> list[AppWindow]:
    """List titled, visible top-level windows, excluding our process and cloaked apps."""
    user32, dwm = _api()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    own_pid, shell = os.getpid(), user32.GetShellWindow()
    windows = []

    @callback_type
    def collect(hwnd, _data):
        if hwnd == shell or not user32.IsWindowVisible(hwnd):
            return True
        pid = _pid(user32, hwnd)
        if not pid or pid == own_pid:
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value.strip():
                windows.append(AppWindow(int(hwnd), pid, title.value.strip(),
                                         bool(user32.IsIconic(hwnd))))
        return True

    if not user32.EnumWindows(collect, 0):
        raise RuntimeError("Windows could not list application windows.")
    return sorted(windows, key=lambda item: (item.title.casefold(), item.hwnd))


def _validate(window: AppWindow):
    user32, dwm = _api()
    if not user32.IsWindow(window.hwnd) or _pid(user32, window.hwnd) != window.pid:
        raise RuntimeError("This application window has closed. Refresh and select another window.")
    if not user32.IsWindowVisible(window.hwnd):
        raise RuntimeError("This application window is hidden. Show it before controlling it.")
    cloaked = wintypes.DWORD()
    dwm.DwmGetWindowAttribute(window.hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    if cloaked.value:
        raise RuntimeError("This window is on another desktop. Switch to its desktop first.")
    return user32, dwm


def window_rect(window: AppWindow) -> Rect:
    user32, dwm = _validate(window)
    if user32.IsIconic(window.hwnd):
        raise RuntimeError("Window minimized. Choose Recover selected to restore it.")
    rect = wintypes.RECT()
    # Visible frame bounds omit invisible resize borders around ordinary windows.
    if (dwm.DwmGetWindowAttribute(window.hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
            and not user32.GetWindowRect(window.hwnd, ctypes.byref(rect))):
        raise RuntimeError("Windows could not read this application's position.")
    result = rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    if min(result[2:]) < 2:
        raise RuntimeError("This application window has no visible area.")
    return result


def restore_window(window: AppWindow) -> None:
    """Restore only on an explicit recovery request; preserve the app's layout."""
    user32, _ = _validate(window)
    if user32.IsIconic(window.hwnd):
        user32.ShowWindow(window.hwnd, 9)  # SW_RESTORE
    activate_window(window)


def activate_window(window: AppWindow) -> None:
    user32, _ = _validate(window)
    if user32.IsIconic(window.hwnd):
        raise RuntimeError("Restore the application window before controlling it.")
    from .window_scene import owned_by

    # A modal dialog may disable the selected root. Resume its active owned
    # popup, so releasing control does not force a different picker selection.
    hwnd, visited = window.hwnd, set()
    while hwnd not in visited:
        visited.add(hwnd)
        popup = user32.GetLastActivePopup(hwnd)
        if (not popup or popup in visited or not user32.IsWindowVisible(popup)
                or not owned_by(user32, popup, window.hwnd)):
            break
        hwnd = popup
    if (user32.GetForegroundWindow() != hwnd
            and not user32.SetForegroundWindow(hwnd)):
        raise RuntimeError("Windows could not activate the application. Select it and try again.")


class ViewerPassThrough:
    """Temporarily let native redirected input reach the app behind our own viewer."""

    def __init__(self, hwnd: int):
        self._child_hwnd = hwnd
        self.hwnd = None
        self._original = None
        self._pinned = False

    def enable(self):
        if self._original is not None:
            return
        self.hwnd = top_level_hwnd(self._child_hwnd)
        user32, _ = _api()
        self._original = user32.GetWindowLongPtrW(self.hwnd, -20)  # GWL_EXSTYLE
        try:
            self._set_style(self._original | 0x80000 | 0x20)  # LAYERED | TRANSPARENT
            if not user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, 2):
                raise RuntimeError("Windows could not prepare the viewer for window control.")
        except BaseException:
            self.disable()
            raise

    def raise_viewer(self):
        if self._original is None:
            self.hwnd = top_level_hwnd(self._child_hwnd)
        user32, _ = _api()
        # Explicit recovery raises a normal window. Only redirected interaction
        # needs TOPMOST so activating the target cannot obscure its own preview.
        active = self._original is not None
        # Used only by explicit Recover/Move actions, never idle refresh or
        # release. A normal window needs activation to rise above the target.
        if not active and not user32.SetForegroundWindow(self.hwnd):
            raise RuntimeError("Windows could not bring the viewer forward.")
        if not user32.SetWindowPos(self.hwnd, -1 if active else 0, 0, 0, 0, 0, 0x0013):
            raise RuntimeError("Windows could not keep the viewer above the application.")
        self._pinned = active

    def _set_style(self, style):
        user32, _ = _api()
        ctypes.set_last_error(0)
        previous = user32.SetWindowLongPtrW(self.hwnd, -20, style)
        if previous == 0 and ctypes.get_last_error():
            raise RuntimeError("Windows could not change the viewer's input mode.")

    def disable(self):
        if self._original is not None:
            self._set_style(self._original)
            self._original = None
        if self._pinned:
            user32, _ = _api()
            if not user32.SetWindowPos(self.hwnd, -2, 0, 0, 0, 0, 0x0013):
                raise RuntimeError("Windows could not restore normal viewer stacking.")
            self._pinned = False


def side_by_side(work: Rect) -> tuple[Rect, Rect]:
    """Leave room for viewer chrome and a gap, including on negative-origin displays."""
    x, y, width, height = work
    if width < 1000 or height < 540:
        raise ValueError("This display is too small for automatic side-by-side layout.")
    viewer_width = max(540, (width - 48) // 2)
    return ((x + 12, y + 12, viewer_width, height - 24),
            (x + viewer_width + 36, y + 12, width - viewer_width - 48, height - 24))


def arrange_window(window: AppWindow, viewer_hwnd: int) -> None:
    """Explicit user action: restore and arrange just the viewer and selected app."""
    from .capture import _MonitorInfo

    user32, _ = _validate(window)
    viewer_hwnd = top_level_hwnd(viewer_hwnd)
    monitor = user32.MonitorFromWindow(viewer_hwnd, 2)
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise RuntimeError("Windows could not read the display's usable area.")
    area = info.rcWork
    viewer, target = side_by_side((area.left, area.top,
                                   area.right - area.left, area.bottom - area.top))
    for hwnd, bounds in ((window.hwnd, target), (viewer_hwnd, viewer)):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if not user32.SetWindowPos(hwnd, None, *bounds, 0x0014):  # NOZORDER | NOACTIVATE
            raise RuntimeError("Windows could not arrange these windows. Move them apart manually.")
    activate_window(window)
