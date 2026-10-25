"""Input safety tests use a fake desktop; never install real hooks or move a mouse."""

import ctypes
import queue
import time
from types import SimpleNamespace

import pytest

from wcfi_tools.monitor_control import input as pointer


def test_native_return_is_ordered_after_in_flight_movement(monkeypatch):
    # Reproduce the trace: a pre-existing packet near the controlled app must
    # not overwrite the return to the viewer when the input stream drains.
    desktop = (-1920, 0, 3840, 1080)
    destination = (-500, 818)
    old_packet = pointer.normalize_absolute((4, 1020), desktop)
    pending = [old_packet]
    cursor = [old_packet]
    sent = []

    def set_cursor_pos(x, y):
        cursor[0] = pointer.normalize_absolute((x, y), desktop)
        return 1

    def send_input(count, address, size):
        event = ctypes.cast(address, ctypes.POINTER(pointer._INPUT)).contents.mi
        sent.append((event.dwFlags, event.dwExtraInfo))
        pending.append((event.dx, event.dy))
        return count

    native = object.__new__(pointer._Win32)
    native.user32 = SimpleNamespace(SetCursorPos=set_cursor_pos, SendInput=send_input)
    native.desktop = lambda: desktop
    monkeypatch.setattr(ctypes, 'set_last_error', lambda value: None, raising=False)
    native.restore(destination)
    for packet in pending:
        cursor[0] = packet
    assert cursor[0] == pointer.normalize_absolute(destination, desktop)
    assert sent == [(0xC001, pointer.INPUT_MARKER)]


class FakeDesktop:
    def __init__(self):
        self.cursor = (100, 200)
        self.sent = []
        self.restored = []
        self.installed = False
        self.held = False
        self.hotkey = False
        self.fail_install = False
        self.fail_send = False
        self.hidden = False

    def hide_cursor(self, point):
        self.hidden = True

    def show_cursor(self):
        self.hidden = False

    def position(self):
        return self.cursor

    def desktop(self):
        return -1920, 0, 3840, 1080

    def primary(self):
        return 0, 0, 1920, 1080

    def buttons_down(self):
        return self.held

    def install(self, on_event, on_error, on_motion):
        if self.fail_install:
            raise RuntimeError("Hotkey unavailable")
        self.installed = True
        self.on_event, self.on_error = on_event, on_error
        self.on_motion = on_motion

    def uninstall(self):
        self.installed = False

    def send(self, flags, data=0, point=(0, 0)):
        if self.fail_send:
            raise RuntimeError("Input blocked")
        self.sent.append((flags, data, point))

    def restore(self, point):
        self.restored.append(point)
        self.cursor = point

    def poll(self):
        release, self.hotkey = self.hotkey, False
        return release


@pytest.fixture
def controller():
    desktop = FakeDesktop()
    controller = pointer.InputController(queue.Queue(), _native=desktop)
    yield controller, desktop
    controller.close()


def test_window_guard_failure_before_start_restores_pointer_and_unhooks(controller):
    control, desktop = controller
    with pytest.raises(RuntimeError, match="input target"):
        control.start((0, 0, 800, 600), (10, 20, 400, 300), guard=lambda point: False)
    assert not control.active and not desktop.installed
    assert desktop.sent == []
    assert desktop.restored == [(100, 200)]


def test_failed_start_release_keeps_diagnostic_and_allows_retry(controller):
    control, desktop = controller
    desktop.fail_send = True
    with pytest.raises(RuntimeError, match="Input blocked"):
        control.start((0, 0, 800, 600), (10, 20, 400, 300))
    assert control.events.get_nowait() == ("released", "Could not start control: Input blocked")
    desktop.fail_send = False
    control.start((0, 0, 800, 600), (10, 20, 400, 300))
    assert control.active


def test_window_guard_stops_click_before_injection(controller):
    control, desktop = controller
    allow = [True]
    control.start((0, 0, 800, 600), (10, 20, 400, 300), guard=lambda point: allow[0])
    allow[0] = False
    # The worker also checks between events; either it has already released or
    # the click boundary rejects before any button is injected.
    deadline = time.perf_counter() + 1
    control._capture_event(0x0201, (0, 0), 0)
    while control.active and time.perf_counter() < deadline:
        time.sleep(.001)
    assert not control.active
    assert not any(flags == 0x0002 for flags, *_ in desktop.sent)


def test_popup_wait_suppresses_clicks_but_keeps_control_and_virtual_movement(controller):
    control, desktop = controller
    allow = [True]
    control.start((0, 0, 800, 600), (10, 20, 400, 300), guard=lambda point: allow[0])
    desktop.sent.clear()
    allow[0] = None
    original = control._point
    control._route(pointer.WM_MOUSEMOVE, (20, 30), 0)
    control._route(0x0201, (0, 0), 0)
    control._route(0x0202, (0, 0), 0)
    control._route(pointer.WM_MOUSEWHEEL, (0, 0), 120 << 16)
    assert control.active
    assert control._point == (original[0] + 20, original[1] + 30)
    assert not desktop.sent
    allow[0] = True
    control._route(0x0201, (0, 0), 0)
    control._route(0x0202, (0, 0), 0)
    # The move suppressed during capture must reach its new location BEFORE the click.
    assert [flags for flags, *_ in desktop.sent] == [0xC001, 2, 4]


def test_popup_transition_balances_an_already_pressed_button(controller):
    control, desktop = controller
    allow = [True]
    control.start((0, 0, 800, 600), (0, 0, 400, 300), guard=lambda point: allow[0])
    control._route(0x0201, (0, 0), 0)
    allow[0] = None
    control._route(0x0202, (0, 0), 0)
    assert desktop.sent[-1][0] == 4
    assert not control._buttons and control.active


def test_expanding_scene_preserves_desktop_position_without_cursor_warp(controller):
    control, desktop = controller
    control.start((-800, 50, 600, 500), (20, 20, 300, 250))
    previous = pointer.preview_to_target(control._point, control._preview, control._target)
    sent = len(desktop.sent)
    control.retarget((-900, 0, 900, 600), (0, 0, 450, 300))
    assert pointer.preview_to_target(control._point, control._preview, control._target) == previous
    assert len(desktop.sent) == sent and control.active


def test_remote_dialog_expansion_preserves_pointer_across_negative_desktop_coordinates(controller):
    control, desktop = controller
    desktop.desktop = lambda: (-1920, -1440, 5760, 2520)
    control.start((-1200, 100, 800, 600), (20, 20, 800, 600))
    old = pointer.preview_to_target(control._point, control._preview, control._target)
    # Parent on left display, dialog on a larger display above/right. All
    # dimensions are already physical pixels; no additional DPI multiplier.
    control.retarget((-1200, -1000, 3600, 1700), (0, 0, 1080, 510))
    assert pointer.preview_to_target(control._point, control._preview, control._target) == old
    point = (1800, -700)
    preview_point = tuple((point[i] - control._target[i]) * (control._preview[i + 2] - 1)
                          / (control._target[i + 2] - 1) for i in (0, 1))
    assert pointer.preview_to_target(preview_point, control._preview, control._target) == point
    normalized = pointer.normalize_absolute(point, desktop.desktop())
    assert 0 <= normalized[0] <= 65535 and 0 <= normalized[1] <= 65535


def test_scene_resize_cannot_drop_a_queued_release_for_an_injected_down():
    # Exercise the worker operation directly without a competing worker thread.
    desktop = FakeDesktop()
    control = pointer.InputController(queue.Queue(), _native=desktop)
    control._active = True
    control._target, control._preview = (0, 0, 800, 600), (0, 0, 400, 300)
    control._buttons.add((4, 0))
    control._pending.extend([(0x0202, (0, 0), 0), (0x0201, (0, 0), 0),
                             (pointer.WM_MOUSEWHEEL, (0, 0), 120 << 16)])
    control._retarget((-100, 0, 900, 600), (0, 0, 450, 300))
    assert list(control._pending) == [(0x0202, (0, 0), 0)]


def start(controller):
    controller.start((-1920, 0, 1920, 1080), (100, 100, 960, 540))


def wait_inactive(controller):
    deadline = time.monotonic() + 0.5
    while controller.active and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not controller.active


def test_input_struct_matches_win32_abi():
    assert ctypes.sizeof(pointer._INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_negative_desktop_coordinates_and_endpoints():
    preview = (30, 60, 960, 540)
    target = (-1920, -100, 1920, 1080)
    assert pointer.preview_to_target((0, 0), preview, target) == (-1920, -100)
    assert pointer.preview_to_target((959, 539), preview, target) == (-1, 979)
    assert pointer.preview_to_target((-10, 9999), preview, target) == (-1920, 979)
    assert pointer.normalize_absolute((-1920, 0), (-1920, 0, 3840, 1080)) == (0, 0)
    assert pointer.normalize_absolute((1919, 1079), (-1920, 0, 3840, 1080)) == (65535, 65535)
    assert pointer.normalize_absolute((-9999, 9999), (-1920, 0, 3840, 1080)) == (0, 65535)


def test_preview_edge_keeps_control_active(controller):
    control, _desktop = controller
    start(control)
    control._route(pointer.WM_MOUSEMOVE, (10000, -10000), 0)
    assert control.active and control._point == (959, 0)


def test_seamless_entry_uses_hover_point_instead_of_jumping_to_center(controller):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (10, 20, 400, 300),
                  initial_point=(13, 27), seamless=True)
    assert control._point == (13, 27)
    expected = pointer.normalize_absolute(
        pointer.preview_to_target((13, 27), control._preview, control._target), desktop.desktop(),
    )
    assert desktop.sent[0][2] == expected
    assert desktop.hidden


@pytest.mark.parametrize('hook_error', [False, True])
def test_seamless_interruption_returns_to_current_preview_point(controller, hook_error):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (-500, 200, 400, 300),
                  initial_point=(20, 30), seamless=True)
    control._route(pointer.WM_MOUSEMOVE, (100, 70), 0)
    if hook_error:
        control._capture_error('Hook stopped')
    control.stop('Focus changed')
    assert desktop.restored[-1] == (-380, 300)
    assert not desktop.hidden


def test_cursor_restored_when_pointer_return_fails(controller):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (10, 20, 400, 300), seamless=True)

    def fail_restore(point):
        raise RuntimeError('Cannot return pointer')

    desktop.restore = fail_restore
    control.stop()
    assert not desktop.hidden and not desktop.installed
    assert ('error', 'Cannot return pointer') in list(control.events.queue)


@pytest.mark.parametrize('delta,expected', [((-21, 0), (99, 230)), ((380, 0), (500, 230)),
                                          ((0, -31), (120, 199)), ((0, 270), (120, 500))])
def test_seamless_exit_restores_pointer_and_hit_testing_before_unhook(controller, delta, expected):
    control, desktop = controller
    order = []
    uninstall, restore = desktop.uninstall, desktop.restore
    show = desktop.show_cursor
    desktop.show_cursor = lambda: (order.append('show'), show())
    desktop.uninstall = lambda: (order.append('unhook'), uninstall())
    desktop.restore = lambda point: (order.append('restore'), restore(point))
    control.start((1000, 0, 800, 600), (100, 200, 400, 300),
                  initial_point=(20, 30), seamless=True, on_release=lambda: order.append('style'))
    control._route(0x0201, (0, 0), 0)
    control._route(pointer.WM_MOUSEMOVE, delta, 0)
    assert not control.active and not desktop.installed
    assert desktop.sent[-1][0] == 4  # Balance a drag before exiting the image.
    assert desktop.restored[-1] == expected
    assert order == ['style', 'restore', 'show', 'unhook']
    control.stop()
    assert order.count('style') == 1


def test_handoff_suppresses_stale_physical_packets_until_return_is_finished(controller):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (100, 200, 400, 300),
                  initial_point=(20, 30), seamless=True)
    restore, show, uninstall = desktop.restore, desktop.show_cursor, desktop.uninstall
    suppressed = []

    def in_flight_packet():
        blocked = desktop.on_event(pointer.WM_MOUSEMOVE, (1003, 60), 0)
        suppressed.append(blocked)
        if not blocked:
            desktop.cursor = (1003, 60)

    desktop.restore = lambda point: (restore(point), in_flight_packet())
    desktop.show_cursor = lambda: (show(), in_flight_packet())
    desktop.uninstall = lambda: (in_flight_packet(), uninstall())
    control._route(pointer.WM_MOUSEMOVE, (-30, 0), 0)
    assert desktop.cursor == (99, 230)
    assert suppressed == [True, True, True]
    assert not desktop.on_event(pointer.WM_MOUSEMOVE, (98, 230), 0)


def test_handoff_fails_open_if_native_cleanup_stalls(controller, monkeypatch):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (100, 200, 400, 300), seamless=True)
    restore = desktop.restore

    def slow_restore(point):
        with monkeypatch.context() as patch:
            patch.setattr(pointer.time, 'monotonic', lambda: control._handoff_until + 1)
            assert not desktop.on_event(pointer.WM_MOUSEMOVE, (10, 20), 0)
        restore(point)

    desktop.restore = slow_restore
    control.stop()
    assert control._handoff_until == 0


def test_remote_seamless_exit_does_not_require_an_initial_inside_sample(controller):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (100, 200, 400, 300),
                  initial_point=(20, 30), seamless=True)
    control._route('remote_move', (90, 230), 0)
    assert not control.active and desktop.restored[-1] == (99, 230)


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('end,expected', [
    ((-380, 230), (99, 240)),  # Cross left first; do not use the endpoint's y.
    ((820, 230), (500, 325)),
    ((120, -270), (130, 199)),
    ((120, 630), (165, 500)),
    ((-380, -370), (99, 210)),  # Diagonal exits through the first crossed side.
])
def test_large_movement_returns_at_first_preview_edge(controller, remote, end, expected):
    control, desktop = controller
    control.start((1000, 0, 800, 600), (100, 200, 400, 300),
                  initial_point=(20, 30), seamless=True)
    if remote:
        control._route('remote_move', (100 + end[0], 200 + end[1]), 0)
    else:
        control._route(pointer.WM_MOUSEMOVE, (end[0] - 20, end[1] - 30), 0)
    assert not control.active and desktop.restored[-1] == expected


def test_preview_exit_uses_image_origin_with_negative_monitor_coordinates():
    assert pointer.preview_exit((20, 30), (-380, 230), (-1500, -400, 400, 300)) == (-1501, -360)


def test_completed_entry_click_is_balanced_and_uses_current_guard(controller):
    control, desktop = controller
    allow = [True]
    control.start((1000, 0, 800, 600), (100, 200, 400, 300),
                  initial_point=(20, 30), seamless=True, guard=lambda point: allow[0])
    desktop.sent.clear()
    control.click(3)
    assert [event[0] for event in desktop.sent] == [8, 16]
    assert not control._buttons
    allow[0] = None
    control.click(1)
    assert [event[0] for event in desktop.sent] == [8, 16]


def test_worker_shutdown_restores_hit_testing_even_when_cleanup_fails(controller):
    control, desktop = controller

    def fail():
        raise RuntimeError('style failed')

    control.start((1000, 0, 800, 600), (100, 200, 400, 300), seamless=True, on_release=fail)
    control.stop()
    assert not control.active and not desktop.installed and desktop.restored
    assert ('error', 'style failed') in list(control.events.queue)


def test_explicit_start_and_idempotent_release(controller):
    control, desktop = controller
    assert not control.active and not desktop.installed and not desktop.sent
    start(control)
    assert control.active and desktop.installed
    control.stop()
    assert not control.active and not desktop.installed
    assert desktop.restored == [(100, 200)]
    control.stop()
    events = list(control.events.queue)
    assert sum(event[0] == "released" for event in events) == 1


def test_raw_mouse_delta_routes_independently_of_hook_position(controller):
    control, desktop = controller
    start(control)
    # The legacy cursor can jump anywhere without becoming physical device motion.
    assert control._capture_event(pointer.WM_MOUSEMOVE, (10000, -10000), 0)
    control._capture_motion(10, 5, 0, 0)
    deadline = time.monotonic() + 0.5
    while control._point != (490, 275) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert control._point == (490, 275)
    assert control.active
    control.stop()
    assert desktop.restored[-1] == (100, 200)


def test_stop_releases_drag_button_before_restoring_cursor(controller):
    control, desktop = controller
    start(control)
    control._route(0x0201, (0, 0), 0)
    control._route(0x020B, (0, 0), 2 << 16)
    control.stop("User released")
    assert (0x0004, 0, (0, 0)) in desktop.sent
    assert (0x0100, 2, (0, 0)) in desktop.sent
    assert not control._buttons


def test_wheel_preserves_signed_delta(controller):
    control, desktop = controller
    start(control)
    control._route(pointer.WM_MOUSEWHEEL, (0, 0), (0x10000 - 120) << 16)
    control._route(pointer.WM_MOUSEHWHEEL, (0, 0), 120 << 16)
    assert desktop.sent[-2:] == [(0x0800, -120, (0, 0)), (0x1000, 120, (0, 0))]


def test_cannot_take_over_with_held_mouse_button(controller):
    control, desktop = controller
    desktop.held = True
    with pytest.raises(RuntimeError, match="Release all mouse buttons"):
        start(control)
    assert not control.active and not desktop.installed and not desktop.sent


def test_hotkey_must_be_reserved_before_takeover(controller):
    control, desktop = controller
    desktop.fail_install = True
    with pytest.raises(RuntimeError, match="Hotkey unavailable"):
        start(control)
    assert not control.active and not desktop.installed and not desktop.sent


def test_failed_send_always_unhooks_and_restores(controller):
    control, desktop = controller
    desktop.fail_send = True
    with pytest.raises(RuntimeError, match="Input blocked"):
        start(control)
    assert not control.active and not desktop.installed
    assert desktop.restored == [(100, 200)]


def test_registered_hotkey_releases_control(controller):
    control, desktop = controller
    start(control)
    desktop.hotkey = True
    wait_inactive(control)
    assert not desktop.installed


def test_gui_stall_releases_control(controller, monkeypatch):
    monkeypatch.setattr(pointer, "HEARTBEAT_TIMEOUT", 0.03)
    control, desktop = controller
    start(control)
    wait_inactive(control)
    assert not desktop.installed
    assert any("stopped responding" in str(event) for event in control.events.queue)


def test_missing_raw_device_motion_releases_instead_of_guessing(controller, monkeypatch):
    monkeypatch.setattr(pointer, "RAW_INPUT_TIMEOUT", 0.03)
    control, desktop = controller
    start(control)
    initial = control._point
    control._capture_event(pointer.WM_MOUSEMOVE, (32000, -32000), 0)
    wait_inactive(control)
    assert control._point == initial
    assert not desktop.installed
    assert any("did not provide raw movement" in str(event) for event in control.events.queue)


def test_raw_report_clears_missing_device_watchdog(controller, monkeypatch):
    monkeypatch.setattr(pointer, "RAW_INPUT_TIMEOUT", 0.03)
    control, _desktop = controller
    start(control)
    control._capture_event(pointer.WM_MOUSEMOVE, (-32000, 32000), 0)
    control._capture_motion(1, 1, 0, 0)
    time.sleep(0.06)
    assert control.active
    assert control._point == (481, 271)


def test_remote_absolute_positions_map_the_preview_without_baseline_or_accumulation():
    desktop = FakeDesktop()
    control = pointer.InputController(queue.Queue(), _native=desktop)
    control._active = True
    control._desktop = (-1920, -200, 3840, 1280)
    control._primary = (0, 0, 1920, 1080)
    control._preview = (-1800, -100, 960, 540)
    control._target = (0, 0, 1920, 1080)
    point = (-1600, 50)
    normalized = pointer.normalize_absolute(point, control._desktop)
    for _ in range(3):
        control._capture_motion(*normalized, 3, 0, pointer.CRD_INPUT_MARKER)
        control._route(*control._pending.popleft())
        control._flush_move()
        assert control._point == (200, 150)
    expected = pointer.normalize_absolute(
        pointer.preview_to_target((200, 150), control._preview, control._target),
        control._desktop,
    )
    assert desktop.sent[-1][2] == expected


def test_remote_pointer_outside_preview_releases_before_following_click(controller):
    control, desktop = controller
    start(control)
    control._route('remote_move', (200, 200), 0)
    desktop.sent.clear()
    control._route('remote_move', (50, 50), 0)
    assert not control._capture_event(0x0201, (50, 50), 0)
    assert not control.active and not desktop.installed
    assert desktop.restored[-1] == (50, 50)
    assert not any(flags == 2 for flags, *_ in desktop.sent)


def test_remote_pointer_can_travel_from_control_button_into_preview(controller):
    control, desktop = controller
    start(control)
    desktop.sent.clear()
    control._route('remote_move', (50, 50), 0)
    control._route(0x0201, (0, 0), 0)
    assert control.active and desktop.sent == []
    control._route('remote_move', (200, 200), 0)
    control._route(0x0201, (0, 0), 0)
    assert control._point == (100, 100)
    assert desktop.sent[-1][0] == 2


def test_remote_absolute_motion_still_obeys_window_guard(controller):
    control, desktop = controller
    control.start((0, 0, 800, 600), (10, 20, 400, 300), guard=lambda point: None)
    desktop.sent.clear()
    control._route('remote_move', (110, 120), 0)
    control._route(0x0201, (0, 0), 0)
    assert control.active and control._point == (100, 100)
    assert desktop.sent == []


def test_hook_error_fails_open_before_reporting(controller):
    control, desktop = controller
    start(control)
    control._capture_error("hook fault")
    assert not control._capture_event(pointer.WM_MOUSEMOVE, (300, 400), 0)
    control.stop()
    assert not desktop.installed


def test_non_windows_start_has_actionable_error(monkeypatch):
    monkeypatch.setattr(pointer.sys, "platform", "linux")
    control = pointer.InputController(queue.Queue())
    try:
        with pytest.raises(RuntimeError, match="only on Windows"):
            start(control)
    finally:
        control.close()
