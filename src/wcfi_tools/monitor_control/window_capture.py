"""Windows Graphics Capture adapter for one HWND, including while occluded."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace


def create_window_camera(device, hwnd: int, size: tuple[int, int]):
    """Keep the DXcam 0.3 integration isolated; reuse its owned-buffer readback."""
    from dxcam import DXCamera
    from dxcam._libs.dxgi import IDXGIDevice
    from dxcam.core.winrt_duplicator import WinRTDuplicator, _WinRTCaptureBindings
    from winrt.windows.graphics.capture.interop import create_for_window

    class WindowDuplicator(WinRTDuplicator):
        def _handle_frame_size_change(self, frame):
            # Keep the capture session alive during resize. DXcam's monitor
            # adapter refreshes its output descriptor here; an HWND has no such
            # descriptor, so use the new frame's content size instead.
            size = frame.content_size
            self._output.resolution = self._output.surface_size = (size.width, size.height)
            return super()._handle_frame_size_change(frame)

        def _create_capture_session(self, output, device):
            try:
                self._open_window_session(output, device)
            except BaseException:
                self.release()
                raise

        def _open_window_session(self, output, device):
            bindings = _WinRTCaptureBindings.load()
            self._get_dxgi_surface_from_object = bindings.get_dxgi_surface_from_object
            self._pixel_format = bindings.directx_pixel_format.B8_G8_R8_A8_UINT_NORMALIZED
            dxgi_device = device.device.QueryInterface(IDXGIDevice)
            pointer = ctypes.cast(dxgi_device, ctypes.c_void_p).value
            self._winrt_device = bindings.create_direct3d11_device_from_dxgi_device(pointer)
            self._capture_item = create_for_window(hwnd)
            dimensions = self._capture_item.size
            output.resolution = output.surface_size = (dimensions.width, dimensions.height)
            self._frame_pool = bindings.frame_pool_cls.create_free_threaded(
                self._winrt_device, self._pixel_format, 2, dimensions,
            )
            self._session = self._frame_pool.create_capture_session(self._capture_item)
            self._cursor_capture_enabled = False  # The viewer draws its mapped pointer.
            self._apply_session_options()
            self._register_frame_arrived_event()
            self._session.start_capture()

    class WindowCamera(DXCamera):
        def grab(self, **kwargs):
            pixels = super().grab(**kwargs)
            size = self._output.resolution
            if size != (self.width, self.height):
                self.width, self.height = size
                self.region = (0, 0, *size)
                # The just-returned image could be cached at the old size. The
                # next grab rebuilds staging storage for the new region.
                return None
            return pixels

        def _create_duplicator(self):
            return WindowDuplicator(output=self._output, device=self._device)

        def _recover_output(self):
            # HWND lifetime is checked by the UI. Never recover onto a different
            # monitor/window through the base class's monitor recovery heuristic.
            raise RuntimeError("Window capture changed. Reselect the application to resume.")

    output = SimpleNamespace(resolution=size, surface_size=size, rotation_angle=0,
                             update_desc=lambda: None)
    return WindowCamera(output=output, device=device, region=None, output_color="BGRA",
                        max_buffer_len=2, backend="winrt", processor_backend="numpy")
