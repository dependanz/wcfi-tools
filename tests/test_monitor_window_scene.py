"""Window-family discovery and input gating without touching the desktop."""

import ctypes
from unittest.mock import Mock

import pytest

from wcfi_tools.monitor_control import window_scene as scene
from wcfi_tools.monitor_control.windows import AppWindow


def fake_api(monkeypatch):
    user = Mock()
    owners = {20: 10, 30: 20}
    user.GetAncestor.side_effect = lambda hwnd, flag: 10 if hwnd == 11 else hwnd
    user.GetWindow.side_effect = lambda hwnd, flag: owners.get(hwnd, 0)
    user.GetClassNameW.side_effect = lambda hwnd, text, length: setattr(
        text, "value", "#32768" if hwnd == 40 else "App",
    )
    user.IsWindow.return_value = True
    user.IsWindowVisible.side_effect = lambda hwnd: hwnd != 50
    user.GetForegroundWindow.return_value = 10
    user.WindowFromPoint.return_value = 11
    monkeypatch.setattr(scene, "_api", lambda: (user, Mock()))
    monkeypatch.setattr(scene, "_pid", lambda user, hwnd: 100)
    return user, owners


@pytest.mark.parametrize("hwnd, expected", [(10, True), (11, True), (20, True),
                                            (30, True), (99, False), (0, False)])
def test_only_selected_root_children_and_owner_descendants_belong(monkeypatch, hwnd, expected):
    user, _ = fake_api(monkeypatch)
    assert scene.belongs_to_window(user, hwnd, 10) is expected


def test_owner_cycle_cannot_hang_the_input_worker(monkeypatch):
    user, owners = fake_api(monkeypatch)
    owners.update({60: 70, 70: 60})
    assert not scene.owned_by(user, 60, 10)


@pytest.mark.parametrize("owner, active, flags, expected", [
    (10, 10, 20, True), (20, 20, 20, True), (50, 10, 20, True),
    (50, 99, 20, False), (99, 10, 20, False), (10, 10, 0, False),
])
def test_native_menu_uses_live_gui_thread_owner_not_process_alone(
    monkeypatch, owner, active, flags, expected,
):
    user, _ = fake_api(monkeypatch)
    def thread_info(thread, ptr):
        ptr._obj.flags = flags
        ptr._obj.hwndMenuOwner = owner
        ptr._obj.hwndActive = active
        return True
    user.GetGUIThreadInfo.side_effect = thread_info
    assert scene.belongs_to_window(user, 40, 10) is expected


def test_scene_expands_in_negative_coordinates_and_keeps_z_order(monkeypatch):
    user, _ = fake_api(monkeypatch)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    user.IsIconic.return_value = False
    user.EnumWindows.side_effect = lambda callback, data: all(
        callback(hwnd, data) for hwnd in (30, 20, 99, 10)
    )
    rects = {10: (-800, 50, 600, 500), 20: (-900, 20, 300, 200), 30: (-500, 90, 400, 100)}
    monkeypatch.setattr(scene, "window_rect", lambda window: rects[window.hwnd])
    result = scene.discover_scene(AppWindow(10, 100, "App"), rects[10])
    assert [layer.window.hwnd for layer in result.layers] == [10, 20, 30]
    assert result.bounds == (-900, 20, 800, 530)


def test_guard_waits_for_popup_pixels_and_blocks_blank_gaps_and_changed_geometry(monkeypatch):
    user, _ = fake_api(monkeypatch)
    root, popup = AppWindow(10, 100, "App"), AppWindow(20, 100, "")
    root_rect, popup_rect = (0, 0, 800, 600), (700, 400, 300, 200)
    rects = {10: root_rect, 20: popup_rect}
    monkeypatch.setattr(scene, "window_rect", lambda window: rects[window.hwnd])
    guard = scene.WindowGuard(root, scene.WindowScene((scene.WindowLayer(root, root_rect),)))
    assert guard((200, 200)) is True
    assert guard.reason == ""
    user.GetForegroundWindow.return_value = user.WindowFromPoint.return_value = 20
    assert guard((750, 450)) is None  # Owned dialog exists but has no presented frame.
    assert "popup" in guard.reason
    guard.presented = scene.WindowScene((scene.WindowLayer(root, root_rect),
                                        scene.WindowLayer(popup, popup_rect)))
    assert guard((750, 450)) is True
    assert guard.reason == ""
    rects[20] = (690, 400, 300, 200)
    assert guard((750, 450)) is None
    user.WindowFromPoint.return_value = 99
    assert guard((900, 100)) is None  # Unrelated app visible through the scene's blank gap.
    user.GetForegroundWindow.return_value = 99
    assert guard((900, 100)) is False  # Actual focus change releases control.
    assert "Focus left" in guard.reason and "resume" in guard.reason


def test_guard_rejects_reused_root_handle(monkeypatch):
    fake_api(monkeypatch)
    guard = scene.WindowGuard(AppWindow(10, 999, "Old app"), scene.WindowScene(()))
    assert guard((0, 0)) is False


def test_guard_never_warps_into_a_gap_even_if_windows_reports_the_selected_hwnd(monkeypatch):
    user, _ = fake_api(monkeypatch)
    root = AppWindow(10, 100, "App")
    bounds = (-100, -100, 400, 400)
    monkeypatch.setattr(scene, "window_rect", lambda _: bounds)
    layout = ((-200, -100, 200, 400), (0, 100, 300, 200))
    guard = scene.WindowGuard(root, scene.WindowScene((scene.WindowLayer(root, bounds),), layout))
    assert guard((50, 50)) is None
    assert "between displays" in guard.reason
    user.WindowFromPoint.assert_not_called()
    assert guard((-50, 50)) is True
    assert guard((50, 150)) is True
    assert guard((300, 150)) is None  # Right edge is exclusive.


def test_scene_bounds_clip_partial_windows_and_keep_remote_dialog_coordinates():
    root, popup = AppWindow(1, 1, "App"), AppWindow(2, 1, "Dialog")
    layers = (scene.WindowLayer(root, (-1200, 0, 600, 500)),
              scene.WindowLayer(popup, (100, -400, 400, 300)))
    layout = ((-1000, 0, 1000, 800), (0, -500, 1000, 1000))
    view = scene.WindowScene(layers, layout)
    assert view.visible_regions(layers[0]) == ((-1000, 0, 400, 500),)
    assert view.bounds == (-1000, -400, 1500, 900)
