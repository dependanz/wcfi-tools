r"""Opt-in native smoke test using only the app's own practice window.

Run from the checkout: .venv\Scripts\python.exe scripts/test-monitor-control-windows.py
Temporarily moves the pointer, restores it, and closes both test windows. Do not
use the mouse during this short test. Never run as part of unattended unit tests.
Device motion is simulated at the raw-motion callback; physical touchpad/mouse
delivery must also be checked interactively. Native output and input hooks are real.
"""

from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
import traceback
from pathlib import Path

from wcfi_tools.monitor_control.app import MonitorApp
from wcfi_tools.monitor_control.capture import GdiCapture, enable_dpi_awareness, top_level_hwnd
from wcfi_tools.monitor_control.input import (
    _INPUT,
    _KEYBDINPUT,
    _MOUSEINPUT,
    _POINT,
    _Win32,
)


def main() -> int:
    if sys.platform != "win32":
        print("This opt-in integration test requires Windows.")
        return 1
    enable_dpi_awareness()
    native = _Win32()
    initial_cursor = native.position()
    native.user32.GetForegroundWindow.restype = ctypes.c_void_p
    native.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    native.user32.WindowFromPoint.argtypes = [_POINT]
    native.user32.WindowFromPoint.restype = ctypes.c_void_p
    initial_focus = native.user32.GetForegroundWindow()
    root = tk.Tk()
    app = MonitorApp(root, practice=True, fps=15)
    result = {"exit": 1}
    movements = []
    button_events = []
    wheel_events = []
    raw_wheels = []
    sent_wheels = []
    app.practice.counter.bind("<ButtonPress-1>", lambda e: button_events.append(("down", e.x, e.y)))
    app.practice.counter.bind("<ButtonRelease-1>", lambda e: button_events.append(("up", e.x, e.y)))
    root.bind_all("<MouseWheel>", lambda e: wheel_events.append((str(e.widget), e.delta)), add="+")
    app.practice.scroll_list.bind(
        "<MouseWheel>", lambda e: wheel_events.append(("listbox", e.delta)), add="+",
    )
    capture_event = app.input._capture_event

    def trace_event(message, point, data):
        if message == 0x0200:
            movements.append((point, app.input._native.position(), app.input._point))
        if message == 0x020A:
            raw_wheels.append(data)
        return capture_event(message, point, data)

    app.input._capture_event = trace_event

    def mouse(flags, *, point=(0, 0), data=0):
        event = _INPUT(type=0)
        event.mi = _MOUSEINPUT(point[0], point[1], data & 0xFFFFFFFF, flags, 0, 0x57435454)
        assert native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1

    def key(vk, up=False):
        event = _INPUT(type=1)
        event.ki = _KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0x57435454)
        assert native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1

    def move_to_widget(widget, local=None):
        assert app.practice is not None and app.input.active
        target, preview = app.active_geometry
        if local is None:
            local = widget.winfo_width() // 2, widget.winfo_height() // 2
        tx, ty = widget.winfo_rootx() + local[0], widget.winfo_rooty() + local[1]
        px = round((tx - target[0]) * (preview[2] - 1) / (target[2] - 1))
        py = round((ty - target[1]) * (preview[3] - 1) / (target[3] - 1))
        for _ in range(150):
            assert app.input.active, app.status.get()
            actual = app.input._point
            dx, dy = px - actual[0], py - actual[1]
            if abs(dx) <= 1 and abs(dy) <= 1:
                return
            dx, dy = max(-12, min(12, dx)), max(-12, min(12, dy))
            # SendInput is not a substitute for hardware WM_INPUT. Feed the same raw
            # motion consumer while exercising real native output and echo filtering.
            app.input._capture_motion(dx, dy, 0, 0)
            yield 25
        raise AssertionError(f"Pointer did not converge on {(px, py)}; got {app.input._point}")

    def click():
        point = native.position()
        hit = native.user32.WindowFromPoint(_POINT(*point))
        assert top_level_hwnd(hit) == top_level_hwnd(app.practice.window.winfo_id()), (
            "Test target is obscured; refusing input", point,
        )
        mouse(0x0002)
        yield 50
        mouse(0x0004)
        yield 100

    def wait_for(condition, message, timeout=1.5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            yield 50
        assert condition(), message

    def exercise():
        yield 900
        yield from wait_for(lambda: app.frame_ready, "Initial preview did not arrive", timeout=5)
        assert app.frame_ready and app.practice is not None, app.status.get()
        target = app.practice
        app.control()
        yield 100
        assert app.input.active, app.status.get()
        native_send = app.input._native.send

        def trace_send(flags, data=0, point=(0, 0)):
            if flags & 0x0800:
                sent_wheels.append((flags, data, point, app.input._native.position()))
            return native_send(flags, data, point)

        app.input._native.send = trace_send
        yield from move_to_widget(target.counter)
        yield from click()
        yield from wait_for(lambda: target.count.get() == 1, "Click did not arrive")
        assert target.count.get() == 1, (
            "Click did not reach target", target.count.get(), button_events,
            native.position(), target.counter.winfo_rootx(), target.counter.winfo_rooty(),
            target.counter.winfo_width(), target.counter.winfo_height(),
        )
        print("PASS native click through preview", flush=True)
        yield from move_to_widget(target.entry)
        yield from click()
        key(ord("W"))
        key(ord("W"), up=True)
        yield from wait_for(lambda: target.text.get().lower() == "w", "Typing did not arrive")
        assert target.text.get().lower() == "w", repr(target.text.get())
        print("PASS native keyboard focus and typing", flush=True)
        yield from move_to_widget(target.drag_canvas, (100, 65))
        before = target.drag_canvas.coords("card")
        mouse(0x0002)
        yield 50
        yield from move_to_widget(target.drag_canvas, (170, 95))
        mouse(0x0004)
        yield 100
        after = target.drag_canvas.coords("card")
        assert after[0] > before[0] + 40 and after[1] > before[1] + 10, (before, after)
        print("PASS native drag through preview", flush=True)
        yield from move_to_widget(target.scroll_list)
        yield from click()
        before_scroll = target.scroll_list.yview()
        mouse(0x0800, data=-360)
        yield from wait_for(
            lambda: target.scroll_list.yview()[0] > before_scroll[0], "Wheel did not arrive",
        )
        assert target.scroll_list.yview()[0] > before_scroll[0], (
            target.scroll_list.yview(), raw_wheels, wheel_events, sent_wheels,
            str(root.focus_get()),
        )
        print("PASS native wheel through preview", flush=True)
        # Use actual keyboard events to test the registered emergency release shortcut.
        key(0x11)
        key(0x12)
        key(ord("Q"))
        key(ord("Q"), up=True)
        key(0x12, up=True)
        key(0x11, up=True)
        yield from wait_for(lambda: not app.input.active, "Release shortcut did not arrive")
        assert not app.input.active, "Ctrl+Alt+Q did not release"
        print("PASS native Ctrl+Alt+Q release", flush=True)
        # Practice uses display capture: a maximized viewer overlaps its target
        # and must pause instead of recursively capturing itself (also in CRD).
        root.state("zoomed")
        yield from wait_for(lambda: not app.frame_ready and 'Preview paused' in app.status.get(),
                            'Overlapping display preview did not pause')
        root.state("normal")
        yield from wait_for(lambda: app.frame_ready, 'Practice preview did not resume')
        app.control()
        yield 100
        assert app.input.active, app.status.get()
        # Deliberately stall the GUI; the independent hook thread must release input.
        time.sleep(2.3)
        assert not app.input.active, "GUI-stall watchdog did not release"
        yield 150
        print("PASS native GUI-stall release watchdog", flush=True)
        artifact = Path(__file__).resolve().parents[1] / "_work" / "monitor-control"
        artifact.mkdir(parents=True, exist_ok=True)
        # A single diagnostic screenshot; do not open a second DXGI duplicator
        # for the same process/output while the live preview owns one.
        with GdiCapture() as capture:
            ppm = capture.grab(
                root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height(),
                root.winfo_width(), root.winfo_height(),
            )
        shot = tk.PhotoImage(data=ppm, format="PPM", master=root)
        shot.write(str(artifact / "practice-smoke.png"), format="png")
        print(f"PASS viewer capture: {artifact / 'practice-smoke.png'}", flush=True)
        result["exit"] = 0

    steps = exercise()

    def advance():
        try:
            delay = next(steps)
        except StopIteration:
            app.close()
        except BaseException:  # noqa: BLE001 - always release native input on a failed test
            traceback.print_exc()
            print(f"Recent pointer events (proposed, current, preview): {movements[-10:]}")
            artifact = Path(__file__).resolve().parents[1] / "_work" / "monitor-control"
            artifact.mkdir(parents=True, exist_ok=True)
            if app.image is not None:
                app.image.write(str(artifact / "practice-failure.png"), format="png")
            app.close()
        else:
            root.after(delay, advance)

    root.after(100, advance)
    root.after(30000, lambda: (print("FAIL integration deadline", flush=True), app.close()))
    try:
        root.mainloop()
    finally:
        app.close()
        try:
            native.restore(initial_cursor)
        except RuntimeError as exc:
            print(f"Pointer restore: {exc}", flush=True)
        native.user32.SetForegroundWindow(initial_focus)
    return result["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
