"""Raw motion tests use native packet layouts without installing desktop input."""

import ctypes
import struct
from types import SimpleNamespace

import pytest

from wcfi_tools.monitor_control import raw_input


def packet(x=-7, y=11, flags=0, device=0, marker=0, kind=0):
    pointer_format = "Q" if ctypes.sizeof(ctypes.c_void_p) == 8 else "I"
    header_format = "<II" + pointer_format * 2
    mouse = struct.pack("<H2xIIiiI", flags, 0, 0, x, y, marker)
    size = struct.calcsize(header_format) + len(mouse)
    return struct.pack(header_format, kind, size, device, 0) + mouse


def test_windows_mouse_layout_has_fixed_scalar_widths_and_union_padding():
    assert ctypes.sizeof(raw_input._RAWMOUSE) == 24
    assert raw_input._RAWMOUSE.button_union.offset == 4
    assert raw_input._RAWMOUSE.lLastX.offset == 12
    assert raw_input._RAWMOUSE.ulExtraInformation.offset == 20
    assert ctypes.sizeof(raw_input._RAWINPUTHEADER) == 8 + 2 * ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(raw_input._RAWINPUTDEVICE) == 8 + ctypes.sizeof(ctypes.c_void_p)


def test_precision_touchpad_with_zero_device_handle_is_not_rejected():
    assert raw_input.parse_raw_mouse(packet(device=0)) == (-7, 11, 0, 0)


def test_device_handle_preserves_pointer_width():
    device = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 1)) + 123
    assert raw_input.parse_raw_mouse(packet(device=device)) == (-7, 11, 0, device)


def test_absolute_motion_flags_and_origin_are_preserved():
    assert raw_input.parse_raw_mouse(packet(0, 65535, flags=3)) == (0, 65535, 3, 0)


def test_only_our_marker_is_filtered():
    assert raw_input.parse_raw_mouse(packet(marker=raw_input.INPUT_MARKER)) is None
    assert raw_input.parse_raw_mouse(packet(marker=0xFF515780)) == (-7, 11, 0, 0)


def test_remote_source_is_preserved_without_discarding_zero_device_input():
    assert raw_input.parse_raw_mouse(
        packet(30000, 40000, flags=3, marker=raw_input.CRD_INPUT_MARKER),
        include_source=True,
    ) == (30000, 40000, 3, 0, raw_input.CRD_INPUT_MARKER)


def test_keyboard_packet_is_ignored():
    assert raw_input.parse_raw_mouse(packet(kind=1)) is None


@pytest.mark.parametrize("data", [b"", packet()[:-1], packet()[:8]])
def test_truncated_packets_are_rejected(data):
    with pytest.raises(ValueError):
        raw_input.parse_raw_mouse(data)


def test_short_mouse_payload_is_rejected_even_if_declared_size_matches():
    data = bytearray(packet()[:-4])
    struct.pack_into("<I", data, 4, len(data))
    with pytest.raises(ValueError, match="mouse packet"):
        raw_input.parse_raw_mouse(data)


def test_construction_and_close_do_not_load_windows_libraries(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Construction must not load DLLs.")

    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    receiver = raw_input.RawMouseReceiver(lambda *args: None, lambda error: None)
    receiver.close()
    receiver.close()
    assert receiver.hwnd is None


@pytest.mark.parametrize("fail_motion", [False, True])
def test_window_proc_cleans_raw_input_even_if_python_callback_raises(fail_motion):
    calls = []

    def motion(*args):
        calls.append(("motion", args))
        if fail_motion:
            raise KeyboardInterrupt("callback failed")

    receiver = raw_input.RawMouseReceiver(motion, lambda error: calls.append(("error", error)))
    receiver._read_motion = lambda handle: (-1, 2, 0, 0)
    receiver.user32 = SimpleNamespace(
        DefWindowProcW=lambda *args: calls.append(("cleanup", args)) or 0
    )
    assert receiver._window_proc(123, raw_input.WM_INPUT, 0, 456) == 0
    assert calls[0] == ("motion", (-1, 2, 0, 0))
    assert calls[-1] == ("cleanup", (123, raw_input.WM_INPUT, 0, 456))
    assert any(call[0] == "error" for call in calls) == fail_motion


def test_error_callback_exception_never_crosses_window_proc():
    calls = []

    def fail(*args):
        raise SystemExit("callback failed")

    receiver = raw_input.RawMouseReceiver(fail, fail)
    receiver._read_motion = fail
    receiver.user32 = SimpleNamespace(
        DefWindowProcW=lambda *args: calls.append("cleanup") or 0
    )
    assert receiver._window_proc(123, raw_input.WM_INPUT, 0, 456) == 0
    assert calls == ["cleanup"]


def test_close_removes_registration_with_null_target_before_destroying_window():
    calls = []
    receiver = raw_input.RawMouseReceiver(lambda *args: None, lambda error: None)
    receiver.hwnd = 123
    receiver._registered = True
    receiver._class_registered = True
    receiver._callback = object()
    raw_input._LIVE_RECEIVERS.add(receiver)

    def unregister(device_pointer, count, size):
        device = ctypes.cast(device_pointer, ctypes.POINTER(raw_input._RAWINPUTDEVICE)).contents
        calls.append(("remove", device.usUsagePage, device.usUsage, device.dwFlags,
                      device.hwndTarget, count, size))
        return 1

    receiver.user32 = SimpleNamespace(
        RegisterRawInputDevices=unregister,
        DestroyWindow=lambda hwnd: calls.append(("destroy", hwnd)) or 1,
        UnregisterClassW=lambda *args: calls.append(("class",)) or 1,
    )
    receiver.close()
    receiver.close()
    assert calls == [
        ("remove", 1, 2, raw_input.RIDEV_REMOVE, None, 1,
         ctypes.sizeof(raw_input._RAWINPUTDEVICE)),
        ("destroy", 123), ("class",),
    ]
    assert receiver._callback is None
    assert receiver not in raw_input._LIVE_RECEIVERS


def test_failed_window_cleanup_keeps_native_callback_alive():
    receiver = raw_input.RawMouseReceiver(lambda *args: None, lambda error: None)
    receiver.hwnd = 123
    receiver._class_registered = True
    callback = receiver._callback = object()
    raw_input._LIVE_RECEIVERS.add(receiver)
    receiver.user32 = SimpleNamespace(
        DestroyWindow=lambda hwnd: 0,
        UnregisterClassW=lambda *args: 0,
    )
    try:
        receiver.close()
        assert receiver._callback is callback
        assert receiver in raw_input._LIVE_RECEIVERS
    finally:
        raw_input._LIVE_RECEIVERS.discard(receiver)
