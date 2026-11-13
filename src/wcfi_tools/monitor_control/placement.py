"""Explicit window placement, independent of capture or offscreen scene bounds."""

import ctypes
from ctypes import wintypes

from .capture import _MonitorInfo, enumerate_monitors, top_level_hwnd
from .windows import AppWindow, Rect, _api, _validate, window_rect


def fit_window(bounds: Rect, work: Rect) -> Rect:
    """Keep size when possible, shrink oversized windows, and expose their title bar."""
    x, y, width, height = bounds
    left, top, aw, ah = work
    if min(width, height, aw, ah) <= 0:
        raise ValueError("Window and display dimensions must be positive.")
    width, height = min(width, aw), min(height, ah)
    return (max(left, min(x, left + aw - width)),
            max(top, min(y, top + ah - height)), width, height)


def destination_work_area(viewer_hwnd: int, device: str | None = None) -> Rect:
    """Resolve a live device identity, or the screen containing the viewer."""
    user, _ = _api()
    if device is None:
        monitor = user.MonitorFromWindow(top_level_hwnd(viewer_hwnd), 2)
    else:
        screen = next((m for m in enumerate_monitors() if device in (m.key, m.device)), None)
        if screen is None:
            raise RuntimeError("That display was disconnected. Choose a connected display.")
        user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user.MonitorFromPoint.restype = wintypes.HANDLE
        monitor = user.MonitorFromPoint(
            wintypes.POINT(screen.left + screen.width // 2, screen.top + screen.height // 2), 0,
        )
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise RuntimeError("The destination display is unavailable. Refresh and try again.")
    if device is not None and info.szDevice != screen.device:
        raise RuntimeError("Display configuration changed. Choose the destination again.")
    r = info.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _outer_rect(user, window):
    _validate(window)
    r = wintypes.RECT()
    if not user.GetWindowRect(window.hwnd, ctypes.byref(r)):
        raise RuntimeError("Windows could not read the application's position.")
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def keep_viewer_visible(viewer_hwnd, monitors):
    """Recover only our own stranded viewer; never restore a user-minimized viewer."""
    from .geometry import intersection

    if not monitors:
        return
    user, _ = _api()
    hwnd = top_level_hwnd(viewer_hwnd)
    if user.IsIconic(hwnd):
        return
    rect = wintypes.RECT()
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("Windows could not locate the viewer after the display change.")
    bounds = rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    title = (*bounds[:2], bounds[2], min(32, bounds[3]))
    for m in monitors:
        visible = intersection(title, (m.left, m.top, m.width, m.height))
        if visible and visible[2] >= min(80, bounds[2]) and visible[3] >= 20:
            return
    destination = fit_window(bounds, destination_work_area(hwnd))
    if not user.SetWindowPos(hwnd, None, *destination, 0x0014):
        raise RuntimeError("Windows could not return the viewer to a connected display.")


def move_window_to_display(window: AppWindow, viewer_hwnd: int, device: str | None = None):
    """Restore and relocate the selected root and its visible owned dialogs only."""
    from .window_scene import discover_scene, owned_by

    # Resolve before restoring/moving: a stale menu choice must have no side effects.
    work = destination_work_area(viewer_hwnd, device)
    user, _ = _validate(window)
    user.IsZoomed.argtypes, user.IsZoomed.restype = [wintypes.HWND], wintypes.BOOL
    maximized = bool(user.IsZoomed(window.hwnd))
    if user.IsIconic(window.hwnd) or maximized:
        user.ShowWindow(window.hwnd, 9)  # SW_RESTORE: recover its ordinary size first.
    original = _outer_rect(user, window)
    scene = discover_scene(window, window_rect(window))
    dialogs = []
    for layer in scene.layers[1:]:
        if owned_by(user, layer.window.hwnd, window.hwnd):
            dialogs.append((layer.window, _outer_rect(user, layer.window)))
    destination = fit_window(original, work)
    dx, dy = destination[0] - original[0], destination[1] - original[1]

    def place(item, bounds):
        _validate(item)
        if not user.SetWindowPos(item.hwnd, None, *fit_window(bounds, work), 0x0014):
            raise RuntimeError("Windows could not move this window. It may require higher privileges.")
        # Apps may enforce a minimum size or resize during a DPI transition. Keep
        # the top-left reachable even if their constraints exceed the work area.
        actual = _outer_rect(user, item)
        fitted = fit_window(actual, work)
        if (actual[:2] != fitted[:2]
                and not user.SetWindowPos(item.hwnd, None, *fitted, 0x0015)):  # also NOSIZE
            raise RuntimeError("Windows could not keep the moved window on the display.")

    place(window, destination)
    for dialog, (x, y, width, height) in dialogs:
        try:
            place(dialog, (x + dx, y + dy, width, height))
        except RuntimeError:
            if user.IsWindow(dialog.hwnd):
                raise
            # Transient dialogs can close in response to their parent's movement.
    if maximized:
        user.ShowWindow(window.hwnd, 3)  # SW_MAXIMIZE on the destination display.
