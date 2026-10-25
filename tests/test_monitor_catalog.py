"""Live search must never retarget input when windows reorder or disappear."""

import threading
import time

import pytest

from wcfi_tools.monitor_control.capture import Monitor
from wcfi_tools.monitor_control.catalog import (
    APPLICATIONS,
    DISPLAYS,
    TargetCatalog,
    WindowDiscovery,
)
from wcfi_tools.monitor_control.windows import AppWindow


def catalog():
    return TargetCatalog([Monitor("screen", "Main", 0, 0, 1920, 1080, True)], [
        AppWindow(10, 100, "Chrome — Slides"), AppWindow(20, 200, "Ableton — Session"),
    ])


def test_views_have_independent_search_and_selection():
    model = catalog()
    model.select(model.windows[1])
    model.searches[APPLICATIONS] = "ABLETON session"
    assert [e.target.hwnd for e in model.entries()] == [20]
    model.mode = DISPLAYS
    assert model.current() is None
    model.select(model.monitors[0])
    assert len(model.entries()) == 1
    model.mode = APPLICATIONS
    assert model.current().target.hwnd == 20
    assert model.searches[APPLICATIONS] == "ABLETON session"


def test_search_no_matches_does_not_switch_or_lose_selected_target():
    model = catalog()
    model.select(model.windows[0])
    model.searches[APPLICATIONS] = "nothing matches"
    assert model.entries() == []
    assert model.current().target.key == (10, 100)


def test_title_state_and_order_changes_preserve_identity():
    model = catalog()
    model.select(model.windows[0])
    model.windows = [AppWindow(30, 300, "A new window"),
                     AppWindow(10, 100, "Renamed", minimized=True)]
    assert model.current().target.key == (10, 100)
    assert model.current().label == "Renamed · Minimized"
    assert model.current().available


def test_closed_or_reused_handle_stays_unavailable_until_explicit_selection():
    model = catalog()
    model.select(model.windows[0])
    model.windows = [AppWindow(10, 999, "Replacement")]
    assert not model.current().available
    assert model.current().target.pid == 100
    assert len(model.entries()) == 2
    model.select(model.windows[0])
    assert model.current().available and model.current().target.pid == 999


def test_empty_start_and_duplicate_titles_are_unambiguous():
    model = TargetCatalog([], [])
    assert model.current() is None and model.entries() == []
    model.windows = [AppWindow(10, 100, "Same"), AppWindow(20, 100, "Same")]
    assert len({e.label for e in model.entries()}) == 2


def test_monitor_reconnect_follows_identity_without_selecting_reused_display_number():
    original = Monitor('DISPLAY2', 'Projector', 1920, 0, 1920, 1080, False, 'projector-id')
    model = TargetCatalog([original], [])
    model.mode = DISPLAYS
    model.select(original)
    model.monitors = [Monitor('DISPLAY2', 'Different', 0, 0, 800, 600, True, 'different-id')]
    assert not model.current().available
    assert 'Waiting to reconnect' in model.current().label
    model.monitors = []
    assert model.current().target == original
    returned = Monitor('DISPLAY3', 'Projector', -2560, 0, 2560, 1440, False, 'projector-id')
    model.monitors = [returned]
    assert model.current().available and model.current().target == returned


def test_explicit_target_change_while_disconnected_is_not_undone_on_reconnect():
    first = Monitor('a', 'A', 0, 0, 800, 600, True, 'a-id')
    second = Monitor('b', 'B', 800, 0, 800, 600, False, 'b-id')
    model = TargetCatalog([first, second], [])
    model.mode = DISPLAYS
    model.select(second)
    model.monitors = [first]
    model.select(first)
    model.monitors = [first, second]
    assert model.current().target == first


def test_metadata_changes_do_not_invalidate_capture_geometry():
    assert AppWindow(1, 2, "Old") == AppWindow(1, 2, "New", minimized=True)
    assert AppWindow(1, 2, "Old") != AppWindow(1, 3, "Old")


def test_discovery_runs_off_ui_and_recovers_after_enumeration_failure():
    calls, entered, proceed = [], threading.Event(), threading.Event()
    def enumerate_fn():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert proceed.wait(2)
            raise RuntimeError("Temporary enumeration failure")
        return [AppWindow(1, 2, "Found")]
    worker = WindowDiscovery(enumerate_fn, interval=60)
    def take():
        deadline = time.perf_counter() + 2
        while time.perf_counter() < deadline:
            result = worker.take()
            if result is not None:
                return result
            time.sleep(.001)
        pytest.fail("Discovery result missing")
    try:
        assert entered.wait(1)
        assert worker.take() is None  # A blocked enumerator does not block the UI.
        proceed.set()
        assert isinstance(take(), RuntimeError)
        worker.request()
        assert take()[0].title == "Found"
        assert set(calls) != {threading.get_ident()}
    finally:
        proceed.set()
        worker.close()
        worker.thread.join(2)
    assert not worker.thread.is_alive()
