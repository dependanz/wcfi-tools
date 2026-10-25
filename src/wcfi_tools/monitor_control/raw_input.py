"""Receive physical mouse motion without consulting the redirected screen cursor.

The receiver belongs to the thread that calls ``start``; that thread must pump
Windows messages and call ``close``. It does not install hooks or inject input.
Importing or constructing it is safe on platforms other than Windows.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
INPUT_MARKER = 0x57434649
# Chromium remoting/host/win/input_extra_info.h ('CRD0'). This identifies
# coordinate semantics, not authorization; the window guard still gates input.
CRD_INPUT_MARKER = 0x43524430
UINT_ERROR = 0xFFFFFFFF


# Windows LONG/ULONG remain 32 bits on Win64, unlike the host C ABI on Unix.
class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", ctypes.c_uint32), ("dwSize", ctypes.c_uint32),
        ("hDevice", ctypes.c_void_p), ("wParam", ctypes.c_size_t),
    ]


class _MOUSEBUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", ctypes.c_uint16), ("usButtonData", ctypes.c_uint16)]


class _MOUSEBUTTONUNION(ctypes.Union):
    _anonymous_ = ("buttons",)
    _fields_ = [("ulButtons", ctypes.c_uint32), ("buttons", _MOUSEBUTTONS)]


class _RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("button_union",)
    _fields_ = [
        ("usFlags", ctypes.c_uint16), ("button_union", _MOUSEBUTTONUNION),
        ("ulRawButtons", ctypes.c_uint32),
        ("lLastX", ctypes.c_int32), ("lLastY", ctypes.c_int32),
        ("ulExtraInformation", ctypes.c_uint32),
    ]


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_uint16), ("usUsage", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32), ("hwndTarget", ctypes.c_void_p),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint32), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int32), ("cbWndExtra", ctypes.c_int32),
        ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
    ]


def parse_raw_mouse(data: bytes, *, include_source: bool = False) -> tuple[int, ...] | None:
    """Decode a native-ABI RAWINPUT packet, preserving zero touchpad handles.

    Other input types and our own marked input are ignored. Invalid packet sizes
    raise instead of allowing a malformed native event to move the pointer.
    Absolute coordinates are left normalized for the controller to interpret.
    """
    header_size = ctypes.sizeof(_RAWINPUTHEADER)
    if len(data) < header_size:
        raise ValueError("Truncated Windows raw input header.")
    header = _RAWINPUTHEADER.from_buffer_copy(data)
    if header.dwSize < header_size or header.dwSize > len(data):
        raise ValueError("Invalid Windows raw input packet size.")
    if header.dwType != 0:  # RIM_TYPEMOUSE
        return None
    if header.dwSize < header_size + ctypes.sizeof(_RAWMOUSE):
        raise ValueError("Truncated Windows raw mouse packet.")
    mouse = _RAWMOUSE.from_buffer_copy(data, header_size)
    if mouse.ulExtraInformation == INPUT_MARKER:
        return None
    # A precision touchpad may have no device handle; zero is valid input.
    motion = mouse.lLastX, mouse.lLastY, mouse.usFlags, int(header.hDevice or 0)
    return (*motion, mouse.ulExtraInformation) if include_source else motion


def _windows_error(message: str) -> RuntimeError:
    code = ctypes.get_last_error()
    if code:
        message = f"{message} (Windows error {code}: {ctypes.FormatError(code).strip()})"
    return RuntimeError(message)


# If native cleanup fails, keep C callbacks alive while Windows could call them.
_LIVE_RECEIVERS: set[RawMouseReceiver] = set()


class RawMouseReceiver:
    def __init__(
        self,
        on_motion: Callable[..., None],
        on_error: Callable[[str], None],
    ):
        self.on_motion = on_motion
        self.on_error = on_error
        self.user32 = None
        self.kernel32 = None
        self.hwnd = None
        self._callback = None
        self._instance = None
        self._registered = False
        self._class_registered = False
        self._class_name = f"WcfiRawMouse_{id(self):x}"

    def _load(self):
        if sys.platform != "win32":
            raise RuntimeError("Raw mouse input is available only on Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "RegisterClassW": ([ctypes.POINTER(_WNDCLASSW)], ctypes.c_uint16),
            "UnregisterClassW": ([ctypes.c_wchar_p, ctypes.c_void_p], ctypes.c_int32),
            "CreateWindowExW": (
                [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                 ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
                ctypes.c_void_p,
            ),
            "DestroyWindow": ([ctypes.c_void_p], ctypes.c_int32),
            "DefWindowProcW": (
                [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t],
                ctypes.c_ssize_t,
            ),
            "RegisterRawInputDevices": (
                [ctypes.POINTER(_RAWINPUTDEVICE), ctypes.c_uint32, ctypes.c_uint32],
                ctypes.c_int32,
            ),
            "GetRawInputData": (
                [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                 ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32],
                ctypes.c_uint32,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result
        self.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    def start(self):
        """Create a message-only window on the calling input worker thread."""
        if self._registered and self.hwnd:
            return
        if self.hwnd or self._class_registered:
            raise RuntimeError("The previous raw input receiver has not been closed.")
        self._load()
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_size_t, ctypes.c_ssize_t,
        )
        self._callback = callback_type(self._window_proc)
        self._instance = self.kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASSW(
            lpfnWndProc=ctypes.cast(self._callback, ctypes.c_void_p).value,
            hInstance=self._instance,
            lpszClassName=self._class_name,
        )
        try:
            ctypes.set_last_error(0)
            if not self.user32.RegisterClassW(ctypes.byref(window_class)):
                raise _windows_error("Windows could not register the raw input window class.")
            self._class_registered = True
            _LIVE_RECEIVERS.add(self)
            ctypes.set_last_error(0)
            self.hwnd = self.user32.CreateWindowExW(
                0, self._class_name, "", 0, 0, 0, 0, 0,
                ctypes.c_void_p(-3), None, self._instance, None,  # HWND_MESSAGE
            )
            if not self.hwnd:
                raise _windows_error("Windows could not create the raw input receiver.")
            # Keep legacy buttons/wheels for the existing fail-open mouse hook.
            device = _RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, self.hwnd)
            ctypes.set_last_error(0)
            if not self.user32.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(device)
            ):
                raise _windows_error("Windows could not register physical pointer motion.")
            self._registered = True
        except BaseException:
            self.close()
            raise

    def _read_motion(self, handle):
        size = ctypes.c_uint32()
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        ctypes.set_last_error(0)
        result = self.user32.GetRawInputData(
            handle, RID_INPUT, None, ctypes.byref(size), header_size
        )
        if result == UINT_ERROR:
            raise _windows_error("Windows could not size physical pointer input.")
        if size.value < header_size or size.value > 1024 * 1024:
            raise ValueError("Invalid Windows raw input buffer size.")
        # DWORD allocation satisfies GetRawInputData's alignment requirement.
        buffer = (ctypes.c_uint32 * ((size.value + 3) // 4))()
        capacity = size.value
        ctypes.set_last_error(0)
        copied = self.user32.GetRawInputData(
            handle, RID_INPUT, buffer, ctypes.byref(size), header_size
        )
        if copied == UINT_ERROR:
            raise _windows_error("Windows could not read physical pointer input.")
        if copied != capacity or copied != size.value:
            raise ValueError("Windows returned an incomplete raw input packet.")
        return parse_raw_mouse(bytes(buffer)[:copied], include_source=True)

    def _report_error(self, exc):
        try:
            self.on_error(str(exc))
        except BaseException:  # noqa: BLE001, S110 - never recurse or escape a C callback
            pass

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_INPUT:
            try:
                motion = self._read_motion(lparam)
                if motion is not None:
                    self.on_motion(*motion)
            except BaseException as exc:  # noqa: BLE001 - fail open through the worker
                self._report_error(exc)
        try:
            # Required for foreground WM_INPUT cleanup, including failed reads.
            return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except BaseException as exc:  # noqa: BLE001 - never unwind through user32
            self._report_error(exc)
            return 0

    def close(self):
        """Best-effort cleanup on the owning thread, including partial startup."""
        if self.user32 is None:
            return
        if self._registered:
            try:
                device = _RAWINPUTDEVICE(1, 2, RIDEV_REMOVE, None)
                self.user32.RegisterRawInputDevices(
                    ctypes.byref(device), 1, ctypes.sizeof(device)
                )
            except BaseException:  # noqa: BLE001, S110 - still destroy the window
                pass
            finally:
                # Never let a repeated close remove a newer receiver's registration.
                self._registered = False
        if self.hwnd:
            try:
                if self.user32.DestroyWindow(self.hwnd):
                    self.hwnd = None
            except BaseException:  # noqa: BLE001, S110 - still attempt class cleanup
                pass
        if self._class_registered:
            try:
                if self.user32.UnregisterClassW(self._class_name, self._instance):
                    self._class_registered = False
            except BaseException:  # noqa: BLE001, S110 - retain callback if cleanup failed
                pass
        if not self.hwnd and not self._class_registered:
            _LIVE_RECEIVERS.discard(self)
            self._callback = None
