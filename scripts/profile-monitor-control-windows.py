"""Opt-in timing test of native wheel redirection to our own practice widget.

Temporarily controls the pointer: do not use it during this short measurement.
Simulated wheel input exercises real hooks and SendInput, but cannot validate a
physical touchpad driver. Restores the pointer/focus and writes profile.json to
_work/monitor-control. Never run as part of unattended unit tests.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import threading
import time
import tkinter as tk
from itertools import pairwise
from pathlib import Path

from wcfi_tools.monitor_control.app import MonitorApp
from wcfi_tools.monitor_control.capture import Capture, enable_dpi_awareness, top_level_hwnd
from wcfi_tools.monitor_control.input import _INPUT, _MOUSEINPUT, _POINT, _Win32
from wcfi_tools.monitor_control.windows import AppWindow


def summary(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values), "median_ms": round(statistics.median(values) * 1000, 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))] * 1000, 2),
        "max_ms": round(max(values) * 1000, 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10,
                        help="Duration of the scrolling/preview phase (default: 10).")
    parser.add_argument("--window", action="store_true", help="Measure window capture instead of DXGI.")
    parser.add_argument("--dialog", action="store_true",
                        help="Capture an owned dialog together with its parent (implies --window).")
    options = parser.parse_args()
    options.window = options.window or options.dialog
    seconds = options.seconds
    if not 1 <= seconds <= 60:
        parser.error("--seconds must be between 1 and 60")
    enable_dpi_awareness()
    native = _Win32()
    native.user32.GetForegroundWindow.restype = ctypes.c_void_p
    native.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    native.user32.WindowFromPoint.argtypes = [_POINT]
    native.user32.WindowFromPoint.restype = ctypes.c_void_p
    initial_focus = native.user32.GetForegroundWindow()
    initial_cursor = native.position()
    paused = False
    original_tick = MonitorApp._tick

    def tick(self):
        if paused:
            if not self.closed:
                self.capture.pause()
                self.root.after(16, self._tick)
        else:
            original_tick(self)

    MonitorApp._tick = tick
    root = tk.Tk()
    app = MonitorApp(root, practice=True, fps=60)
    if options.window:
        # The benchmark HWND belongs to the viewer's process, which normal
        # application discovery deliberately excludes.
        app.discovery.close()
        app.discovery.take = lambda: None
        target = AppWindow(top_level_hwnd(app.practice.window.winfo_id()), os.getpid(),
                           "Window capture benchmark")
        app.windows.append(target)
        app._populate(window_key=target.key)
    root.title("WCFI · Scroll timing measurement")
    widget = app.practice.scroll_list
    if options.dialog:
        dialog = tk.Toplevel(app.practice.window)
        dialog.title("WCFI benchmark owned dialog")
        dialog.transient(app.practice.window)
        dialog.geometry(f"350x240+{app.practice.window.winfo_rootx() + 50}"
                        f"+{app.practice.window.winfo_rooty() + 100}")
        widget = tk.Listbox(dialog, font=("Segoe UI", 11))
        widget.pack(fill="both", expand=True, padx=20, pady=20)
    widget_hwnd = widget.winfo_id()
    widget.delete(0, "end")
    # Keep the target moving throughout longer runs instead of hitting its end.
    for index in range(max(10000, round(seconds * 1000))):
        widget.insert("end", f"Timing row {index}")
    posted, hook_times, forward_times = [], [], []
    latencies, captures, uploads, frame_starts, injection_calls, forward_calls = [], [], [], [], [], []
    present_times, frame_ages = [], []
    stop = threading.Event()
    producer = None
    post_errors = []
    received_units = 0
    recording = False
    measurement = 0
    results = {
        "capture_backend": "Windows Graphics Capture" if options.window else "DXGI Desktop Duplication",
        "tk_version": str(root.tk.call("info", "patchlevel")),
        "frame_clock": time.get_clock_info("perf_counter").implementation,
        "filter": "HALFTONE", "requested_fps": 60, "scroll_seconds": seconds,
        "presentation_measurement": "Fresh images submitted to Tk, not physical panel scan-out",
        "phases": {},
    }
    original_grab = Capture.grab
    original_grab_window = Capture.grab_window
    original_grab_scene = Capture.grab_scene
    original_photo = tk.PhotoImage
    original_present = app._present_frame

    def timed_grab(original, capture, *args):
        began = time.perf_counter()
        current_measurement = measurement if recording else None
        result = original(capture, *args)
        if recording and current_measurement == measurement:
            frame_starts.append(began)
            captures.append(time.perf_counter() - began)
        return result

    class TimedPhoto(original_photo):
        def __init__(self, *args, **kwargs):
            began = time.perf_counter()
            super().__init__(*args, **kwargs)
            if recording:
                uploads.append(time.perf_counter() - began)

        def configure(self, **kwargs):
            began = time.perf_counter()
            result = super().configure(**kwargs)
            if recording:
                uploads.append(time.perf_counter() - began)
            return result

    def present(frame, target):
        original_present(frame, target)
        if recording:
            present_times.append(time.perf_counter())
            frame_ages.append(time.perf_counter() - frame.started_at)

    Capture.grab = lambda capture, *args: timed_grab(original_grab, capture, *args)
    Capture.grab_window = lambda capture, *args: timed_grab(original_grab_window, capture, *args)
    Capture.grab_scene = lambda capture, *args: timed_grab(original_grab_scene, capture, *args)
    app._present_frame = present
    tk.PhotoImage = TimedPhoto

    def received(event):
        nonlocal received_units
        if not recording:
            return
        count = -event.delta
        if count <= 0 or received_units + count > len(posted):
            post_errors.append("Unexpected wheel input during measurement")
            stop.set()
            return
        now = time.perf_counter()
        latencies.extend(now - stamp for stamp in posted[received_units:received_units + count])
        received_units += count

    widget.bind("<MouseWheel>", received, add="+")

    capture_event = app.input._capture_event

    def on_event(message, point, data):
        if recording and message == 0x020A:
            count = -ctypes.c_int16(data >> 16).value
            hook_times.extend([time.perf_counter()] * max(0, count))
        return capture_event(message, point, data)

    app.input._capture_event = on_event

    def mouse(flags, data=0):
        event = _INPUT(type=0)
        event.mi = _MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0x57435454)
        if native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("Test input was blocked")

    prepare_deadline = time.perf_counter() + 8

    def prepare():
        if not app.frame_ready or time.monotonic() < app.settle_until:
            if time.perf_counter() < prepare_deadline:
                root.after(50, prepare)
                return
            post_errors.append("Preview unavailable")
            app.close()
            return
        preview = app._preview_rect()
        point = preview[0] + preview[2] // 2, preview[1] + preview[3] // 2
        if top_level_hwnd(native.user32.WindowFromPoint(_POINT(*point))) != top_level_hwnd(
            root.winfo_id(),
        ):
            post_errors.append("Viewer preview is obscured; refusing input")
            app.close()
            return
        from wcfi_tools.monitor_control.input import normalize_absolute

        event = _INPUT(type=0)
        event.mi = _MOUSEINPUT(*normalize_absolute(point, native.desktop()), 0, 0xC001, 0, 0x57435454)
        if native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError('Test hover input was blocked')
        root.after(150, prepare_controlled)

    def prepare_controlled():
        if not app.input.active:
            post_errors.append(app.status.get())
            app.close()
            return
        target, preview = app.active_geometry
        results["target_size"] = list(target[2:])
        results["preview_size"] = list(preview[2:])
        results["captured_windows"] = (len(app.presented_scene.layers)
                                       if app.presented_scene is not None else 0)
        tx = widget.winfo_rootx() + widget.winfo_width() // 2
        ty = widget.winfo_rooty() + widget.winfo_height() // 2
        px = round((tx - target[0]) * (preview[2] - 1) / (target[2] - 1))
        py = round((ty - target[1]) * (preview[3] - 1) / (target[3] - 1))
        app.input._capture_motion(px - app.input._point[0], py - app.input._point[1], 0, 0)
        original_send = app.input._native.send

        def send(flags, data=0, point=(0, 0)):
            began = time.perf_counter()
            if recording and flags == 0x0800:
                forward_times.extend([began] * max(0, -data))
            result = original_send(flags, data, point)
            if recording and flags == 0x0800:
                forward_calls.append(time.perf_counter() - began)
            return result

        app.input._native.send = send
        root.after(150, focus_target)

    def safe_target():
        hit = native.user32.WindowFromPoint(_POINT(*native.position()))
        return hit == widget_hwnd

    def focus_target():
        if not safe_target():
            post_errors.append("Practice target is obscured; refusing input")
            app.close()
            return
        mouse(0x0002)
        mouse(0x0004)
        root.after(150, lambda: begin("preview_paused", False))

    def begin(name, capture):
        nonlocal paused, producer, received_units, recording, measurement
        paused = not capture
        if capture:
            # The benchmark deliberately disables _tick; reset its stale-frame
            # deadline when restoring it so the intentional pause isn't a fault.
            app.last_capture_at = time.perf_counter()
        measurement += 1
        for values in (posted, hook_times, forward_times, latencies, captures, uploads,
                       frame_starts, injection_calls, forward_calls, present_times, frame_ages):
            values.clear()
        received_units = 0
        recording = True
        widget.yview_moveto(0)
        stop.clear()

        def post_wheels():
            while not stop.is_set():
                if (name != "direct" and not app.input.active) or not safe_target():
                    post_errors.append("Pointer left our test widget; stopped injection")
                    break
                began = time.perf_counter()
                posted.extend([began] * 120)
                try:
                    mouse(0x0800, -120)
                except RuntimeError as exc:
                    post_errors.append(str(exc))
                    break
                injection_calls.append(time.perf_counter() - began)
                time.sleep(.008)

        producer = threading.Thread(target=post_wheels, daemon=True)
        producer.start()
        root.after(round(seconds * 1000) if name == "preview_running" else 1800,
                   lambda: finish(name))

    def finish(name):
        stop.set()
        producer.join(timeout=.5)
        root.after(250, lambda: report(name))

    def report(name):
        nonlocal recording
        recording = False
        phase = {
            "injection_to_widget": summary(latencies),
            "injection_to_hook": summary([b - a for a, b in zip(posted, hook_times)]),
            "hook_to_forward": summary([b - a for a, b in zip(hook_times, forward_times)]),
            "source_SendInput_call": summary(injection_calls),
            "forward_SendInput_call": summary(forward_calls),
            "capture": summary(captures), "photo_conversion": summary(uploads),
            "frame_interval": summary([b - a for a, b in pairwise(frame_starts)]),
            "present_interval": summary([b - a for a, b in pairwise(present_times)]),
            "present_fps": round((len(present_times) - 1) / (present_times[-1] - present_times[0]), 2)
            if len(present_times) > 1 else 0,
            "capture_start_to_present": summary(frame_ages),
            "sent_units": len(posted), "unreceived_units": len(posted) - received_units,
            "post_errors": list(post_errors),
            "rows_scrolled": round(widget.yview()[0] * widget.size()),
        }
        results["phases"][name] = phase
        print(name, json.dumps(phase), flush=True)
        if name == "preview_paused":
            root.after(250, lambda: begin("preview_running", True))
        elif name == "preview_running":
            app.release()
            native.restore((widget.winfo_rootx() + widget.winfo_width() // 2,
                            widget.winfo_rooty() + widget.winfo_height() // 2))
            root.after(150, lambda: begin("direct", False))
        else:
            output = Path(__file__).resolve().parents[1] / "_work/monitor-control/profile.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(results, indent=2), encoding="utf-8")
            app.close()

    root.after(800, prepare)
    root.after(round((seconds + 20) * 1000), app.close)
    try:
        root.mainloop()
    finally:
        stop.set()
        if producer:
            producer.join(timeout=.5)
        app.close()
        tk.PhotoImage = original_photo
        Capture.grab = original_grab
        Capture.grab_window = original_grab_window
        Capture.grab_scene = original_grab_scene
        MonitorApp._tick = original_tick
        native.restore(initial_cursor)
        native.user32.SetForegroundWindow(initial_focus)
    if post_errors:
        print(post_errors, flush=True)
    complete = "direct" in results["phases"]
    valid = all(p["unreceived_units"] == 0 and p["sent_units"] > 0
                for p in results["phases"].values())
    return 0 if complete and valid and not post_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
