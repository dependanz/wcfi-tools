"""Scroll batching and hook responsiveness without installing native hooks."""

import queue
import threading
from types import SimpleNamespace

import pytest

from wcfi_tools.monitor_control import input as pointer


@pytest.mark.parametrize("deltas", [
    [-1] * 240, [-120, 120], [-32760, -120], [120, -120, -120, 120],
])
def test_scroll_batching_preserves_amount_and_direction_changes(deltas):
    sent = []
    control = pointer.InputController(
        queue.Queue(), _native=SimpleNamespace(send=lambda flags, data: sent.append(data)),
    )
    control._active = True
    for delta in deltas:
        control._capture_event(pointer.WM_MOUSEWHEEL, (0, 0), (delta & 0xFFFF) << 16)
    while control._pending:
        control._route(*control._pending.popleft())
    assert sum(sent) == sum(deltas)
    # Opposite directions must remain ordered, especially at a target's scroll edge.
    input_signs = [d > 0 for d in deltas]
    output_signs = [d > 0 for d in sent]
    assert [s for i, s in enumerate(input_signs) if i == 0 or s != input_signs[i - 1]] == [
        s for i, s in enumerate(output_signs) if i == 0 or s != output_signs[i - 1]
    ]
    assert all(-32768 <= value <= 32767 for value in sent)
    if deltas == [-1] * 240:
        assert sent == [-240]


def test_hook_pumps_while_sendinput_waits_for_a_hook_callback(monkeypatch):
    # Model SendInput waiting for a hook response. It must not block the thread
    # responsible for producing that response (the old 300+ ms stall).
    native = object.__new__(pointer._Win32)
    native._pump_thread = None
    native._pump_stop = threading.Event()
    native._pump_ready = threading.Event()
    native._release_requested = threading.Event()
    native._pump_error = None
    requested, responded = threading.Event(), threading.Event()
    owners, cleanup = [], []

    native._install_on_thread = lambda *args: owners.append(threading.get_ident())
    native._uninstall_on_thread = lambda: cleanup.append(threading.get_ident())

    def pump_messages():
        if requested.is_set():
            requested.clear()
            responded.set()
        return False

    def send_input(*args):
        requested.set()
        assert responded.wait(.5), "Input injection starved the hook message pump"
        return 1

    native._pump_messages = pump_messages
    native.user32 = SimpleNamespace(
        MsgWaitForMultipleObjectsEx=lambda *args: int(requested.wait(.005)),
        SendInput=send_input,
    )
    monkeypatch.setattr(pointer.ctypes, "set_last_error", lambda code: None, raising=False)
    errors = []
    try:
        native.install(lambda *args: False, errors.append, lambda *args: None)
        native.send(0x0800, -120)
        assert owners[0] != threading.get_ident()
    finally:
        native.uninstall()
    assert cleanup == owners
    assert not errors


def test_release_arriving_during_poll_is_not_cleared_unread():
    native = object.__new__(pointer._Win32)
    native._pump_messages = lambda: False
    requested = threading.Event()

    def read_before_arrival():
        # Simulate the hook thread setting the event just after a false read.
        requested.set()
        return False

    native._release_requested = SimpleNamespace(
        is_set=read_before_arrival, clear=requested.clear,
    )
    assert not native.poll()
    assert requested.is_set()
    native._release_requested = requested
    assert native.poll()
    assert not native.poll()
