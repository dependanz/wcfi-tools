"""Viewer lifecycle regression tests; no windows, native hooks, or pointer movement."""

import queue
import time
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

pytest.importorskip("tkinter", reason="Monitor viewer requires the optional Tk runtime")

from wcfi_tools.monitor_control import app as viewer
from wcfi_tools.monitor_control.capture import Monitor
from wcfi_tools.monitor_control.catalog import TargetCatalog
from wcfi_tools.monitor_control.stream import Frame
from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
from wcfi_tools.monitor_control.windows import AppWindow


def _viewer(*, active=False):
    app = object.__new__(viewer.MonitorApp)
    app.closed = False
    app.input = SimpleNamespace(active=active, heartbeat=Mock(), stop=Mock(), close=Mock())
    app.capture = SimpleNamespace(close=Mock(), pause=Mock(), reset=Mock())
    app.discovery = Mock()
    app.last_topology = time.monotonic()
    app.topology_error = None
    app.catalog = TargetCatalog([], [])
    app.monitors, app.windows = [], []
    app.practice = None
    app.picker = Mock()
    app.picker.tk.getboolean.return_value = False
    app.rendered_entries = None
    app.mode, app.search, app.list_hint = Mock(), Mock(), Mock()
    app.target_title, app.selector, app.switch_button = Mock(), Mock(), Mock()
    app.selector_open = False
    app.details, app.arrange_button = Mock(), Mock()
    app.placement_bar, app.move_button, app.bring_button = Mock(), Mock(), Mock()
    app.preview_timer = Mock()
    app.pass_through = Mock()
    app.root = Mock()
    app.canvas = Mock()
    app.status = Mock()
    app.status_label = Mock()
    app.recover_button = Mock()
    app._reset_recovery()
    app.guard_feedback = ""
    app.hover_blocked = False
    app.pending_preview_click = None
    app.idle_pointer_position = None
    app.image_item, app.placeholder = 1, 2
    app.cursor_ring = 11
    app.cursor_dot = 12
    app.cursor_position = (50, 75)
    app.active_geometry = ((0, 0, 1920, 1080), (10, 20, 960, 540))
    app.events = queue.Queue()
    app.frame_ready = True
    app.preview = (0, 0, 960, 540)
    app.window_scene = app.presented_scene = None
    app.window_guard = None
    app._draw_cursor = Mock()
    return app


def test_picker_layout_change_releases_input_and_invalidates_old_coordinates():
    app = _viewer(active=True)
    app._set_selector(True)
    app.input.stop.assert_called_once()
    assert not app.frame_ready
    assert app.preview is None
    assert app.active_geometry is None
    assert app.selector_open
    app.selector.pack.assert_called_once_with(fill="x", before=app.canvas)
    app.pass_through.raise_viewer.assert_not_called()


def test_selection_collapses_picker_but_background_refresh_keeps_it_open():
    app = _viewer()
    target = AppWindow(77, 99, "Fixture")
    app.windows = [target]
    app.selector_open = True
    app.targets = [target]
    app.picker.current.return_value = 0
    app._select()
    assert app.catalog.current().target == target
    assert not app.selector_open
    assert not app.hover_blocked  # Fresh hover can enter without an extra exit/re-entry.
    app.selector.pack_forget.assert_called_once()
    app._set_selector(True)
    app._populate()
    assert app.selector_open
    app.target_title.set.assert_called_with("Fixture")


@pytest.mark.parametrize("kind", ["released", "error"])
def test_old_release_event_does_not_stop_new_control_session(kind):
    app = _viewer(active=True)
    app.events.put((kind, "Previous session ended"))
    app.events.put(("move", (100, 150)))

    app._poll_input()

    app.input.stop.assert_not_called()
    app.status.set.assert_not_called()
    assert app.active_geometry is not None
    assert app.cursor_position == (100, 150)
    app.input.heartbeat.assert_called_once()
    app.recover_button.configure.assert_called_once_with(state="disabled")
    app.root.after.assert_called_once_with(10, app._poll_input)


@pytest.mark.parametrize("kind", ["released", "error"])
def test_release_event_updates_ui_after_input_has_stopped(kind):
    app = _viewer()
    app.events.put((kind, "Emergency shortcut"))

    app._poll_input()

    app.input.stop.assert_called_once_with("Emergency shortcut")
    assert app.active_geometry is None
    assert app.cursor_position is None
    app.status.set.assert_called_once_with("Emergency shortcut")
    assert app.hover_blocked


@pytest.mark.parametrize("error", [RuntimeError("Stop timed out"), OSError("Hook unavailable")])
def test_release_failure_still_clears_control_ui(error):
    app = _viewer(active=True)
    app.input.stop.side_effect = error

    app.release("Released")

    assert app.active_geometry is None
    assert app.cursor_position is None
    app.canvas.itemconfigure.assert_has_calls([
        call(app.cursor_ring, state="hidden"),
        call(app.cursor_dot, state="hidden"),
    ])
    app.canvas.configure.assert_called_once_with(highlightbackground="#34455e")
    app.status_label.configure.assert_called_once_with(fg=viewer.MUTED)
    app.status.set.assert_called_once_with(f"Released · {error}")
    app.pass_through.disable.assert_called_once()


def test_close_timeout_still_disposes_capture_and_window():
    app = _viewer(active=True)
    app.input.close.side_effect = RuntimeError("Input shutdown timed out")

    with pytest.raises(RuntimeError, match="shutdown timed out"):
        app.close()

    assert app.closed
    app.capture.close.assert_called_once()
    app.root.destroy.assert_called_once()
    app.pass_through.disable.assert_called_once()
    app.close()
    app.input.close.assert_called_once()
    app.capture.close.assert_called_once()
    app.root.destroy.assert_called_once()


def test_capture_cleanup_error_still_destroys_window():
    app = _viewer()
    app.capture.close.side_effect = OSError("Capture disposal failed")

    with pytest.raises(OSError, match="Capture disposal failed"):
        app.close()

    assert app.closed
    app.input.close.assert_called_once()
    app.root.destroy.assert_called_once()


def test_close_disposes_resources_in_order():
    app = _viewer()
    order = Mock()
    order.attach_mock(app.input.close, "input")
    order.attach_mock(app.capture.close, "capture")
    order.attach_mock(app.preview_timer.close, "timer")
    order.attach_mock(app.root.destroy, "window")

    app.close()

    assert order.mock_calls == [call.input(), call.capture(), call.timer(), call.window()]


def test_offscreen_placement_works_without_frame_and_preserves_selection(monkeypatch):
    app = _viewer(active=True)
    app.frame_ready = False
    app._target_rect = Mock(side_effect=RuntimeError('Offscreen'))
    selected = AppWindow(123, 456, 'Stranded')
    app._selected = Mock(return_value=selected)
    move = Mock()
    monkeypatch.setattr(viewer, 'move_window_to_display', move)
    app.move_selected('external')
    app.input.stop.assert_called_once_with('Moving selected application…')
    move.assert_called_once_with(selected, app.root.winfo_id(), 'external')
    app._target_rect.assert_not_called()
    app.capture.reset.assert_called_once()
    assert app.presented_scene is None and not app.frame_ready
    assert app.recovering and app.hover_blocked
    assert app._selected() == selected


def test_missing_destination_leaves_input_released_and_viewer_usable(monkeypatch):
    app = _viewer(active=True)
    app._selected = Mock(return_value=AppWindow(123, 456, 'App'))
    monkeypatch.setattr(viewer, 'move_window_to_display',
                        Mock(side_effect=RuntimeError('Display disconnected')))
    app.move_selected('gone')
    app.status.set.assert_called_with('Could not move window: Display disconnected')
    app.pass_through.disable.assert_called_once()
    assert app.active_geometry is None


def test_minimized_viewer_stops_capture_and_releases_precision_timer():
    app = _viewer(active=True)
    app.root.state.return_value = "iconic"

    app._tick()

    app.preview_timer.set_active.assert_called_once_with(False)
    app.input.stop.assert_called_once()
    assert not app.frame_ready
    app.root.after.assert_called_once_with(100, app._tick)


@pytest.mark.parametrize("expired_frame", [None, True])
def test_stalled_capture_releases_control_even_while_ui_heartbeat_is_alive(expired_frame):
    app = _viewer(active=True)
    app.root.state.return_value = "normal"
    app.settle_until = 0
    app.last_topology = time.monotonic()
    app.last_capture_at = time.perf_counter() - 1
    target, preview = app.active_geometry
    app._target_rect = Mock(return_value=target)
    app._preview_rect = Mock(return_value=preview)
    app._viewer_rect = Mock(return_value=(3000, 0, 960, 540))
    app._selected = Mock(return_value=object())
    app.canvas.winfo_width.return_value = 960
    app.canvas.winfo_height.return_value = 540
    app.capture.request = Mock(return_value=False)
    app.capture.take = Mock(return_value=(
        Frame(1, (*target, 960, 540), b"stale", app.last_capture_at, app.last_capture_at)
        if expired_frame else None
    ))
    app._present_frame = Mock()

    app._tick()

    app._present_frame.assert_not_called()
    assert not app.frame_ready
    app.input.stop.assert_called_once_with("Released because the preview stopped updating")


def test_no_displays_rejected_before_native_resources_are_allocated(monkeypatch):
    monkeypatch.setattr(viewer, "enumerate_monitors", list)
    native_input = Mock()
    native_capture = Mock()
    monkeypatch.setattr(viewer, "InputController", native_input)
    monkeypatch.setattr(viewer, "CaptureStream", native_capture)

    with pytest.raises(RuntimeError, match="did not report any connected displays"):
        viewer.MonitorApp(Mock())

    native_input.assert_not_called()
    native_capture.assert_not_called()


def test_missing_displays_block_stale_selected_target():
    app = _viewer()
    app.monitors = []
    app._selected = Mock()

    with pytest.raises(RuntimeError, match="No connected displays"):
        app._target_rect()

    app._selected.assert_not_called()


def test_launch_destroys_window_when_display_enumeration_is_empty(monkeypatch):
    root = Mock()
    monkeypatch.setattr(viewer.tk, "Tk", Mock(return_value=root))
    monkeypatch.setattr(viewer, "enable_dpi_awareness", Mock())
    monkeypatch.setattr(viewer, "enumerate_monitors", list)

    with pytest.raises(RuntimeError, match="did not report any connected displays"):
        viewer.launch()

    root.mainloop.assert_not_called()
    root.destroy.assert_called_once()


def test_refresh_preserves_selected_application_by_handle_and_process(monkeypatch):
    app = _viewer()
    app.fps = 60
    selected = AppWindow(123, 456, "Old title")
    app._selected = Mock(return_value=selected)
    app._populate = Mock()
    monitors = [Monitor("screen", "Screen", 0, 0, 1920, 1080, True)]
    app.monitors = monitors
    refreshed = [AppWindow(123, 456, "New title")]
    monkeypatch.setattr(viewer, "enumerate_monitors", lambda: monitors)
    monkeypatch.setattr(viewer, "enumerate_windows", lambda: refreshed)
    monkeypatch.setattr(viewer, "CaptureStream", Mock())
    app.refresh()
    app.capture.close.assert_not_called()
    app.capture.reset.assert_not_called()
    app.capture.pause.assert_not_called()
    app.input.stop.assert_not_called()
    app.discovery.request.assert_called_once()
    app._populate.assert_called_once_with()


def test_topology_loss_invalidates_pixels_and_reconnect_resumes_preview_only(monkeypatch):
    old = Monitor('old-name', 'External', 0, 0, 1920, 1080, True, 'same-screen')
    new = Monitor('new-name', 'External', -1920, 100, 1920, 1080, False, 'same-screen')
    app = ticking_viewer()
    del app._selected, app._target_rect
    app.monitors = app.catalog.monitors = [old]
    app.catalog.mode = 'Displays'
    app.catalog.select(old)
    app.window_scene = app.presented_scene = object()
    app.input.active = True
    app.input.stop.side_effect = lambda *_: setattr(app.input, 'active', False)
    app._populate = Mock()
    now = time.monotonic()
    topology = [[]]
    monkeypatch.setattr(viewer, 'enumerate_monitors', lambda: topology[0])
    rehome = Mock()
    monkeypatch.setattr(viewer, 'keep_viewer_visible', rehome)
    app._update_monitors()
    assert not app.input.active and app.hover_blocked and not app.frame_ready
    assert app.window_scene is None and app.presented_scene is None
    assert not app.catalog.current().available
    rehome.assert_not_called()
    app.recovery_exhausted = True
    topology[0] = [new]
    app._update_monitors()
    assert app.catalog.current().target == new
    assert not app.recovery_exhausted
    assert app.capture.reset.call_count == 2
    app._update_monitors()  # Unchanged topology must not reset or release again.
    assert app.capture.reset.call_count == 2
    app.settle_until = 0
    app.capture.take.return_value = Frame(10, (-1920, 100, 1920, 1080, 960, 540),
                                        b'new pixels', now, time.perf_counter())
    monkeypatch.setattr(viewer.tk, 'PhotoImage', Mock())
    app._tick()
    assert app.frame_ready and not app.recovering and not app.input.active
    assert app.hover_blocked
    app.capture.request.assert_called_with((-1920, 100, 1920, 1080, 960, 540),
                                           window=None, scene=None)


def test_display_enumeration_failure_releases_input_then_recovers(monkeypatch):
    app = _viewer(active=True)
    screen = Monitor('a', 'A', 0, 0, 800, 600, True)
    app.monitors = [screen]
    app._populate = Mock()
    monkeypatch.setattr(viewer, 'keep_viewer_visible', Mock())
    monkeypatch.setattr(viewer, 'enumerate_monitors', Mock(side_effect=[
        OSError('Temporary topology failure'), [screen],
    ]))
    app._update_monitors()
    app.input.stop.assert_called_once()
    assert app.monitors == [] and not app.frame_ready
    with pytest.raises(RuntimeError, match='Retrying automatically'):
        app._target_rect()
    app._update_monitors()
    assert app.monitors == [screen] and app.topology_error is None
    assert app.capture.reset.call_count == 2


def test_offscreen_application_cannot_supply_click_coordinates(monkeypatch):
    app = _viewer()
    app.monitors = [Monitor("screen", "Screen", 0, 0, 1920, 1080, True)]
    app._selected = Mock(return_value=AppWindow(123, 456, "App"))
    rect = (2100, 50, 500, 500)
    monkeypatch.setattr(viewer, "window_rect", lambda _: rect)
    monkeypatch.setattr(viewer, "discover_scene",
                        lambda w, r: WindowScene((WindowLayer(w, r),)))
    with pytest.raises(RuntimeError, match="Arrange side by side"):
        app._target_rect()


def test_application_and_dialog_can_span_displays_and_clip_offscreen_pixels(monkeypatch):
    app = _viewer()
    app.monitors = [Monitor("left", "Left", -1920, 0, 1920, 1080, False),
                    Monitor("right", "Right", 0, -200, 2560, 1440, True)]
    root, dialog = AppWindow(10, 100, "App"), AppWindow(20, 100, "Dialog")
    app._selected = Mock(return_value=root)
    monkeypatch.setattr(viewer, "window_rect", lambda _: (-2000, 100, 700, 600))
    monkeypatch.setattr(viewer, "discover_scene", lambda w, r: WindowScene((
        WindowLayer(w, r), WindowLayer(dialog, (200, -100, 500, 400)),
    )))
    assert app._target_rect() == (-1920, -100, 2620, 800)
    assert len(app.window_scene.displays) == 2


def test_closed_application_stops_capture_and_active_control(monkeypatch):
    app = _viewer(active=True)
    app.root.state.return_value = "normal"
    app.settle_until = 0
    app.last_topology = time.monotonic()
    app.image_item, app.placeholder = 1, 2
    app._target_rect = Mock(side_effect=RuntimeError("Window closed"))
    app._tick()
    app.capture.pause.assert_called_once()
    app.input.stop.assert_called_once_with("Capture unavailable: Window closed")
    assert not app.frame_ready


def test_window_list_has_real_apps_and_preserves_duplicate_title_identity():
    app = _viewer()
    app.practice = None
    app.monitors = [Monitor("screen", "Screen", 0, 0, 1920, 1080, True)]
    app.windows = [AppWindow(1, 10, "Chrome"), AppWindow(2, 10, "Chrome")]
    app._selection_changed = Mock()
    app._populate(window_key=(2, 10))
    app.picker.current.assert_called_once_with(1)
    labels = app.picker.configure.call_args.kwargs["values"]
    assert labels[0] == "Chrome"
    assert labels[0] != labels[1]


@pytest.mark.parametrize("failed", [False, True])
def test_covered_application_control_and_startup_failure_restore_viewer(monkeypatch, failed):
    app = _viewer()
    app.settle_until = 0
    app._selected = Mock(return_value=AppWindow(123, 456, "App"))
    app._target_rect = Mock(return_value=(0, 0, 800, 600))
    app._viewer_rect = Mock(return_value=(0, 0, 1000, 800))
    app._preview_rect = Mock(return_value=(0, 0, 800, 600))
    app.input.start = Mock(side_effect=RuntimeError("start failed") if failed else None)
    app.window_scene = app.presented_scene = WindowScene((
        WindowLayer(app._selected(), app._target_rect()),
    ))
    monkeypatch.setattr(viewer, "activate_window", Mock())
    app.control()
    app.pass_through.enable.assert_called_once()
    app.input.start.assert_called_once()
    if failed:
        app.pass_through.disable.assert_called_once()
        assert app.active_geometry is None
    else:
        app.pass_through.raise_viewer.assert_called_once()


def ticking_viewer():
    app = _viewer()
    app.root.state.return_value = "normal"
    app.settle_until = 0
    app.last_topology = time.monotonic()
    app.last_capture_at = time.perf_counter()
    app._target_rect = Mock(return_value=(0, 0, 1920, 1080))
    app._preview_rect = Mock(return_value=(3000, 0, 960, 540))
    app._viewer_rect = Mock(return_value=(3000, 0, 1000, 800))
    app._selected = Mock(return_value=object())
    app.canvas.winfo_width.return_value = 960
    app.canvas.winfo_height.return_value = 540
    app.capture.request = Mock(return_value=False)
    app.capture.take = Mock(return_value=None)
    return app


def test_missing_first_frame_backs_off_but_keeps_retrying_after_long_outage(monkeypatch):
    app = ticking_viewer()
    app.frame_ready = False
    now = [10.0]
    monkeypatch.setattr(viewer.time, "perf_counter", lambda: now[0])
    for attempt in range(1, 5):
        app.capture_started_at = now[0] - 6
        app._tick()
        assert app.recovery_attempts == min(attempt, 3)
        if attempt < 4:
            assert not app.recovery_exhausted
            before = app.capture.request.call_count
            app._tick()  # Retry delay must not hammer the backend.
            assert app.capture.request.call_count == before
            now[0] = app.retry_at + .1
    assert app.recovery_exhausted
    before = app.capture.request.call_count
    app._tick()
    assert app.capture.request.call_count == before
    now[0] += 60
    app._tick()
    assert app.capture.request.call_count == before + 1
    app.input.stop.assert_not_called()


def test_backend_error_recovers_without_relaunch_or_automatic_input(monkeypatch):
    app = ticking_viewer()
    app.capture.take.side_effect = RuntimeError("Device lost")
    app._tick()
    app.capture.reset.assert_called_once()
    assert app.recovering and not app.frame_ready
    app.capture.take.side_effect = None
    now = app.retry_at + .1
    monkeypatch.setattr(viewer.time, "perf_counter", lambda: now)
    app.capture.take.return_value = Frame(2, (0, 0, 1920, 1080, 960, 540), b"pixels", now, now)
    monkeypatch.setattr(viewer.tk, "PhotoImage", Mock())
    app._tick()
    assert app.frame_ready and not app.recovering
    app.status.set.assert_called_with("Preview recovered · Move out and back into the image to resume.")
    app.input.stop.assert_not_called()


def test_slow_first_frame_gets_startup_grace_without_retry_loop(monkeypatch):
    app = ticking_viewer()
    app.frame_ready = False
    monkeypatch.setattr(viewer.time, "perf_counter", lambda: 10.0)
    app.capture_started_at = 6.0
    app._tick()
    app.capture.reset.assert_not_called()
    assert not app.recovery_exhausted


def test_failed_recovery_stays_released_and_explains_closed_target(monkeypatch):
    app = ticking_viewer()
    app._selected.return_value = AppWindow(123, 456, "Closed")
    monkeypatch.setattr(viewer, "restore_window", Mock(side_effect=RuntimeError("Window closed")))
    app.recover()
    assert not app.frame_ready
    assert app.active_geometry is None
    app.pass_through.disable.assert_called_once()
    app.status.set.assert_called_with("Could not recover: Window closed")


def test_recover_keeps_selection_restores_app_and_rebuilds_capture(monkeypatch):
    app = ticking_viewer()
    selected = AppWindow(123, 456, "App")
    app._selected.return_value = selected
    restore = Mock()
    monkeypatch.setattr(viewer, "restore_window", restore)
    app.recovery_exhausted = True
    app.recovery_attempts = 3
    app.recover()
    restore.assert_called_once_with(selected)
    app.capture.reset.assert_called_once()
    assert app._selected() == selected
    assert not app.frame_ready and not app.recovery_exhausted
    assert app.recovery_attempts == 0
    assert app.presented_scene is None
    assert app.settle_until > time.monotonic()


def test_same_screen_monitor_preview_pauses_and_resumes_after_viewer_moves(monkeypatch):
    app = ticking_viewer()
    app._viewer_rect.return_value = (10, 10, 1000, 800)
    app._tick()
    app.capture.request.assert_not_called()
    assert not app.frame_ready
    app.status.set.assert_called_with("Preview paused · Viewer is on the selected display.")
    app._viewer_rect.return_value = (3000, 0, 1000, 800)
    now = time.perf_counter()
    app.capture.take.return_value = Frame(1, (0, 0, 1920, 1080, 960, 540), b"pixels", now, now)
    monkeypatch.setattr(viewer.tk, "PhotoImage", Mock())
    app._tick()
    assert app.frame_ready


def test_blocked_click_feedback_clears_when_guard_allows_input():
    app = _viewer(active=True)
    app.window_guard = SimpleNamespace(reason="Input paused · Waiting for popup")
    app._poll_input()
    app.status.set.assert_called_with("Input paused · Waiting for popup")
    app.window_guard.reason = ""
    app._poll_input()
    app.status.set.assert_called_with("Interacting · Move out of the image to return.")


def test_search_and_background_updates_do_not_release_or_retarget_active_input():
    app = _viewer(active=True)
    selected = AppWindow(123, 456, "Old title")
    app.catalog.select(selected)
    app.catalog.searches["Applications"] = "different"
    app.discovery.take.return_value = [AppWindow(123, 456, "Renamed"),
                                       AppWindow(999, 456, "Different app")]
    app._poll_windows()
    assert app.targets == [app.windows[1]]
    assert app._selected().key == selected.key
    assert app._selected().title == "Renamed"
    app.input.stop.assert_not_called()
    app.capture.pause.assert_not_called()


def test_open_dropdown_does_not_reorder_rows_under_pointer():
    app = _viewer()
    old = AppWindow(123, 456, "Old")
    app.windows = [old]
    app._populate()
    app.picker.tk.getboolean.return_value = True
    app.windows = [AppWindow(999, 888, "New"), old]
    app._populate()
    assert app.targets == [old]
    app.picker.tk.getboolean.return_value = False
    app._populate()
    assert app.targets == app.windows


def hover_viewer():
    app = _viewer()
    app.active_geometry = None
    app._preview_rect = Mock(return_value=(100, 200, 400, 300))
    app._selected = Mock(return_value=AppWindow(10, 20, 'App'))
    app.control = Mock()
    return app


def hover_event(app, x=120, y=230, state=0, num=1):
    return SimpleNamespace(widget=app.canvas, x_root=x, y_root=y, state=state, num=num)


def test_hover_starts_at_pointer_and_letterbox_does_not_start_control():
    app = hover_viewer()
    app._preview_hover(hover_event(app, x=99))
    app.control.assert_not_called()
    app._preview_hover(hover_event(app))
    app.control.assert_called_once_with((20, 30))


def test_idle_poll_enters_background_viewer_but_never_reads_redirected_cursor(monkeypatch):
    app = hover_viewer()
    app.root.state.return_value = 'normal'
    snapshot = Mock(return_value=((120, 230), True, False))
    monkeypatch.setattr(viewer, 'idle_pointer', snapshot)
    app._poll_hover()  # Opening underneath a stationary pointer is not entry.
    app.control.assert_not_called()
    snapshot.return_value = ((121, 230), True, False)
    app._poll_hover()
    app.control.assert_called_once_with((21, 30))
    app.input.active = True
    app._poll_hover()
    assert snapshot.call_count == 2


@pytest.mark.parametrize('over_viewer,held', [(False, False), (True, True)])
def test_idle_poll_ignores_foreign_windows_and_external_drags(monkeypatch, over_viewer, held):
    app = hover_viewer()
    app.root.state.return_value = 'normal'
    app.idle_pointer_position = (110, 230)
    monkeypatch.setattr(viewer, 'idle_pointer', lambda hwnd: ((120, 230), over_viewer, held))
    app._poll_hover()
    app.control.assert_not_called()


def test_emergency_release_requires_leave_and_reentry_without_focus_loop():
    app = hover_viewer()
    app.release('Released with Ctrl+Alt+Q')
    app._preview_hover(hover_event(app))
    app.control.assert_not_called()
    app._preview_hover(hover_event(app, x=99))
    app._preview_hover(hover_event(app))
    app.control.assert_called_once_with((20, 30))


def test_hover_never_takes_over_external_drag_or_stale_preview():
    app = hover_viewer()
    app._preview_hover(hover_event(app, state=0x100))
    app.frame_ready = False
    app._preview_hover(hover_event(app))
    app.frame_ready = True
    app.input.active = True
    app._preview_hover(hover_event(app))
    app.control.assert_not_called()


def test_fast_first_click_enters_and_forwards_one_balanced_click():
    app = hover_viewer()
    app.input.click = Mock()
    app.control.side_effect = lambda point: setattr(app.input, 'active', True)
    app._preview_press(hover_event(app))
    app._preview_click(hover_event(app))
    app.control.assert_called_once_with((20, 30))
    app.input.click.assert_called_once_with(1, point=(20, 30))
    app._preview_click(hover_event(app))
    app.input.click.assert_called_once()


def test_click_queued_before_hover_hook_is_not_lost_when_delivered_after_entry():
    app = hover_viewer()
    app.input.active = True
    app.input.click = Mock()
    app._preview_press(hover_event(app))
    app._preview_click(hover_event(app))
    app.control.assert_not_called()
    app.input.click.assert_called_once_with(1, point=(20, 30))


@pytest.mark.parametrize('change', ['selection', 'layout', 'drag', 'stale'])
def test_click_started_before_selection_geometry_or_readiness_change_is_discarded(change):
    app = hover_viewer()
    app._preview_press(hover_event(app))
    if change == 'selection':
        app._selected.return_value = AppWindow(11, 20, 'Other')
    elif change == 'layout':
        app._preview_rect.return_value = (101, 200, 400, 300)
    elif change == 'stale':
        app.frame_ready = False
    app._preview_click(hover_event(app, x=140 if change == 'drag' else 120))
    app.control.assert_not_called()


@pytest.mark.parametrize("new_bounds", [(100, 50, 1920, 1080), (0, 0, 1600, 900)])
def test_moving_or_resizing_app_gates_input_until_new_frame_then_retargets(monkeypatch, new_bounds):
    app = ticking_viewer()
    app.input.active = True
    app.input.retarget = Mock()
    window = AppWindow(123, 456, "App")
    old_scene = WindowScene((WindowLayer(window, app.active_geometry[0]),))
    new_scene = WindowScene((WindowLayer(window, new_bounds),))
    app._selected.return_value = window
    app._target_rect.return_value = new_bounds
    app.window_scene = new_scene
    app.presented_scene = old_scene
    app.window_guard = SimpleNamespace(presented=old_scene)
    app._tick()
    assert app.window_guard.presented is None
    app.input.stop.assert_not_called()
    app.input.retarget.assert_not_called()
    now = time.perf_counter()
    app.capture.take.return_value = Frame(1, (*new_bounds, 960, 540), b"pixels", now, now, new_scene)
    monkeypatch.setattr(viewer.tk, "PhotoImage", Mock())
    app._tick()
    app.input.stop.assert_not_called()
    app.input.retarget.assert_called_once_with(new_bounds, app._preview_rect())
    assert app.window_guard.presented == new_scene
