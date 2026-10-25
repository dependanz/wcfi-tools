"""Capture a selected window and its owned popups as one desktop-coordinate scene."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from .geometry import contains, intersection
from .windows import AppWindow, Rect, _api, _pid, window_rect


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD)] + [
        (name, wintypes.HWND) for name in (
            "hwndActive", "hwndFocus", "hwndCapture", "hwndMenuOwner",
            "hwndMoveSize", "hwndCaret",
        )
    ] + [("rcCaret", wintypes.RECT)]


@dataclass(frozen=True)
class WindowLayer:
    window: AppWindow
    bounds: Rect


@dataclass(frozen=True)
class WindowScene:
    # Back to front, in native Z order. The selected root is always first.
    layers: tuple[WindowLayer, ...]
    # An empty tuple preserves unrestricted geometry for older/internal callers.
    # The viewer always supplies the physical rectangles of connected displays.
    displays: tuple[Rect, ...] = ()

    def visible_regions(self, layer: WindowLayer) -> tuple[Rect, ...]:
        if not self.displays:
            return (layer.bounds,)
        return tuple(dict.fromkeys(
            region for display in self.displays
            if (region := intersection(layer.bounds, display)) is not None
        ))

    @property
    def bounds(self) -> Rect:
        regions = [region for layer in self.layers for region in self.visible_regions(layer)]
        if not regions:
            raise RuntimeError("This application is offscreen. Move it onto a connected display, "
                               "or choose Bring to laptop / Arrange side by side.")
        left = min(region[0] for region in regions)
        top = min(region[1] for region in regions)
        right = max(region[0] + region[2] for region in regions)
        bottom = max(region[1] + region[3] for region in regions)
        return left, top, right - left, bottom - top


def owned_by(user32, hwnd: int, root: int) -> bool:
    """Walk owners, never accept unrelated windows merely sharing a process."""
    seen = set()
    hwnd = user32.GetAncestor(hwnd, 2) if hwnd else 0  # GA_ROOT
    while hwnd and hwnd not in seen:
        if hwnd == root:
            return True
        seen.add(hwnd)
        hwnd = user32.GetWindow(hwnd, 4)  # GW_OWNER
    return False


def belongs_to_window(user32, hwnd: int, root: int) -> bool:
    if owned_by(user32, hwnd, root):
        return True
    # Native menu HWNDs have no owner. Tie them to the GUI thread's live menu
    # owner, not just the process (two independent windows can share a thread).
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, name, len(name))
    if name.value != "#32768":
        return False
    thread = user32.GetWindowThreadProcessId(hwnd, None)
    info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    if not (user32.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.flags & 0x1C):
        return False  # GUI_INMENUMODE | SYSTEMMENUMODE | POPUPMENUMODE
    if owned_by(user32, info.hwndMenuOwner, root):
        return True
    # Toolkits such as Tk route TrackPopupMenu through a hidden helper HWND.
    # Require the same GUI thread's active window to belong to the selection.
    return bool(info.hwndMenuOwner and not user32.IsWindowVisible(info.hwndMenuOwner)
                and _pid(user32, info.hwndMenuOwner) == _pid(user32, root)
                and owned_by(user32, info.hwndActive, root))


def discover_scene(window: AppWindow, bounds: Rect) -> WindowScene:
    user32, _ = _api()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    layers = []

    @callback_type
    def collect(hwnd, _data):
        if (hwnd != window.hwnd and user32.IsWindowVisible(hwnd)
                and not user32.IsIconic(hwnd) and belongs_to_window(user32, hwnd, window.hwnd)):
            item = AppWindow(int(hwnd), _pid(user32, hwnd), "")
            try:
                layers.append(WindowLayer(item, window_rect(item)))
            except RuntimeError:
                pass  # A transient menu can disappear during enumeration.
        return True

    if not user32.EnumWindows(collect, 0):
        raise RuntimeError("Windows could not list this application's menus and dialogs.")
    if len(layers) > 32:
        raise RuntimeError("Too many application popups to capture safely.")
    return WindowScene((WindowLayer(window, bounds), *reversed(layers)))


class WindowGuard:
    """Only inject into the window geometry in the last presented scene.

    None means keep control but suppress input: a popup is awaiting its first
    frame, or the preview pointer is in a blank gap between owned windows.
    False means release because focus left the selected window's family.
    """

    def __init__(self, window: AppWindow, scene: WindowScene):
        self.window = window
        self.presented: WindowScene | None = scene
        self.reason = ""

    def __call__(self, point: tuple[int, int]) -> bool | None:
        user32, _ = _api()
        if (not user32.IsWindow(self.window.hwnd)
                or _pid(user32, self.window.hwnd) != self.window.pid):
            self.reason = "Selected window closed. Refresh and select its new window."
            return False
        if not belongs_to_window(user32, user32.GetForegroundWindow(), self.window.hwnd):
            self.reason = "Focus left this app. Move out and back into the preview to resume."
            return False
        scene = self.presented
        if scene is None:
            self.reason = "Input paused · Waiting for updated window or dialog pixels."
            return None
        if scene.displays and not any(contains(display, point) for display in scene.displays):
            self.reason = "Input paused · This area is between displays or offscreen."
            return None
        hit = user32.WindowFromPoint(wintypes.POINT(*point))
        hit = user32.GetAncestor(hit, 2) if hit else 0
        for layer in reversed(scene.layers):
            if layer.window.hwnd == hit and layer.window.pid == _pid(user32, hit):
                try:
                    if window_rect(layer.window) == layer.bounds:
                        self.reason = ""
                        return True
                except RuntimeError:
                    pass
                self.reason = "Input paused · Window position changed; waiting for its preview."
                return None
        self.reason = "Input paused · Pointer is over a gap, an uncaptured popup, or another window."
        return None
