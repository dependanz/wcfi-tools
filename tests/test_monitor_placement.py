"""Window rescue and placement without driving the user's desktop."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wcfi_tools.monitor_control import placement
from wcfi_tools.monitor_control.capture import Monitor
from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
from wcfi_tools.monitor_control.windows import AppWindow


@pytest.mark.parametrize('bounds,work,expected', [
    ((2500, 200, 800, 600), (0, 0, 1920, 1040), (1120, 200, 800, 600)),
    ((-4000, -2000, 800, 600), (0, 40, 1920, 1000), (0, 40, 800, 600)),
    ((100, 100, 2000, 1500), (-1280, -300, 1280, 984), (-1280, -300, 1280, 984)),
    ((10, 50, 800, 600), (0, 40, 1920, 1000), (10, 50, 800, 600)),
])
def test_fit_preserves_size_or_fits_usable_area(bounds, work, expected):
    assert placement.fit_window(bounds, work) == expected


def test_disconnected_destination_rejected_before_window_mutation(monkeypatch):
    user = Mock()
    monkeypatch.setattr(placement, '_api', lambda: (user, None))
    monkeypatch.setattr(placement, 'enumerate_monitors', list)
    validate = Mock()
    monkeypatch.setattr(placement, '_validate', validate)
    with pytest.raises(RuntimeError, match='disconnected'):
        placement.move_window_to_display(AppWindow(1, 10, 'App'), 99, 'removed')
    validate.assert_not_called()
    user.ShowWindow.assert_not_called()
    user.SetWindowPos.assert_not_called()


def test_bring_home_resolves_viewer_screen_work_area_not_primary(monkeypatch):
    user = Mock()
    monkeypatch.setattr(placement, '_api', lambda: (user, None))
    monkeypatch.setattr(placement, 'top_level_hwnd', lambda _: 99)
    user.MonitorFromWindow.return_value = 7
    def info(handle, address):
        r = address._obj.rcWork
        r.left, r.top, r.right, r.bottom = -1600, 0, 0, 860
        return True
    user.GetMonitorInfoW.side_effect = info
    assert placement.destination_work_area(12) == (-1600, 0, 1600, 860)
    user.MonitorFromWindow.assert_called_once_with(99, 2)


def test_hotplug_between_enumeration_and_resolution_does_not_redirect_move(monkeypatch):
    user = Mock()
    monkeypatch.setattr(placement, '_api', lambda: (user, None))
    monkeypatch.setattr(placement, 'enumerate_monitors', lambda: [
        Monitor('wanted', 'External', 1920, 0, 1920, 1080, False),
    ])
    user.MonitorFromPoint.return_value = 7
    def info(handle, address):
        address._obj.szDevice = 'replacement'
        return True
    user.GetMonitorInfoW.side_effect = info
    with pytest.raises(RuntimeError, match='configuration changed'):
        placement.destination_work_area(99, 'wanted')


def native_fixture(monkeypatch, *, maximized=False, minimized=False, oversized=False):
    from wcfi_tools.monitor_control import window_scene
    root, dialog = AppWindow(1, 10, 'Root'), AppWindow(2, 10, 'Dialog')
    rects = {1: (3000, 200, 800, 600), 2: (3500, 300, 400, 300)}
    user = Mock()
    user.IsZoomed.return_value = maximized
    user.IsIconic.return_value = minimized
    monkeypatch.setattr(placement, '_validate', lambda _: (user, None))
    monkeypatch.setattr(placement, 'destination_work_area', lambda *_: (0, 40, 1200, 760))
    monkeypatch.setattr(placement, 'window_rect', lambda w: rects[w.hwnd])
    monkeypatch.setattr(window_scene, 'discover_scene', lambda *_: WindowScene(tuple(
        WindowLayer(w, rects[w.hwnd]) for w in (root, dialog)
    )))
    monkeypatch.setattr(window_scene, 'owned_by', lambda _, h, r: h == 2 and r == 1)
    def read(hwnd, address):
        x, y, width, height = rects[hwnd]
        r = address._obj
        r.left, r.top, r.right, r.bottom = x, y, x + width, y + height
        return True
    user.GetWindowRect.side_effect = read
    def move(hwnd, after, x, y, width, height, flags):
        if flags & 1:
            width, height = rects[hwnd][2:]
        elif oversized and hwnd == 1:
            # Simulate an app enforcing a larger minimum size after a DPI change.
            width, height = 1600, 1000
        rects[hwnd] = (x, y, width, height)
        return True
    user.SetWindowPos.side_effect = move
    return SimpleNamespace(root=root, user=user, rects=rects)


@pytest.mark.parametrize('state', [{}, {'minimized': True}, {'maximized': True}])
def test_offscreen_window_and_dialog_are_rescued_without_touching_viewer(monkeypatch, state):
    f = native_fixture(monkeypatch, **state)
    placement.move_window_to_display(f.root, 99)
    assert f.rects == {1: (400, 200, 800, 600), 2: (800, 300, 400, 300)}
    assert {c.args[0] for c in f.user.SetWindowPos.call_args_list} == {1, 2}
    if state.get('maximized'):
        assert [c.args for c in f.user.ShowWindow.call_args_list] == [(1, 9), (1, 3)]
    elif state.get('minimized'):
        f.user.ShowWindow.assert_called_once_with(1, 9)


def test_app_minimum_size_cannot_leave_titlebar_on_missing_screen(monkeypatch):
    f = native_fixture(monkeypatch, oversized=True)
    placement.move_window_to_display(f.root, 99)
    assert f.rects[1] == (0, 40, 1600, 1000)


def test_reused_window_handle_is_rejected_before_moving(monkeypatch):
    f = native_fixture(monkeypatch)
    monkeypatch.setattr(placement, '_validate', Mock(side_effect=RuntimeError('closed')))
    with pytest.raises(RuntimeError, match='closed'):
        placement.move_window_to_display(f.root, 99)
    f.user.SetWindowPos.assert_not_called()


@pytest.mark.parametrize('minimized,position,should_move', [
    (False, (3000, -1000), True), (False, (20, 40), False),
    (True, (3000, -1000), False), (False, (20, -900), True),
])
def test_viewer_rehomes_only_when_titlebar_is_stranded(monkeypatch, minimized, position, should_move):
    user = Mock()
    user.IsIconic.return_value = minimized
    monkeypatch.setattr(placement, '_api', lambda: (user, None))
    monkeypatch.setattr(placement, 'top_level_hwnd', lambda _: 99)
    monkeypatch.setattr(placement, 'destination_work_area', lambda _: (0, 0, 1920, 1040))
    def read(hwnd, address):
        x, y = position
        r = address._obj
        r.left, r.top, r.right, r.bottom = x, y, x + 1100, y + 800
        return True
    user.GetWindowRect.side_effect = read
    placement.keep_viewer_visible(99, [Monitor('a', 'A', 0, 0, 1920, 1080, True)])
    assert user.SetWindowPos.called == should_move
    user.ShowWindow.assert_not_called()
    if should_move:
        assert user.SetWindowPos.call_args.args[-1] == 0x0014  # no activation
