"""Scoped visibility accounting; no real cursor or input queues are touched."""

from types import SimpleNamespace

import pytest

from wcfi_tools.monitor_control.cursor import ScopedCursor


class CursorDesktop:
    def __init__(self):
        self.count = 0
        self.target = 2
        self.attached = None
        self.app_counts = {2: 2, 3: -1}
        self.calls = []
        self.capture = 0

    def WindowFromPoint(self, point):
        return self.target

    def GetCapture(self):
        return self.capture

    def GetWindowThreadProcessId(self, hwnd, pid):
        return hwnd

    def PeekMessageW(self, *args):
        return 0

    def AttachThreadInput(self, owner, target, attach):
        self.calls.append((target, attach))
        self.attached = target if attach else None
        return 1

    def ShowCursor(self, show):
        self.count += 1 if show else -1
        return self.count + self.app_counts.get(self.attached, 0)


def fixture():
    desktop = CursorDesktop()
    return ScopedCursor(desktop, SimpleNamespace(GetCurrentThreadId=lambda: 1)), desktop


def test_balances_only_our_visibility_calls_and_keeps_existing_app_state():
    cursor, desktop = fixture()
    cursor.hide_at((10, 20))
    assert desktop.count == -3
    cursor.hide_at((30, 40))
    assert desktop.count == -3 and len(desktop.calls) == 1
    cursor.restore()
    cursor.restore()
    assert desktop.count == 0 and desktop.attached is None
    assert desktop.app_counts == {2: 2, 3: -1}


def test_switching_threads_and_capture_preserve_other_apps_visibility():
    cursor, desktop = fixture()
    cursor.hide_at((10, 20))
    desktop.target, desktop.capture = 3, 2
    cursor.hide_at((30, 40))
    assert desktop.attached == 2  # A drag still belongs to its capture owner.
    desktop.capture = 0
    cursor.hide_at((30, 40))
    assert desktop.attached == 3 and desktop.count == -1
    assert desktop.calls == [(2, True), (2, False), (3, True)]
    cursor.restore()
    assert desktop.count == 0 and desktop.app_counts[3] == -1


def test_excessive_counter_fails_with_bounded_reversible_changes():
    cursor, desktop = fixture()
    desktop.app_counts[2] = 100
    with pytest.raises(RuntimeError, match='kept its pointer visible'):
        cursor.hide_at((10, 20))
    cursor.restore()
    assert desktop.count == 0 and desktop.attached is None
    assert desktop.app_counts[2] == 100
