"""Local Windows pointer redirection for the monitor-control prototype.

The real cursor is on the target display while the UI draws a preview cursor.
No keyboard keys are synthesized: after a target click, ordinary Windows focus
delivers typing and shortcuts. All native coordinates must be physical pixels.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .raw_input import CRD_INPUT_MARKER

Rect = tuple[int, int, int, int]
Point = tuple[int, int]

WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_HOTKEY = 0x0312
HOTKEY_ID = 0x5743
INPUT_MARKER = 0x57434649
HEARTBEAT_TIMEOUT = 2.0
RAW_INPUT_TIMEOUT = 0.75
HANDOFF_TIMEOUT = 0.25

# message: (SendInput flag, matching release flag, pressed)
BUTTONS = {
    0x0201: (0x0002, 0x0004, True),
    0x0202: (0x0004, 0x0004, False),
    0x0204: (0x0008, 0x0010, True),
    0x0205: (0x0010, 0x0010, False),
    0x0207: (0x0020, 0x0040, True),
    0x0208: (0x0040, 0x0040, False),
    0x020B: (0x0080, 0x0100, True),
    0x020C: (0x0100, 0x0100, False),
}


def validate_rect(rect: Rect) -> Rect:
    if len(rect) != 4 or any(not isinstance(value, int) for value in rect):
        raise ValueError("Display bounds must contain four integer pixel coordinates.")
    if rect[2] < 2 or rect[3] < 2:
        raise ValueError("The target and preview must each be at least 2 × 2 pixels.")
    return rect


def preview_to_target(point: Point, preview: Rect, target: Rect) -> Point:
    """Map a preview-local point into the target, preserving both endpoints."""
    x, y = point
    x = min(max(x, 0), preview[2] - 1)
    y = min(max(y, 0), preview[3] - 1)
    return (
        target[0] + round(x * (target[2] - 1) / (preview[2] - 1)),
        target[1] + round(y * (target[3] - 1) / (preview[3] - 1)),
    )


def normalize_absolute(point: Point, desktop: Rect) -> Point:
    """Convert signed desktop pixels to SendInput's absolute virtual desktop units."""
    return tuple(
        round(min(max(point[axis] - desktop[axis], 0), desktop[axis + 2] - 1)
              * 65535 / max(desktop[axis + 2] - 1, 1))
        for axis in (0, 1)
    )


def preview_exit(point: Point, next_point: Point, preview: Rect) -> Point:
    """Return just outside the first image edge crossed by a movement segment.

    Raw/remote packets can overshoot by hundreds of pixels. Their endpoint is
    not the place where the visible preview pointer left the image. Intersect
    the path with the image bounds, then step one pixel outside on that side.
    """
    crossings = []
    for axis in (0, 1):
        end, size = next_point[axis], preview[axis + 2]
        if end < 0 or end >= size:
            edge = 0 if end < 0 else size
            travel = next_point[axis] - point[axis]
            crossings.append(((edge - point[axis]) / travel, axis, edge))
    if not crossings:
        raise ValueError("The pointer must cross a preview edge.")
    fraction, axis, edge = min(crossings)
    local = [min(max(round(point[i] + fraction * (next_point[i] - point[i])), 0),
                 preview[i + 2] - 1) for i in (0, 1)]
    local[axis] = -1 if edge == 0 else edge
    return preview[0] + local[0], preview[1] + local[1]


# Explicit 32-bit Win32 scalar widths also make these layouts importable on Unix.
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_uint32), ("value", _INPUTUNION)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT), ("mouseData", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32), ("pt", _POINT), ("lPrivate", ctypes.c_uint32),
    ]


def _windows_error(message: str) -> RuntimeError:
    """Preserve the native failure before cleanup makes another Win32 call."""
    code = ctypes.get_last_error()
    if code:
        message = f"{message} (Windows error {code}: {ctypes.FormatError(code).strip()})"
    return RuntimeError(message)


class _Win32:
    """Thin injectable adapter. Construction never installs a hook or moves input."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Monitor control input is available only on Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.hook = None
        self.callback = None
        self.hotkey = False
        self.raw_receiver = None
        self._pump_thread: threading.Thread | None = None
        self._pump_stop = threading.Event()
        self._pump_ready = threading.Event()
        self._pump_error: Exception | None = None
        self._release_requested = threading.Event()
        self.hook_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
        )
        signatures = {
            "GetCursorPos": ([ctypes.POINTER(_POINT)], ctypes.c_int),
            "SetCursorPos": ([ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "WindowFromPoint": ([_POINT], ctypes.c_void_p),
            "GetCapture": ([], ctypes.c_void_p),
            "GetWindowThreadProcessId": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
                                          ctypes.c_uint32),
            "AttachThreadInput": ([ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int], ctypes.c_int),
            "ShowCursor": ([ctypes.c_int], ctypes.c_int),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_int16),
            "RegisterHotKey": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                                ctypes.c_uint], ctypes.c_int),
            "UnregisterHotKey": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "SetWindowsHookExW": ([ctypes.c_int, self.hook_type, ctypes.c_void_p,
                                   ctypes.c_uint32], ctypes.c_void_p),
            "UnhookWindowsHookEx": ([ctypes.c_void_p], ctypes.c_int),
            "CallNextHookEx": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t,
                                ctypes.c_ssize_t], ctypes.c_ssize_t),
            "PeekMessageW": ([ctypes.POINTER(_MSG), ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
            "TranslateMessage": ([ctypes.POINTER(_MSG)], ctypes.c_int),
            "DispatchMessageW": ([ctypes.POINTER(_MSG)], ctypes.c_ssize_t),
            "MsgWaitForMultipleObjectsEx": (
                [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                 ctypes.c_uint32, ctypes.c_uint32], ctypes.c_uint32,
            ),
            "SendInput": ([ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int],
                          ctypes.c_uint),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result
        self.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        from .cursor import ScopedCursor

        self.cursor = ScopedCursor(self.user32, self.kernel32)

    def hide_cursor(self, point):
        self.cursor.hide_at(point)

    def show_cursor(self):
        self.cursor.restore()

    def position(self) -> Point:
        point = _POINT()
        ctypes.set_last_error(0)
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise _windows_error("Windows could not read the desktop cursor.")
        return point.x, point.y

    def desktop(self) -> Rect:
        return tuple(self.user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))

    def primary(self) -> Rect:
        return 0, 0, self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1)

    def buttons_down(self) -> bool:
        return any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 5, 6))

    def install(self, on_event, on_error, on_motion):
        # SendInput may block on another hook delivery. The hook-owning thread
        # must keep pumping messages while the routing worker injects input.
        if self._pump_thread is not None and self._pump_thread.is_alive():
            raise RuntimeError("The previous pointer hook is still shutting down.")
        self._pump_stop.clear()
        self._pump_ready.clear()
        self._release_requested.clear()
        self._pump_error = None

        def pump():
            try:
                self._install_on_thread(on_event, on_error, on_motion)
                self._pump_ready.set()
                while not self._pump_stop.is_set():
                    if self._pump_messages():
                        self._release_requested.set()
                    result = self.user32.MsgWaitForMultipleObjectsEx(
                        0, None, 8, 0x04FF, 0x0004,  # QS_ALLINPUT, MWMO_INPUTAVAILABLE
                    )
                    if result == 0xFFFFFFFF:
                        raise _windows_error("Windows could not wait for pointer input.")
            except Exception as exc:  # noqa: BLE001 - surface native worker failures
                self._pump_error = exc
                on_error(str(exc))
            finally:
                self._uninstall_on_thread()
                self._pump_ready.set()

        self._pump_thread = threading.Thread(target=pump, name="wcfi-hook", daemon=True)
        self._pump_thread.start()
        if not self._pump_ready.wait(.75):
            self._pump_stop.set()
            raise RuntimeError("The Windows pointer hook did not start in time.")
        if self._pump_error is not None:
            raise self._pump_error

    def _install_on_thread(self, on_event, on_error, on_motion):
        from .raw_input import RawMouseReceiver

        ctypes.set_last_error(0)
        if not self.user32.RegisterHotKey(None, HOTKEY_ID, 0x4003, ord("Q")):
            raise _windows_error(
                "Windows could not reserve Ctrl+Alt+Q. Another app may already use that shortcut."
            )
        self.hotkey = True
        try:
            self.raw_receiver = RawMouseReceiver(on_motion, on_error)
            self.raw_receiver.start()
        except Exception:
            self._uninstall_on_thread()
            raise

        def callback(code, message, address):
            try:
                if code >= 0:
                    data = ctypes.cast(address, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    # Ignore only OUR injection. Accessibility devices can inject normal input.
                    if data.dwExtraInfo != INPUT_MARKER and on_event(
                        message, (data.pt.x, data.pt.y), data.mouseData
                    ):
                        return 1
            except BaseException as exc:  # noqa: BLE001 - the C callback must always fail open
                # Native callbacks must fail open; exceptions must never cross the C boundary.
                on_error(str(exc))
            return self.user32.CallNextHookEx(self.hook, code, message, address)

        self.callback = self.hook_type(callback)
        ctypes.set_last_error(0)
        self.hook = self.user32.SetWindowsHookExW(
            14, self.callback, self.kernel32.GetModuleHandleW(None), 0
        )
        if not self.hook:
            error = _windows_error("Windows could not install the pointer hook.")
            self._uninstall_on_thread()
            raise error

    def uninstall(self):
        self._pump_stop.set()
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=.5)
            if self._pump_thread.is_alive():
                raise RuntimeError("The Windows pointer hook has not finished shutting down.")
            self._pump_thread = None

    def _uninstall_on_thread(self):
        if self.hook:
            self.user32.UnhookWindowsHookEx(self.hook)
            self.hook = None
        if self.hotkey:
            self.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.hotkey = False
        if self.raw_receiver is not None:
            self.raw_receiver.close()
            self.raw_receiver = None
        # Keep the callback alive until after UnhookWindowsHookEx.
        self.callback = None

    def poll(self) -> bool:
        # This worker may share an input queue with the target while its cursor
        # is hidden. Keep that queue responsive; the hook has its own pump.
        self._pump_messages()
        released = self._release_requested.is_set()
        if released:
            self._release_requested.clear()
        return released

    def _pump_messages(self) -> bool:
        message = _MSG()
        released = False
        # Bounding the batch keeps a stream of mouse messages from starving the watchdog.
        for _ in range(128):
            if not self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                break
            if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                released = True
            else:
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
        return released

    def send(self, flags: int, data: int = 0, point: Point = (0, 0)):
        event = _INPUT(type=0)
        event.mi = _MOUSEINPUT(point[0], point[1], data & 0xFFFFFFFF, flags, 0, INPUT_MARKER)
        ctypes.set_last_error(0)
        if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise _windows_error(
                "Windows blocked pointer input. Elevated apps and secure desktops are unsupported."
            )

    def restore(self, point: Point):
        # SetCursorPos updates the displayed position outside the input stream.
        # A mouse packet already in flight can then overwrite it with coordinates
        # near the controlled app. Queue the handoff after earlier input instead,
        # using our marker so it cannot feed back into physical/raw movement.
        self.send(0x0001 | 0x8000 | 0x4000,
                  point=normalize_absolute(point, self.desktop()))


@dataclass
class _Command:
    name: str
    args: tuple
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class InputController:
    """Routes input on a worker separate from the native hook's message pump.

    Call heartbeat() from the GUI every 250 ms or faster. start() is synchronous;
    it raises if safe control could not be established. stop() and close() are
    idempotent. Events are ('move', preview-local point), ('released', reason),
    and ('error', message). No hook is installed until explicit start().
    """

    def __init__(self, events: queue.Queue, *, _native=None):
        self.events = events
        self._native = _native
        self._commands: queue.Queue = queue.Queue()
        self._pending = deque()
        self._pending_lock = threading.Lock()
        self._move_pending = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False
        self._handoff_until = 0.0
        self._closed = False
        self._last_heartbeat = time.monotonic()
        self._buttons: set[tuple[int, int]] = set()
        self._hook_error: str | None = None
        self._return_point: Point | None = None
        self._point: Point = (0, 0)
        self._target: Rect = (0, 0, 2, 2)
        self._preview: Rect = (0, 0, 2, 2)
        self._desktop: Rect = (0, 0, 2, 2)
        self._primary: Rect = (0, 0, 2, 2)
        self._raw_positions: dict[tuple[int, int], tuple[float, float]] = {}
        self._last_raw_motion = 0.0
        self._unmatched_hook_motion: float | None = None
        self._remote_outside = False
        self._remote_entered = False
        self._guard = None
        self._seamless = False
        self._on_release = None

    @property
    def active(self) -> bool:
        return self._active

    def heartbeat(self):
        self._last_heartbeat = time.monotonic()

    def start(self, target_rect: Rect, preview_rect: Rect, *, guard=None,
              initial_point: Point | None = None, seamless: bool = False, on_release=None):
        target_rect, preview_rect = validate_rect(target_rect), validate_rect(preview_rect)
        if self._closed:
            raise RuntimeError("This input controller has been closed.")
        if self.active:
            raise RuntimeError("Release the current display before switching targets.")
        self.heartbeat()
        if initial_point is not None and not (
            0 <= initial_point[0] < preview_rect[2] and 0 <= initial_point[1] < preview_rect[3]
        ):
            raise ValueError("The starting pointer must be inside the preview.")
        self._submit("start", target_rect, preview_rect, guard, initial_point, seamless, on_release)

    def stop(self, reason: str = "Released"):
        if self._thread is not None and self._thread.is_alive():
            self._submit("stop", reason)

    def click(self, button: int = 1, *, point: Point | None = None):
        """Forward a completed viewer click when entry raced the first hover."""
        messages = {1: (0x0201, 0x0202), 2: (0x0207, 0x0208), 3: (0x0204, 0x0205)}
        if button not in messages:
            raise ValueError("Unsupported mouse button")
        self._submit("click", messages[button], point)

    def retarget(self, target_rect: Rect, preview_rect: Rect):
        """Keep the desktop pointer fixed when a popup expands the preview bounds."""
        self._submit("retarget", validate_rect(target_rect), validate_rect(preview_rect))

    def _retarget(self, target, preview):
        if not self._active:
            return
        point = preview_to_target(self._point, self._preview, self._target)
        self._point = tuple(
            min(max((point[axis] - target[axis]) * (preview[axis + 2] - 1)
                    / (target[axis + 2] - 1), 0), preview[axis + 2] - 1)
            for axis in (0, 1)
        )
        self._target, self._preview = target, preview
        with self._pending_lock:
            # Do not reinterpret pre-layout clicks in the new image. Preserve
            # releases for downs already sent, even if a dialog opened mid-drag.
            releases = []
            for event in self._pending:
                message, _, data = event
                if message in BUTTONS:
                    _, release, pressed = BUTTONS[message]
                    button_data = (data >> 16) & 0xFFFF if release == 0x0100 else 0
                    if not pressed and (release, button_data) in self._buttons:
                        releases.append(event)
            self._pending.clear()
            self._pending.extend(releases)
        self._move_pending = self._move_pending or (
            preview_to_target(self._point, preview, target) != point
        )
        self._notify("move", self._point)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._thread is not None and self._thread.is_alive():
            self._submit("close")
            self._thread.join(timeout=1.0)

    def _submit(self, name, *args):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="wcfi-pointer", daemon=True)
            self._thread.start()
        command = _Command(name, args)
        self._commands.put(command)
        self._wake.set()
        if not command.done.wait(1.0):
            command.cancelled.set()
            # A delayed start must never take over the desktop after the caller gives up.
            self._commands.put(_Command("stop", ("Control startup timed out",)))
            self._wake.set()
            raise RuntimeError("The Windows input worker did not respond; control was cancelled.")
        if command.error is not None:
            raise command.error

    def _notify(self, name, value):
        try:
            self.events.put_nowait((name, value))
        except queue.Full:
            # The rendering queue must never hold up the global hook/watchdog.
            pass

    def _capture_error(self, message):
        self._hook_error = message
        self._active = False  # Immediately stop suppressing physical input.

    def _capture_event(self, message, point, data):
        if not self._active:
            # In-flight hook coordinates still refer to the target while we
            # return the cursor. Keep them suppressed until the marked return
            # has completed, visibility is restored, and the hook is removed.
            # Fail open on a deadline even if native cleanup stalls.
            if time.monotonic() < self._handoff_until:
                return message == WM_MOUSEMOVE or message in BUTTONS or message in (
                    WM_MOUSEWHEEL, WM_MOUSEHWHEEL,
                )
            return False
        if message == WM_MOUSEMOVE:
            # Hook positions are output cursor coordinates, not physical motion. Comparing
            # them to GetCursorPos feeds our asynchronous SendInput warps back into movement
            # (especially with precision touchpads). WM_INPUT is the sole motion source.
            if self._unmatched_hook_motion is None:
                self._unmatched_hook_motion = time.monotonic()
        elif message in BUTTONS or message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            with self._pending_lock:
                if message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    delta = ctypes.c_int16((data >> 16) & 0xFFFF).value
                    if self._pending and self._pending[-1][0] == message:
                        previous = ctypes.c_int16(
                            (self._pending[-1][2] >> 16) & 0xFFFF
                        ).value
                        combined = previous + delta
                        if -32768 <= combined <= 32767 and previous * delta >= 0:
                            self._pending[-1] = (
                                message, (0, 0), (combined & 0xFFFF) << 16,
                            )
                        else:
                            self._pending.append((message, (0, 0), data))
                    else:
                        self._pending.append((message, (0, 0), data))
                else:
                    self._pending.append((message, (0, 0), data))
            self._wake.set()
        else:
            return False
        return True

    def _capture_motion(self, dx: int, dy: int, flags: int, device: int, source: int = 0):
        if not self._active:
            return
        self._last_raw_motion = time.monotonic()
        self._unmatched_hook_motion = None
        if flags & 1 and source == CRD_INPUT_MARKER:
            # CRD's normal mouse mode addresses screen pixels in the viewer.
            # Unlike absolute touchpads, these are positions, not a delta stream.
            # Do not subtract a previous remote position after our native warp.
            desktop = self._desktop if flags & 2 else self._primary
            point = (desktop[0] + round(dx * (desktop[2] - 1) / 65535),
                     desktop[1] + round(dy * (desktop[3] - 1) / 65535))
            with self._pending_lock:
                self._pending.append(('remote_move', point, 0))
            self._wake.set()
            return
        self._remote_outside = False
        if flags & 1:  # MOUSE_MOVE_ABSOLUTE: baseline each device independently.
            desktop = self._desktop if flags & 2 else self._primary
            sample = (dx * (desktop[2] - 1) / 65535, dy * (desktop[3] - 1) / 65535)
            key = device, flags & 2
            previous = self._raw_positions.get(key)
            self._raw_positions[key] = sample
            if previous is None:
                return
            dx, dy = sample[0] - previous[0], sample[1] - previous[1]
        else:
            # Precision touchpads may legitimately report device == 0.
            self._raw_positions.pop((device, 0), None)
            self._raw_positions.pop((device, 2), None)
        if dx or dy:
            with self._pending_lock:
                self._pending.append((WM_MOUSEMOVE, (dx, dy), 0))
            self._wake.set()

    def _begin(self, target, preview, guard=None, initial_point=None, seamless=False,
               on_release=None):
        if self._active:
            raise RuntimeError("A display is already being controlled.")
        if self._native.buttons_down():
            raise RuntimeError("Release all mouse buttons, then choose Control again.")
        self._desktop = validate_rect(self._native.desktop())
        self._primary = validate_rect(self._native.primary())
        self._target, self._preview = target, preview
        self._guard = guard
        self._seamless, self._on_release = seamless, on_release
        self._return_point = self._native.position()
        self._point = initial_point if initial_point is not None else (
            preview[2] // 2, preview[3] // 2,
        )
        with self._pending_lock:
            self._pending.clear()
        self._move_pending = False
        self._hook_error = None
        self._raw_positions.clear()
        self._last_raw_motion = 0.0
        self._unmatched_hook_motion = None
        self._remote_outside = False
        self._remote_entered = seamless
        try:
            self._native.install(self._capture_event, self._capture_error, self._capture_motion)
            if self._hook_error is not None:
                raise RuntimeError(self._hook_error)
            self._active = True
            self._move_pending = not self._move()
            self._notify("move", self._point)
        except Exception as exc:
            # The queued release is processed after start() raises on the UI
            # thread. Keep the cause here so it cannot erase the useful error.
            self._release(f"Could not start control: {exc}")
            raise

    def _move(self):
        if not self._check_guard():
            return False
        point = preview_to_target(self._point, self._preview, self._target)
        self._native.hide_cursor(point)
        self._native.send(0x0001 | 0x8000 | 0x4000, point=normalize_absolute(point, self._desktop))
        return True

    def _route(self, message, delta, data):
        allowed = self._check_guard()
        if message == 'remote_move':
            x, y = delta[0] - self._preview[0], delta[1] - self._preview[1]
            if not (0 <= x < self._preview[2] and 0 <= y < self._preview[3]):
                # Never turn a remote click on viewer chrome into an edge click
                # in the selected app. Returning outside the image releases.
                self._remote_outside = True
                self._move_pending = False
                # Allow travel from the Control button into the image first.
                if self._remote_entered:
                    destination = (preview_exit(self._point, (x, y), self._preview)
                                   if self._seamless else delta)
                    self._release('Pointer left the preview', return_point=destination)
                return
            self._remote_outside = False
            self._remote_entered = True
            self._point = x, y
            self._notify('move', self._point)
            self._move_pending = True
        elif message == WM_MOUSEMOVE:
            next_point = self._point[0] + delta[0], self._point[1] + delta[1]
            if self._seamless and not (
                0 <= next_point[0] < self._preview[2] and 0 <= next_point[1] < self._preview[3]
            ):
                destination = preview_exit(self._point, next_point, self._preview)
                self._release('Pointer left the preview', return_point=destination)
                return
            self._point = (
                min(max(self._point[0] + delta[0], 0), self._preview[2] - 1),
                min(max(self._point[1] + delta[1], 0), self._preview[3] - 1),
            )
            self._notify("move", self._point)
            self._move_pending = True
        elif message in BUTTONS:
            # Preserve movement order at click/drag boundaries without generating one
            # injected cursor warp for every high-rate raw touchpad packet.
            self._flush_move()
            flag, release, pressed = BUTTONS[message]
            button_data = (data >> 16) & 0xFFFF if release == 0x0100 else 0
            if not allowed:
                # Always balance a down already delivered before a popup opened.
                if not pressed and (release, button_data) in self._buttons:
                    self._native.send(release, button_data)
                    self._buttons.discard((release, button_data))
                return
            self._native.send(flag, button_data)
            if pressed:
                self._buttons.add((release, button_data))
            else:
                self._buttons.discard((release, button_data))
        else:
            if not allowed:
                return
            self._flush_move()
            wheel_delta = ctypes.c_int16((data >> 16) & 0xFFFF).value
            self._native.send(0x0800 if message == WM_MOUSEWHEEL else 0x1000, wheel_delta)

    def _flush_move(self):
        if self._move_pending and self._active:
            self._move_pending = not self._move()

    def _check_guard(self):
        if self._guard is not None:
            point = preview_to_target(self._point, self._preview, self._target)
            allowed = self._guard(point)
            if allowed is False:
                raise RuntimeError(getattr(self._guard, "reason", "") or
                                   "Window focus or input target changed; control released")
            return allowed is not None and not self._remote_outside
        return not self._remote_outside

    def _release(self, reason, return_point=None):
        had_control = self._active or self._return_point is not None
        if return_point is None and had_control and self._seamless:
            return_point = (round(self._preview[0] + self._point[0]),
                            round(self._preview[1] + self._point[1]))
        self._handoff_until = time.monotonic() + HANDOFF_TIMEOUT if had_control else 0.0
        self._active = False
        self._guard = None
        with self._pending_lock:
            self._pending.clear()
        self._move_pending = False
        errors = []
        try:
            for release, data in self._buttons:
                try:
                    self._native.send(release, data)
                except Exception as exc:  # noqa: BLE001 - cleanup must still unhook
                    errors.append(str(exc))
            self._buttons.clear()
        finally:
            # Restore the viewer's hit testing BEFORE unhooking/returning the
            # pointer. Otherwise a fast exit click can pass through the viewer.
            cleanup, self._on_release = self._on_release, None
            if cleanup is not None:
                try:
                    cleanup()  # Native only; never call Tk from the input worker.
                except Exception as exc:  # noqa: BLE001 - still release hooks/buttons
                    errors.append(str(exc))
            destination = return_point if return_point is not None else self._return_point
            self._return_point = None
            if destination is not None:
                try:
                    self._native.restore(destination)
                except Exception as exc:  # noqa: BLE001 - report cleanup failure to the UI
                    errors.append(str(exc))
            try:
                # The target cursor stays hidden until it has returned to the
                # viewer. This also runs after startup/restore/unhook failures.
                self._native.show_cursor()
            except Exception as exc:  # noqa: BLE001 - finish release notification
                errors.append(str(exc))
            try:
                self._native.uninstall()
            except Exception as exc:  # noqa: BLE001 - always finish the bounded handoff
                errors.append(str(exc))
            finally:
                self._handoff_until = 0.0
        if had_control:
            self._notify("released", reason)
        if errors:
            self._notify("error", errors[0])

    def _run(self):
        initialization_error = None
        try:
            if self._native is None:
                self._native = _Win32()
        except Exception as exc:  # noqa: BLE001 - return worker initialization failures to caller
            initialization_error = exc
        closing = False
        try:
            while not closing:
                while True:
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if command.cancelled.is_set():
                            continue
                        if command.name == "close":
                            closing = True
                        elif initialization_error is not None:
                            raise initialization_error
                        elif command.name == "start":
                            self._begin(*command.args)
                            if command.cancelled.is_set():
                                self._release("Control startup cancelled")
                        elif command.name == "stop":
                            self._release(*command.args)
                        elif command.name == "retarget":
                            self._retarget(*command.args)
                        elif command.name == "click" and self._active:
                            try:
                                messages, point = command.args
                                if point is not None:
                                    self._route('remote_move', (
                                        self._preview[0] + point[0], self._preview[1] + point[1],
                                    ), 0)
                                for message in messages:
                                    if not self._active:
                                        break
                                    self._route(message, (0, 0), 0)
                            except Exception:
                                self._release("Click interrupted")
                                raise
                    except Exception as exc:  # noqa: BLE001 - propagate through command result
                        command.error = exc
                    finally:
                        command.done.set()
                if closing or initialization_error is not None:
                    if closing:
                        break
                else:
                    try:
                        if self._native.poll():
                            self._release("Released with Ctrl+Alt+Q")
                        if self._hook_error is not None:
                            error, self._hook_error = self._hook_error, None
                            self._release("Pointer hook failed")
                            self._notify("error", error)
                        if self._active and time.monotonic() - self._last_heartbeat > HEARTBEAT_TIMEOUT:
                            self._release("Released because the preview stopped responding")
                        if self._active:
                            self._check_guard()
                        if self._active and self._unmatched_hook_motion is not None and (
                            time.monotonic() - max(
                                self._unmatched_hook_motion, self._last_raw_motion,
                            ) > RAW_INPUT_TIMEOUT
                        ):
                            self._release(
                                "This pointing device did not provide raw movement; control released"
                            )
                        while self._active:
                            with self._pending_lock:
                                event = self._pending.popleft() if self._pending else None
                            if event is None:
                                break
                            self._route(*event)
                        self._flush_move()
                    except Exception as exc:  # noqa: BLE001 - release input on any worker failure
                        self._release("Input control stopped")
                        self._notify("error", str(exc))
                self._wake.wait(0.008)
                self._wake.clear()
        finally:
            if self._native is not None:
                self._release("Closed")
