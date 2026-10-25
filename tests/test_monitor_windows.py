"""Application discovery, stale handles and layout; no desktop interaction."""

import ctypes
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wcfi_tools.monitor_control import windows


def fake_api(monkeypatch, *, pid=101, minimized=False, cloaked=False):
    user = Mock()
    user.IsWindow.return_value = True
    user.IsWindowVisible.return_value = True
    user.IsIconic.return_value = minimized
    user.GetLastActivePopup.side_effect = lambda hwnd: hwnd
    user.GetWindowThreadProcessId.side_effect = lambda hwnd, pointer: setattr(pointer._obj,
                                                                            "value", pid)

    def attribute(hwnd, name, pointer, size):
        if name == 14:
            pointer._obj.value = int(cloaked)
        else:
            pointer._obj.left, pointer._obj.top = -800, 20
            pointer._obj.right, pointer._obj.bottom = -200, 520
        return 0

    dwm = SimpleNamespace(DwmGetWindowAttribute=Mock(side_effect=attribute))
    monkeypatch.setattr(windows, "_api", lambda: (user, dwm))
    return user, dwm


@pytest.mark.parametrize("work", [(0, 0, 1920, 1040), (-1920, -200, 1920, 1080),
                                 (0, 0, 1024, 600)])
def test_arranged_windows_are_separate_inside_work_area(work):
    viewer, target = windows.side_by_side(work)
    for x, y, width, height in (viewer, target):
        assert x >= work[0] and y >= work[1]
        assert x + width <= work[0] + work[2]
        assert y + height <= work[1] + work[3]
    assert viewer[0] + viewer[2] < target[0]
    assert viewer[2] >= 520


def test_small_display_does_not_arrange_windows_outside_screen():
    with pytest.raises(ValueError, match="too small"):
        windows.side_by_side((0, 0, 800, 600))


def test_window_bounds_use_visible_frame_including_negative_coordinates(monkeypatch):
    user, _ = fake_api(monkeypatch)
    assert windows.window_rect(windows.AppWindow(77, 101, "App")) == (-800, 20, 600, 500)
    user.GetWindowRect.assert_not_called()


@pytest.mark.parametrize("state, message", [({"pid": 999}, "closed"),
                                           ({"minimized": True}, "minimized"),
                                           ({"cloaked": True}, "another desktop")])
def test_unavailable_window_never_exposes_stale_bounds(monkeypatch, state, message):
    fake_api(monkeypatch, **state)
    with pytest.raises(RuntimeError, match=message):
        windows.window_rect(windows.AppWindow(77, 101, "App"))


def test_activation_failure_blocks_input_instead_of_clicking_occluding_app(monkeypatch):
    user, _ = fake_api(monkeypatch)
    user.SetForegroundWindow.return_value = False
    with pytest.raises(RuntimeError, match="activate"):
        windows.activate_window(windows.AppWindow(77, 101, "App"))


def test_resume_activates_owned_modal_popup_instead_of_disabled_parent(monkeypatch):
    user, _ = fake_api(monkeypatch)
    user.GetLastActivePopup.side_effect = lambda hwnd: 88
    user.GetAncestor.side_effect = lambda hwnd, flag: hwnd
    user.GetWindow.side_effect = lambda hwnd, flag: 77 if hwnd == 88 else 0
    windows.activate_window(windows.AppWindow(77, 101, "App"))
    user.SetForegroundWindow.assert_called_once_with(88)


def test_explicit_recovery_restores_minimized_window_without_rearranging(monkeypatch):
    user, _ = fake_api(monkeypatch, minimized=True)
    user.ShowWindow.side_effect = lambda *_: setattr(user.IsIconic, "return_value", False)
    windows.restore_window(windows.AppWindow(77, 101, "App"))
    user.ShowWindow.assert_called_once_with(77, 9)
    user.SetForegroundWindow.assert_called_once_with(77)
    user.SetWindowPos.assert_not_called()


def test_recovery_never_restores_reused_handle(monkeypatch):
    user, _ = fake_api(monkeypatch, pid=999)
    with pytest.raises(RuntimeError, match="closed"):
        windows.restore_window(windows.AppWindow(77, 101, "Old app"))
    user.ShowWindow.assert_not_called()
    user.SetForegroundWindow.assert_not_called()


def test_discovery_filters_own_hidden_cloaked_and_untitled_windows(monkeypatch):
    user, dwm = fake_api(monkeypatch)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    user.EnumWindows.side_effect = lambda callback, data: all(callback(i, data) for i in range(1, 8))
    user.GetShellWindow.return_value = 7
    user.IsWindowVisible.side_effect = lambda hwnd: hwnd != 2
    user.GetWindowThreadProcessId.side_effect = lambda hwnd, ptr: setattr(
        ptr._obj, "value", os.getpid() if hwnd == 1 else 101,
    )
    dwm.DwmGetWindowAttribute.side_effect = lambda hwnd, name, ptr, size: setattr(
        ptr._obj, "value", int(hwnd == 3),
    ) or 0
    user.GetWindowTextLengthW.side_effect = lambda hwnd: 0 if hwnd == 4 else 3
    user.GetWindowTextW.side_effect = lambda hwnd, text, size: setattr(text, "value", "App")
    result = windows.enumerate_windows()
    assert [item.hwnd for item in result] == [5, 6]
    assert all(item.title == "App" for item in result)
    assert result[0].key != result[1].key  # Duplicate titles remain independently selectable.


def test_viewer_hit_testing_restores_exact_original_style_and_resolves_realized_wrapper(monkeypatch):
    user, _ = fake_api(monkeypatch)
    monkeypatch.setattr(ctypes, "set_last_error", Mock(), raising=False)
    user.GetWindowLongPtrW.return_value = 0x100
    user.SetWindowLongPtrW.return_value = 1
    wrapper = Mock(return_value=900)
    monkeypatch.setattr(windows, "top_level_hwnd", wrapper)
    overlay = windows.ViewerPassThrough(123)
    wrapper.assert_not_called()  # Tk has not necessarily created its wrapper yet.
    overlay.enable()
    user.SetWindowLongPtrW.assert_called_with(900, -20, 0x100 | 0x80000 | 0x20)
    overlay.disable()
    user.SetWindowLongPtrW.assert_called_with(900, -20, 0x100)
    assert overlay._original is None
    calls = user.SetWindowLongPtrW.call_count
    overlay.disable()
    assert user.SetWindowLongPtrW.call_count == calls


def test_viewer_passthrough_failure_rolls_back_style(monkeypatch):
    user, _ = fake_api(monkeypatch)
    monkeypatch.setattr(ctypes, "set_last_error", Mock(), raising=False)
    monkeypatch.setattr(windows, "top_level_hwnd", lambda hwnd: hwnd)
    user.GetWindowLongPtrW.return_value = 0x100
    user.SetWindowLongPtrW.return_value = 1
    user.SetLayeredWindowAttributes.return_value = False
    overlay = windows.ViewerPassThrough(123)
    with pytest.raises(RuntimeError, match="prepare"):
        overlay.enable()
    user.SetWindowLongPtrW.assert_called_with(123, -20, 0x100)
    assert overlay._original is None


def test_viewer_only_pins_during_control_and_unpins_before_handoff(monkeypatch):
    user, _ = fake_api(monkeypatch)
    monkeypatch.setattr(ctypes, "set_last_error", Mock(), raising=False)
    monkeypatch.setattr(windows, "top_level_hwnd", lambda hwnd: hwnd)
    user.GetWindowLongPtrW.return_value = 0x100
    user.SetWindowLongPtrW.return_value = 1
    overlay = windows.ViewerPassThrough(123)
    overlay.raise_viewer()
    user.SetWindowPos.assert_called_with(123, 0, 0, 0, 0, 0, 0x0013)
    overlay.enable()
    overlay.raise_viewer()
    user.SetWindowPos.assert_called_with(123, -1, 0, 0, 0, 0, 0x0013)
    overlay.disable()
    user.SetWindowLongPtrW.assert_called_with(123, -20, 0x100)
    user.SetWindowPos.assert_called_with(123, -2, 0, 0, 0, 0, 0x0013)
    count = user.SetWindowPos.call_count
    overlay.disable()
    assert user.SetWindowPos.call_count == count  # Idle refresh must not change stacking.


