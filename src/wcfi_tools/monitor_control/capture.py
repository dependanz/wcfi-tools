"""Windows Desktop Duplication capture with filtered GDI memory-image scaling.

Call ``enable_dpi_awareness`` before constructing any Tk windows. All coordinates
are physical pixels in the virtual desktop; monitors can have negative origins.
Desktop Duplication captures desktop content without toggling the system cursor.
GDI is used only to scale memory images in the live preview.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from typing import Self

from .geometry import overlap_area


def composite_bgra(destination, source):
    """Source-over premultiplied WGC pixels onto an opaque scene, in place.

    Transparent popup margins contain black RGB, not black window content.
    Blend before scaling or converting to PPM, which has no alpha channel.
    """
    import numpy as np

    translucent = source[:, :, 3] != 255
    if not np.any(translucent):
        destination[:] = source
        return
    # Most window pixels are opaque. Gather only margins/shadows instead of
    # allocating and blending three full-size 16-bit color planes per frame.
    rows, columns = np.nonzero(translucent)
    foreground = source[rows, columns].astype(np.uint16)
    background = destination[rows, columns, :3].astype(np.uint16)
    background *= 255 - foreground[:, 3:4]
    background += 127
    background //= 255
    background += foreground[:, :3]
    destination[:] = source
    destination[rows, columns, :3] = np.minimum(background, 255).astype(np.uint8)
    destination[:, :, 3] = 255


@dataclass(frozen=True)
class Monitor:
    device: str
    label: str
    left: int
    top: int
    width: int
    height: int
    primary: bool
    stable_id: str = ""

    @property
    def key(self):
        return self.stable_id or self.device


class _DisplayDevice(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


def monitor_identity(user32, device: str) -> str:
    """Per-monitor interface names survive GDI display-number changes when available."""
    try:
        enum = user32.EnumDisplayDevicesW
        enum.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                         ctypes.POINTER(_DisplayDevice), wintypes.DWORD]
        enum.restype = wintypes.BOOL
        identities = []
        for index in range(64):
            info = _DisplayDevice(cb=ctypes.sizeof(_DisplayDevice))
            if not enum(device, index, ctypes.byref(info), 1):  # EDD_GET_DEVICE_INTERFACE_NAME
                break
            if info.StateFlags & 1 and info.DeviceID:  # DISPLAY_DEVICE_ACTIVE
                identities.append(info.DeviceID.casefold())
        return '|'.join(sorted(set(identities)))
    except (AttributeError, OSError):
        return ""  # Virtual/older drivers may only expose their GDI device name.


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 1)]


@lru_cache(maxsize=1)
def _libraries():
    if sys.platform != "win32":
        raise RuntimeError("Monitor control requires Windows.")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    # Explicit pointer-sized handles are essential on 64-bit Python.
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetStretchBltMode.restype = ctypes.c_int
    gdi32.SetBrushOrgEx.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.POINT),
    ]
    gdi32.SetBrushOrgEx.restype = wintypes.BOOL
    gdi32.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.GdiFlush.argtypes = []
    gdi32.GdiFlush.restype = wintypes.BOOL
    return user32, gdi32


def _error(operation: str) -> OSError:
    code = ctypes.get_last_error()
    detail = ctypes.FormatError(code).strip() if code else "Windows returned failure"
    return OSError(code, f"{operation}: {detail}")


def enable_dpi_awareness() -> None:
    """Use physical coordinates; call once before creating windows.

    An existing process DPI setting is respected. Older Windows versions fall
    back to per-monitor v1 or system awareness.
    """
    user32, _ = _libraries()
    try:
        setter = user32.SetProcessDpiAwarenessContext
    except AttributeError:
        pass
    else:
        setter.argtypes = [wintypes.HANDLE]
        setter.restype = wintypes.BOOL
        if setter(ctypes.c_void_p(-4)) or ctypes.get_last_error() == 5:
            return  # PER_MONITOR_AWARE_V2, or awareness was already established.
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        setter = shcore.SetProcessDpiAwareness
    except (AttributeError, OSError):
        pass
    else:
        setter.argtypes = [ctypes.c_int]
        setter.restype = ctypes.c_long
        result = setter(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if result in (0, -2147024891):  # S_OK or E_ACCESSDENIED (already set)
            return
    user32.SetProcessDPIAware.argtypes = []
    user32.SetProcessDPIAware.restype = wintypes.BOOL
    user32.SetProcessDPIAware()


def enumerate_monitors() -> list[Monitor]:
    """List desktop displays, primary first, with monitor interface identities."""
    user32, _ = _libraries()
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    monitors: list[Monitor] = []
    errors: list[OSError] = []

    @callback_type
    def collect(handle, _dc, _rect, _data):
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            errors.append(_error("GetMonitorInfoW"))
            return False
        rect = info.rcMonitor
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width > 0 and height > 0:
            device = info.szDevice
            name = device.removeprefix("\\\\.\\")
            primary = bool(info.dwFlags & 1)
            label = f"{name} - {width} x {height}" + (" (primary)" if primary else "")
            monitors.append(Monitor(
                device, label, rect.left, rect.top, width, height, primary,
                monitor_identity(user32, device),
            ))
        return True

    success = user32.EnumDisplayMonitors(None, None, collect, 0)
    if errors:
        raise errors[0]
    if not success:
        raise _error("EnumDisplayMonitors")
    return sorted(monitors, key=lambda monitor: (not monitor.primary, monitor.device))


def top_level_hwnd(hwnd: int) -> int:
    """Resolve Tk's ``winfo_id()`` child handle to its real top-level window."""
    user32, _ = _libraries()
    return int(user32.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT


class GdiCapture:
    """Reusable GDI desktop capture, owned and closed by its creating thread."""

    def __init__(self) -> None:
        self._user32, self._gdi32 = _libraries()
        self._screen = None
        self._memory = None
        self._bitmap = None
        self._original_bitmap = None
        self._bits = ctypes.c_void_p()
        self._size = (0, 0)
        self._closed = False
        try:
            self._screen = self._user32.GetDC(None)
            if not self._screen:
                raise _error("GetDC")
            self._memory = self._gdi32.CreateCompatibleDC(self._screen)
            if not self._memory:
                raise _error("CreateCompatibleDC")
            # Area filtering retains fine strokes when the desktop is reduced.
            if not self._gdi32.SetStretchBltMode(self._memory, 4):  # HALFTONE
                raise _error("SetStretchBltMode")
            if not self._gdi32.SetBrushOrgEx(self._memory, 0, 0, None):
                raise _error("SetBrushOrgEx")
        except BaseException:
            self.close()
            raise

    def _resize(self, width: int, height: int) -> None:
        if self._size == (width, height):
            return
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # Top-down, not bottom-up.
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32  # BGRA; every row is DWORD-aligned.
        bits = ctypes.c_void_p()
        bitmap = self._gdi32.CreateDIBSection(
            self._screen, ctypes.byref(info), 0, ctypes.byref(bits), None, 0,
        )
        if not bitmap:
            raise _error("CreateDIBSection")
        previous = self._gdi32.SelectObject(self._memory, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            error = _error("SelectObject")
            self._gdi32.DeleteObject(bitmap)
            raise error
        if self._original_bitmap is None:
            self._original_bitmap = previous
        if self._bitmap:
            self._gdi32.DeleteObject(self._bitmap)
        self._bitmap, self._bits, self._size = bitmap, bits, (width, height)

    def grab(
        self, left: int, top: int, width: int, height: int,
        out_width: int, out_height: int,
    ) -> bytes:
        """Return a scaled RGB PPM image suitable for ``tkinter.PhotoImage``.

        A source rectangle can be a full monitor or a practice window's client
        area. This captures visible pixels; minimized/occluded windows and secure
        or protected content cannot be recovered by this backend.
        """
        if self._closed:
            raise RuntimeError("Capture has been closed.")
        values = (left, top, width, height, out_width, out_height)
        if any(not isinstance(value, int) for value in values):
            raise TypeError("Capture coordinates and sizes must be integers.")
        if min(width, height, out_width, out_height) <= 0:
            raise ValueError("Capture dimensions must be positive.")
        if any(not -(2**31) <= value < 2**31 for value in values):
            raise ValueError("Capture coordinates exceed Windows integer limits.")
        if out_width * out_height > 16_777_216:
            raise ValueError("Preview is too large (maximum 16 megapixels).")
        self._resize(out_width, out_height)
        if not self._gdi32.StretchBlt(
            self._memory, 0, 0, out_width, out_height,
            self._screen, left, top, width, height, 0x40CC0020,  # SRCCOPY | CAPTUREBLT
        ):
            raise _error("StretchBlt")
        return self._ppm(out_width, out_height)

    def _ppm(self, out_width: int, out_height: int) -> bytes:
        # GDI can batch writes. Flush before directly accessing DIB memory.
        if not self._gdi32.GdiFlush():
            raise _error("GdiFlush")
        pixels = out_width * out_height
        bgra = ctypes.string_at(self._bits, pixels * 4)
        rgb = bytearray(pixels * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
        return f"P6\n{out_width} {out_height}\n255\n".encode("ascii") + rgb

    def close(self) -> None:
        """Release the DIB, memory DC and desktop DC; safe to call repeatedly."""
        if self._closed:
            return
        if self._memory and self._original_bitmap:
            self._gdi32.SelectObject(self._memory, self._original_bitmap)
        if self._bitmap:
            self._gdi32.DeleteObject(self._bitmap)
        if self._memory:
            self._gdi32.DeleteDC(self._memory)
        if self._screen:
            self._user32.ReleaseDC(None, self._screen)
        self._bitmap = self._memory = self._screen = None
        self._bits = ctypes.c_void_p()
        self._closed = True

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("Capture has been closed.")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def local_capture_region(target, output) -> tuple[int, int, int, int] | None:
    """Translate physical desktop bounds into one output's local pixel coordinates."""
    x, y, width, height = target
    ox, oy, ow, oh = output
    if x < ox or y < oy or x + width > ox + ow or y + height > oy + oh:
        return None
    return x - ox, y - oy, x - ox + width, y - oy + height


class Capture(GdiCapture):
    """Capture DXGI frames; never read the screen with GDI in the live preview.

    The DXcam 0.3 adapter is intentionally contained here. We own fresh devices
    and outputs on the worker instead of sharing DXcam's global camera cache.
    Recreating this backend therefore re-enumerates outputs after hotplug.
    """

    def __init__(self) -> None:
        self._camera = None
        self._source = None
        self._outputs = []
        self._binding = None
        self._com = None
        self._window_cameras = {}
        super().__init__()
        try:
            try:
                import comtypes
                from dxcam import DXCamera
                from dxcam.core import Device, Output
                from dxcam.util.io import enum_dxgi_adapters
            except ImportError as exc:
                raise RuntimeError(
                    'Install monitor capture support: pip install "wcfi-tools[monitor-control]"'
                ) from exc
            comtypes.CoInitialize()
            self._com = comtypes
            self._camera_type = DXCamera
            for adapter in enum_dxgi_adapters():
                device = Device(adapter)
                for pointer in device.enum_outputs():
                    output = Output(pointer)
                    if output.attached_to_desktop:
                        rect = output.desc.DesktopCoordinates
                        self._outputs.append((device, output, (
                            rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
                        )))
            if not self._outputs:
                raise RuntimeError("No desktop capture outputs are available. Reconnect and Refresh.")
            self._source = GdiCapture()
        except BaseException:
            self.close()
            raise

    def grab(
        self, left: int, top: int, width: int, height: int,
        out_width: int, out_height: int,
    ) -> bytes | None:
        if self._closed:
            raise RuntimeError("Capture has been closed.")
        self._close_window_cameras()
        values = (left, top, width, height, out_width, out_height)
        if any(not isinstance(value, int) for value in values):
            raise TypeError("Capture coordinates and sizes must be integers.")
        if min(width, height, out_width, out_height) <= 0:
            raise ValueError("Capture dimensions must be positive.")
        if out_width * out_height > 16_777_216:
            raise ValueError("Preview is too large (maximum 16 megapixels).")
        target = left, top, width, height
        for index, (device, output, bounds) in enumerate(self._outputs):
            region = local_capture_region(target, bounds)
            if region is not None:
                break
        else:
            raise RuntimeError("Target is outside the connected displays. Refresh or arrange it.")
        if index != self._binding:
            if self._camera is not None:
                self._camera.release()
                self._camera = None
            self._camera = self._camera_type(
                output=output, device=device, region=None, output_color="BGRA",
                max_buffer_len=2, backend="dxgi", processor_backend="numpy",
            )
            self._binding = index
            # Match DXcam's factory warm-up after duplication is first created.
            # This runs only on the capture worker, never on the live UI thread.
            time.sleep(.1)
        # Own the BGRA bytes: a zero-copy view can outlive the mapped DXGI surface.
        pixels = self._camera.grab(region=region, copy=True, new_frame_only=False)
        if pixels is None:
            return None  # No first frame yet; the stream retries on its next deadline.
        return self._scale_pixels(pixels, width, height, out_width, out_height)

    def grab_window(
        self, hwnd: int, pid: int, left: int, top: int, width: int, height: int,
        out_width: int, out_height: int,
    ) -> bytes | None:
        from .window_capture import create_window_camera
        from .windows import AppWindow, window_rect

        if self._closed:
            raise RuntimeError("Capture has been closed.")
        self._close_window_cameras()
        window_rect(AppWindow(hwnd, pid, ""))  # Reject stale/reused handles and minimized windows.
        binding = "window", hwnd, pid, width, height
        if self._binding != binding:
            if self._camera is not None:
                self._camera.release()
                self._camera = None
            device = self._window_device((left, top, width, height))
            self._camera = create_window_camera(device, hwnd, (width, height))
            self._binding = binding
        pixels = self._camera.grab(copy=True, new_frame_only=False)
        if pixels is None:
            return None
        if pixels.shape[:2] != (height, width):
            raise RuntimeError("Window capture size changed. Restore or reselect the window.")
        return self._scale_pixels(pixels, width, height, out_width, out_height)

    def _window_device(self, bounds):
        # A straddling window has no single containing output. Start its HWND
        # session on the adapter with the largest overlap, retaining that session
        # during movement. Normal device-loss recovery rebuilds it if needed.
        return max(self._outputs, key=lambda output: overlap_area(bounds, output[2]))[0]

    def grab_scene(self, scene, left, top, width, height, out_width, out_height):
        """Read each HWND independently, then compose before a single quality scale.

        Separate capture sessions avoid IncludeSecondaryWindows' clipping at the
        main window's edges and also work on Windows versions predating that API.
        """
        import numpy as np

        from .window_capture import create_window_camera
        from .windows import window_rect

        if self._closed:
            raise RuntimeError("Capture has been closed.")
        if min(width, height, out_width, out_height) <= 0:
            raise ValueError("Capture dimensions must be positive.")
        if width * height > 33_554_432 or out_width * out_height > 16_777_216:
            raise ValueError("Application preview spans too large an area. Move its windows "
                             "closer together or reduce their size.")
        if self._camera is not None:
            self._camera.release()
            self._camera = self._binding = None
        wanted = {layer.window.key for layer in scene.layers}
        for key in list(self._window_cameras):
            if key not in wanted:
                self._window_cameras.pop(key).release()
        frames = []
        for layer in scene.layers:
            if not scene.visible_regions(layer):
                continue  # An entirely offscreen owner need not stall its visible dialog.
            if layer.bounds[2] * layer.bounds[3] > 33_554_432:
                raise ValueError("Application window is too large to capture. Reduce its size.")
            try:
                if window_rect(layer.window) != layer.bounds:
                    return None  # UI will request the new scene; never misplace an old frame.
                key = layer.window.key
                camera = self._window_cameras.get(key)
                if camera is None:
                    device = self._window_device(layer.bounds)
                    camera = create_window_camera(device, layer.window.hwnd, layer.bounds[2:])
                    self._window_cameras[key] = camera
                pixels = camera.grab(copy=True, new_frame_only=False)
                if pixels is None or pixels.shape[:2] != layer.bounds[2:][::-1]:
                    return None
                frames.append((layer, pixels))
            except (OSError, RuntimeError):
                # A menu closing between discovery and readback is normal. Only
                # ignore failures when that HWND actually disappeared or moved.
                try:
                    unchanged = window_rect(layer.window) == layer.bounds
                except RuntimeError:
                    unchanged = False
                if unchanged:
                    raise
                return None
        if (len(frames) == 1 and frames[0][0].bounds == (left, top, width, height)
                and frames[0][0].bounds in scene.visible_regions(frames[0][0])
                and np.all(frames[0][1][:, :, 3] == 255)):
            return self._scale_pixels(frames[0][1], width, height, out_width, out_height)
        composite = np.zeros((height, width, 4), dtype=np.uint8)
        composite[:, :, :3] = (20, 12, 7)  # Match the viewer's empty background.
        composite[:, :, 3] = 255
        for layer, pixels in frames:
            for rx, ry, w, h in scene.visible_regions(layer):
                x, y = rx - left, ry - top
                sx, sy = rx - layer.bounds[0], ry - layer.bounds[1]
                composite_bgra(composite[y:y + h, x:x + w], pixels[sy:sy + h, sx:sx + w])
        return self._scale_pixels(composite, width, height, out_width, out_height)

    def _close_window_cameras(self):
        cameras = getattr(self, "_window_cameras", {})
        while cameras:
            _, camera = cameras.popitem()
            camera.release()

    def _scale_pixels(self, pixels, width, height, out_width, out_height):
        self._source._resize(width, height)
        data = pixels.tobytes()
        if len(data) != width * height * 4:
            raise RuntimeError("Display geometry changed during capture. Refresh the display list.")
        ctypes.memmove(self._source._bits, data, len(data))
        self._resize(out_width, out_height)
        if not self._gdi32.StretchBlt(
            self._memory, 0, 0, out_width, out_height,
            self._source._memory, 0, 0, width, height, 0x00CC0020,
        ):
            raise _error("Scale captured image")
        return self._ppm(out_width, out_height)

    def close(self) -> None:
        try:
            self._close_window_cameras()
            if self._camera is not None:
                self._camera.release()
                self._camera = None
        finally:
            self._outputs.clear()
            self._binding = None
            try:
                if self._source is not None:
                    self._source.close()
                    self._source = None
                super().close()
            finally:
                if self._com is not None:
                    self._com.CoUninitialize()
                    self._com = None
