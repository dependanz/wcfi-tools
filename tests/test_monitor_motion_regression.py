"""Pointer-feedback regressions; all input and cursor operations are simulated."""

import queue
import time

import pytest

from wcfi_tools.monitor_control import input as pointer


class FakeDesktop:
    def __init__(self):
        self.cursor = (100, 200)
        self.sent = []
        self.restored = []
        self.operations = []
        self.installed = False
        self.reject_position_reads = False
        self.echo_hook_moves = False

    def hide_cursor(self, point):
        pass

    def show_cursor(self):
        pass

    def position(self):
        if self.reject_position_reads:
            raise AssertionError("Movement must never read the redirected system cursor")
        return self.cursor

    def desktop(self):
        return -1920, 0, 3840, 1080

    def primary(self):
        return 0, 0, 1920, 1080

    def buttons_down(self):
        return False

    def install(self, on_event, on_error, on_motion):
        self.installed = True
        self.on_event = on_event
        self.on_error = on_error
        self.on_motion = on_motion

    def uninstall(self):
        self.installed = False

    def poll(self):
        return False

    def send(self, flags, data=0, point=(0, 0)):
        self.sent.append((flags, data, point))
        self.operations.append(("send", flags))
        if self.echo_hook_moves and flags & 1:
            # Asynchronous desktop warps can disagree wildly with a touchpad's
            # queued hook coordinates. Neither coordinate is physical movement.
            self.cursor = (40000, -20000)
            self.on_event(pointer.WM_MOUSEMOVE, (-30000, 50000), 0)

    def restore(self, point):
        self.restored.append(point)
        self.operations.append(("restore", point))


@pytest.fixture
def controller():
    desktop = FakeDesktop()
    control = pointer.InputController(queue.Queue(), _native=desktop)
    try:
        control.start((-1920, 0, 1920, 1080), (100, 100, 960, 540))
        assert control.events.get(timeout=0.5) == ("move", (480, 270))
        desktop.reject_position_reads = True
        yield control, desktop
    finally:
        control.close()


def next_move(control):
    event, point = control.events.get(timeout=0.5)
    assert event == "move", (event, point)
    return point


def assert_final_cursor(control, desktop):
    # A button forces pending movement to reach Windows first. Check that final
    # location rather than requiring one injection per raw sample (now batched).
    desktop.on_event(0x0201, (0, 0), 0)
    deadline = time.monotonic() + .5
    while desktop.sent[-1][0] != 0x0002 and time.monotonic() < deadline:
        time.sleep(.001)
    assert desktop.sent[-1][0] == 0x0002
    expected = pointer.normalize_absolute(
        pointer.preview_to_target(control._point, control._preview, control._target),
        control._desktop,
    )
    assert desktop.sent[-2][2] == expected


def test_hook_coordinates_never_become_motion_or_read_system_cursor(controller):
    control, desktop = controller
    for point in ((101, 200), (-30000, 50000), (40000, -20000), (0, 0)):
        assert desktop.on_event(pointer.WM_MOUSEMOVE, point, 0)
    assert control._point == (480, 270)
    assert not control._pending
    assert control.events.empty()
    assert len(desktop.sent) == 1
    assert control.active


@pytest.mark.parametrize("device", [0, 42])
def test_relative_touchpad_motion_ignores_stale_cursor_coordinates(controller, device):
    control, desktop = controller
    desktop.cursor = (-50000, 40000)
    for delta, expected in [((1, 0), (481, 270)), ((2, -1), (483, 269)), ((-1, 2), (482, 271))]:
        desktop.on_motion(*delta, 0, device)
        assert next_move(control) == expected
    assert control.active and desktop.installed
    assert not desktop.restored


def test_alternating_single_steps_do_not_amplify_or_oscillate(controller):
    control, desktop = controller
    desktop.echo_hook_moves = True
    for _ in range(50):
        desktop.on_motion(1, -1, 0, 0)
        desktop.on_motion(-1, 1, 0, 0)
    for _ in range(50):
        assert next_move(control) == (481, 269)
        assert next_move(control) == (480, 270)
    assert control._point == (480, 270)
    assert_final_cursor(control, desktop)
    assert not control._pending
    assert control.active


@pytest.mark.parametrize("flags,width", [(1, 1920), (3, 3840)])
def test_absolute_touchpad_uses_its_baseline_and_coordinate_space(controller, flags, width):
    control, desktop = controller
    desktop.on_motion(32000, 30000, flags, 0)
    assert control.events.empty()
    assert control._point == (480, 270)
    desktop.on_motion(32034, 29939, flags, 0)
    assert next_move(control) == pytest.approx(
        (480 + 34 * (width - 1) / 65535, 270 - 61 * 1079 / 65535)
    )
    assert control.active


def test_absolute_devices_have_independent_baselines(controller):
    control, desktop = controller
    desktop.on_motion(10000, 10000, 1, 0)
    desktop.on_motion(50000, 50000, 1, 42)
    assert control.events.empty()
    desktop.on_motion(10034, 10000, 1, 0)
    assert next_move(control) == pytest.approx((480 + 34 * 1919 / 65535, 270))


def test_switching_from_relative_to_absolute_takes_a_new_baseline(controller):
    control, desktop = controller
    desktop.on_motion(10000, 10000, 1, 0)
    desktop.on_motion(1, 1, 0, 0)
    assert next_move(control) == (481, 271)
    desktop.on_motion(50000, 50000, 1, 0)
    assert control.events.empty()
    desktop.on_motion(50034, 50000, 1, 0)
    assert next_move(control) == pytest.approx((481 + 34 * 1919 / 65535, 271))


def test_buffered_raw_samples_do_not_feed_back_injected_hook_positions(controller):
    control, desktop = controller
    desktop.echo_hook_moves = True
    # Queue faster than rendering and interleave the absolute positions that
    # previously became enormous deltas when compared with GetCursorPos.
    for _ in range(100):
        desktop.on_event(pointer.WM_MOUSEMOVE, (-25000, 35000), 0)
        desktop.on_motion(1, 1, 0, 0)
    for offset in range(1, 101):
        assert next_move(control) == (480 + offset, 270 + offset)
    assert_final_cursor(control, desktop)
    assert not control._pending
    assert control.active


@pytest.mark.parametrize("delta,edge", [((-10000, -10000), (0, 0)), ((10000, 10000), (959, 539))])
def test_drag_at_preview_edge_stays_in_control_until_explicit_release(controller, delta, edge):
    control, desktop = controller
    assert desktop.on_event(0x0201, (40000, -20000), 0)
    desktop.on_motion(*delta, 0, 0)
    assert next_move(control) == edge
    assert control.active and desktop.installed
    assert (0x0004, 0) in control._buttons
    assert not any(flags == 0x0004 for flags, _, _ in desktop.sent)
    assert not desktop.restored

    control.stop("User released")
    assert not control.active and not desktop.installed
    assert not control._buttons
    assert desktop.operations[-2:] == [("send", 0x0004), ("restore", (100, 200))]

