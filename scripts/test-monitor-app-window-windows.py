"""Opt-in application picker test against a separate, disposable Tk process.

Moves the pointer briefly. Never operates existing user applications. Restores
the pointer/focus and closes only the two test processes' windows afterward.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path

from wcfi_tools.monitor_control import app as viewer_module
from wcfi_tools.monitor_control.app import MonitorApp, overlaps
from wcfi_tools.monitor_control.capture import (
    GdiCapture,
    Monitor,
    enable_dpi_awareness,
    top_level_hwnd,
)
from wcfi_tools.monitor_control.catalog import APPLICATIONS, DISPLAYS
from wcfi_tools.monitor_control.input import _INPUT, _KEYBDINPUT, _MOUSEINPUT, _POINT, _Win32
from wcfi_tools.monitor_control.window_scene import belongs_to_window
from wcfi_tools.monitor_control.windows import AppWindow, _api, window_rect


def fixture():
    enable_dpi_awareness()
    root = tk.Tk()
    root.title("WCFI application discovery fixture")
    root.geometry("450x600+80+80")
    root.attributes("-topmost", False)
    # Exercise a layered window with ordinary SRCCOPY capture as well.
    root.attributes("-alpha", .99)
    canvas = tk.Canvas(root, bg="#119944", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(30, 70, text="Click through the preview", anchor="w",
                       font=("Segoe UI", 18), fill="white")
    def clicked(_event):
        canvas.configure(bg="#2277aa")
        print("clicked", flush=True)

    canvas.bind("<ButtonRelease-1>", clicked)
    entry = tk.Entry(root, font=("Segoe UI", 12))
    entry.place(x=20, y=110, width=300, height=32)
    scroll = tk.Listbox(root, font=("Segoe UI", 11))
    scroll.place(x=20, y=170, width=400, height=140)
    for i in range(100):
        scroll.insert("end", f"Scroll row {i}")
    dialog = None
    dialog_entry = None

    def open_dialog(position=None):
        nonlocal dialog, dialog_entry
        dialog = tk.Toplevel(root)
        dialog.title("WCFI owned test dialog")
        dialog.transient(root)
        # Extend outside the parent's left/top bounds; the scene must expand.
        x, y = position or (max(0, root.winfo_rootx() - 60), max(0, root.winfo_rooty() - 80))
        dialog.geometry(f"320x180{x:+}{y:+}")
        dialog.configure(bg="#bd44dd")
        dialog_entry = tk.Entry(dialog)
        dialog_entry.place(x=20, y=30, width=250, height=30)
        def accept():
            print("dialog-text " + dialog_entry.get(), flush=True)
            dialog.destroy()
            root.focus_force()
        tk.Button(dialog, text="Accept", command=accept).place(x=20, y=90, width=100, height=35)
        dialog.grab_set()
        dialog_entry.focus_set()

    menu = tk.Menu(root, tearoff=False, font=("Segoe UI", 12))
    menu.add_command(label="Test menu command", command=lambda: print("menu-clicked", flush=True))
    submenu = tk.Menu(menu, tearoff=False, font=("Segoe UI", 12))
    submenu.add_command(label="Open owned dialog", command=open_dialog)
    menu.add_cascade(label="More test actions", menu=submenu)
    canvas.bind("<ButtonRelease-3>", lambda event: menu.tk_popup(event.x_root, event.y_root))
    commands = queue.Queue()

    def read_commands():
        for line in sys.stdin:
            commands.put(line.strip())

    threading.Thread(target=read_commands, daemon=True).start()

    def poll():
        while not commands.empty():
            command = commands.get_nowait()
            if command == "minimize":
                root.iconify()
            elif command == "rename":
                root.title("WCFI renamed discovery fixture")
            elif command == "resize":
                root.geometry("650x430")
            elif command == "resize-again":
                root.geometry("620x410")
            elif command == "move":
                root.geometry(f"+{root.winfo_x() + 20}+{root.winfo_y() + 15}")
            elif command.startswith("dialog-at "):
                _, x, y = command.split()
                open_dialog((int(x), int(y)))
            elif command == "close":
                root.destroy()
                return
            elif command == "state":
                print(json.dumps({
                    "text": entry.get(), "scroll": scroll.yview()[0],
                    "entry": [entry.winfo_rootx() + 80, entry.winfo_rooty() + 15],
                    "list": [scroll.winfo_rootx() + 80, scroll.winfo_rooty() + 40],
                    "canvas": [canvas.winfo_rootx() + canvas.winfo_width() - 50,
                               canvas.winfo_rooty() + 80],
                    "dialog_entry": ([dialog_entry.winfo_rootx() + 40,
                                      dialog_entry.winfo_rooty() + 15]
                                     if dialog is not None and dialog.winfo_exists() else None),
                    "dialog_accept": ([dialog.winfo_rootx() + 60, dialog.winfo_rooty() + 105]
                                      if dialog is not None and dialog.winfo_exists() else None),
                }), flush=True)
        root.after(20, poll)

    root.after(20, poll)
    root.after(200, lambda: print(f"ready {os.getpid()}", flush=True))
    root.after(120000, root.destroy)
    root.mainloop()
    return 0


def main():
    enable_dpi_awareness()
    native = _Win32()
    class CursorInfo(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('flags', ctypes.c_uint32),
                    ('handle', ctypes.c_void_p), ('position', _POINT)]

    def cursor_visible():
        info = CursorInfo(size=ctypes.sizeof(CursorInfo))
        assert native.user32.GetCursorInfo(ctypes.byref(info))
        return bool(info.flags & 1)

    initial_cursor = native.position()
    native.user32.GetForegroundWindow.restype = ctypes.c_void_p
    native.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    native.user32.WindowFromPoint.argtypes = [_POINT]
    native.user32.WindowFromPoint.restype = ctypes.c_void_p
    initial_focus = native.user32.GetForegroundWindow()
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--fixture"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    messages = queue.Queue()

    def read_messages():
        for line in process.stdout:
            messages.put(line.strip())

    threading.Thread(target=read_messages, daemon=True).start()
    root = tk.Tk()
    app = MonitorApp(root)
    recovery_calls = []
    def recover():
        recovery_calls.append(time.perf_counter())
        app.recover()
    app.recover_button.configure(command=recover)
    hover_events = []

    def trace_hover(event):
        hover_events.append((str(event.widget), event.x_root, event.y_root, event.state,
                             app.hover_blocked, app.frame_ready, app.active_geometry is not None))
        del hover_events[:-8]

    root.bind('<Motion>', trace_hover, add='+')
    success = False

    def command(value):
        process.stdin.write(value + "\n")
        process.stdin.flush()

    def wait_for(predicate, label):
        deadline = time.perf_counter() + 10
        while not predicate() and time.perf_counter() < deadline:
            yield 50
        assert predicate(), (label, app.status.get())

    def start_control(button=None):
        if button is None:
            # Re-entry after emergency/focus loss is a deliberate boundary crossing.
            pointer_at((root.winfo_rootx() + 200, root.winfo_rooty() + 40))
            yield 100
            preview = app._preview_rect()
            point = preview[0] + preview[2] // 2, preview[1] + preview[3] // 2
            hit = native.user32.WindowFromPoint(_POINT(*point))
            assert top_level_hwnd(hit) == top_level_hwnd(root.winfo_id()), "Viewer is obscured"
            pointer_at(point)
            yield 100
            yield from wait_for(lambda: app.input.active,
                                f"Hover did not start control: {hover_events}")
            return
        point = (button.winfo_rootx() + button.winfo_width() // 2,
                 button.winfo_rooty() + button.winfo_height() // 2)
        pointer_at(point)
        yield 100
        hit = native.user32.WindowFromPoint(_POINT(*point))
        assert top_level_hwnd(hit) == top_level_hwnd(root.winfo_id()), "Viewer is obscured"
        # Exercise a real user click so Windows grants normal foreground activation.
        for flags in (0x0002, 0x0004):
            event = _INPUT(type=0)
            event.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0x57435454)
            assert native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1
            yield 60

    def state():
        command("state")
        yield from wait_for(lambda: not messages.empty(), "Fixture state unavailable")
        return json.loads(messages.get_nowait())

    def move(point):
        target, preview = app.active_geometry
        desired = (round((point[0] - target[0]) * (preview[2] - 1) / (target[2] - 1)),
                   round((point[1] - target[1]) * (preview[3] - 1) / (target[3] - 1)))
        app.input._capture_motion(desired[0] - app.input._point[0],
                                  desired[1] - app.input._point[1], 0, 0)
        yield 100
        hit = native.user32.WindowFromPoint(_POINT(*native.position()))
        user32, _ = _api()
        assert belongs_to_window(user32, hit, app._selected().hwnd), (
            "Test application is obscured", app.input.active, app.status.get(),
            point, native.position(), hit, app._selected().hwnd,
        )

    def mouse(flags, data=0, point=(0, 0)):
        event = _INPUT(type=0)
        event.mi = _MOUSEINPUT(*point, data & 0xFFFFFFFF, flags, 0, 0x57435454)
        assert native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1

    def pointer_at(point):
        from wcfi_tools.monitor_control.input import normalize_absolute

        mouse(0xC001, point=normalize_absolute(point, native.desktop()))

    def key(vk, up=False):
        event = _INPUT(type=1)
        event.ki = _KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0x57435454)
        assert native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1

    def exercise():
        nonlocal success
        yield 900
        assert app.practice is None, "Default launch must not open the practice fixture"
        yield from wait_for(lambda: not messages.empty(), "Fixture did not start")
        ready = messages.get_nowait()
        assert ready.startswith("ready "), ready
        fixture_pid = int(ready.split()[1])
        yield from wait_for(lambda: any(isinstance(w, AppWindow) and w.pid == fixture_pid
                                       for w in app.targets), "Automatic discovery missed fixture")
        index = next(i for i, w in enumerate(app.targets)
                     if isinstance(w, AppWindow) and w.pid == fixture_pid)
        # Give the test viewer a real activation click. Windows may reject a
        # foreground request from this background test runner; the product no
        # longer pins itself above the user's foreground app on startup.
        root.attributes("-topmost", True)
        yield 150
        pointer_at((root.winfo_rootx() + 300, root.winfo_rooty() + 20))
        mouse(0x0002)
        mouse(0x0004)
        yield 150
        root.attributes("-topmost", False)
        app.picker.current(index)
        app._select()
        selected = app._selected()
        yield 200
        assert not app.selector.winfo_ismapped()
        assert app.switch_button.winfo_ismapped()
        expanded_height = app.canvas.winfo_height()
        app.toggle_selector()
        yield 200
        assert app.selector.winfo_ismapped()
        assert app.canvas.winfo_height() < expanded_height - 100
        app._populate()
        assert app.selector_open, "Background refresh closed the picker"
        app.toggle_selector()
        yield 200
        assert app.canvas.winfo_height() == expanded_height
        assert not _api()[0].GetWindowLongPtrW(top_level_hwnd(root.winfo_id()), -20) & 8
        print("PASS selection expands preview; Switch restores picker; viewer is not always on top",
              flush=True)
        app.search.set("discovery fixture")
        app.mode.set(DISPLAYS)
        app._switch_view()
        assert not any(isinstance(w, AppWindow) for w in app.targets)
        app.mode.set(APPLICATIONS)
        app._switch_view()
        assert app._selected().key == selected.key
        assert app.search.get() == "discovery fixture"
        app.search.set("")
        # Check the compact layout and save only this test viewer's own pixels.
        root.geometry("520x540")
        yield 250
        assert app.status_label.winfo_y() + app.status_label.winfo_height() <= root.winfo_height()
        assert app.recover_button.winfo_x() + app.recover_button.winfo_width() < root.winfo_width()
        output = Path(__file__).resolve().parents[1] / "_work/monitor-control"
        output.mkdir(parents=True, exist_ok=True)
        with GdiCapture() as screenshot:
            ppm = screenshot.grab(root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(),
                                  root.winfo_height(), root.winfo_width(), root.winfo_height())
        tk.PhotoImage(data=ppm, format="PPM").write(str(output / "views-compact.png"), format="png")
        print("PASS separate views remember search/selection; compact controls remain visible",
              flush=True)
        root.focus_force()
        app.arrange()
        yield from wait_for(lambda: app.frame_ready, "Window preview did not become ready")
        yield 800  # Let restore/resize animation and the independent app's redraw finish.
        assert not overlaps(app._target_rect(), app._viewer_rect())
        user32, _ = _api()
        # Put the entire fixture behind the viewer, including its clickable center.
        assert user32.SetWindowPos(selected.hwnd, None, root.winfo_rootx() + 80,
                                   root.winfo_rooty() + 220, 600, 400, 0x0014)
        yield 500
        app.pass_through.raise_viewer()
        yield 900
        assert overlaps(app._target_rect(), app._viewer_rect())
        bounds = app._target_rect()
        center = bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2
        hit = native.user32.WindowFromPoint(_POINT(*center))
        assert top_level_hwnd(hit) == top_level_hwnd(root.winfo_id()), (
            "Fixture is not covered", center, app._viewer_rect(), bounds,
            top_level_hwnd(hit), top_level_hwnd(root.winfo_id()), selected.hwnd,
            app.status.get(), app.pass_through._original,
        )
        # Green must survive layered-window capture and image conversion.
        rgb = app.image.get(app.image.width() * 9 // 10, app.image.height() // 4)
        assert rgb[1] > 120 and rgb[0] < 40 and rgb[2] < 90, rgb
        print("PASS application discovery and live preview while fully covered by viewer", flush=True)
        command("rename")
        yield from wait_for(lambda: "renamed" in app._selected().title,
                            "Automatic title update missed fixture")
        app.search.set("no matching fixture title")
        assert app.targets == [] and app._selected().key == selected.key
        app.search.set("")
        assert app._selected().key == selected.key
        command("resize")
        yield from wait_for(lambda: app.frame_ready and app.last_target[2] != bounds[2],
                            "Resized HWND did not produce matching pixels")
        command("move")
        yield from wait_for(lambda: app.frame_ready and app.last_target[0] != bounds[0],
                            "Moved HWND did not update preview coordinates")
        bounds = app._target_rect()
        print("PASS live title/search updates preserve selection; resize/move preview follows",
              flush=True)
        if '--hotplug-only' in sys.argv:
            screen = app.monitors[0]
            split = screen.width // 2
            left = Monitor('sim-left', 'Laptop', screen.left, screen.top,
                           split, screen.height, True, 'sim-laptop-id')
            right = Monitor('sim-right', 'External', screen.left + split, screen.top,
                            screen.width - split, screen.height, False, 'sim-external-id')
            topology = [left, right]
            original_enumerator = viewer_module.enumerate_monitors
            viewer_module.enumerate_monitors = lambda: list(topology)
            try:
                yield from start_control()  # Only the disposable application is controlled.
                app._update_monitors()
                assert not app.input.active and app.pass_through._original is None
                pointer_at((root.winfo_rootx() + 200, root.winfo_rooty() + 40))
                app._populate(device=right.device)
                yield from wait_for(lambda: app.frame_ready, 'Simulated external preview missing')
                for cycle in range(3):
                    topology[:] = [left]
                    app._update_monitors()
                    assert not app.frame_ready and app.presented_scene is None
                    assert not app.catalog.current().available
                    yield 700
                    assert 'disconnected' in app.status.get().lower()
                    assert not app.input.active
                    # Include a no-displays interval, as can occur during wake/driver reset.
                    topology[:] = []
                    app._update_monitors()
                    yield 700
                    assert not app.frame_ready and not app.input.active
                    right = Monitor(f'sim-returned-{cycle}', 'External', screen.left + split,
                                    screen.top, screen.width - split, screen.height, False,
                                    'sim-external-id')
                    topology[:] = [left, right]
                    app._update_monitors()
                    yield from wait_for(lambda: app.frame_ready, 'Reconnected preview did not resume')
                    assert app._selected() == right
                    assert not app.input.active
                print('PASS three disconnect/no-display/renumbered-reconnect cycles; '
                      'fresh preview resumes with input released', flush=True)
            finally:
                viewer_module.enumerate_monitors = original_enumerator
                app._update_monitors()
            app._populate(window_key=selected.key)
            yield from wait_for(lambda: app.frame_ready, 'Application preview did not recover')
            print('PASS application selection and preview remain usable after topology recovery',
                  flush=True)
            success = True
            return
        if '--placement-only' in sys.argv:
            app._set_selector(True)
            from wcfi_tools.monitor_control.placement import destination_work_area
            work = destination_work_area(root.winfo_id())
            def on_screen(layer):
                x, y, width, height = layer.bounds
                return (work[0] <= x < work[0] + work[2]
                        and work[1] <= y < work[1] + work[3]
                        and x + width <= work[0] + work[2]
                        and y + height <= work[1] + work[3])
            # Simulate coordinates left behind by an unplugged display without
            # changing Windows display settings or touching any personal apps.
            assert user32.SetWindowPos(selected.hwnd, None, 12000, -4000, 700, 450, 0x0014)
            yield from wait_for(lambda: not app.frame_ready, 'Offscreen frame was not rejected')
            command('dialog-at 12500 -3800')
            yield 400
            yield from start_control(app.bring_button)
            yield from wait_for(lambda: app.frame_ready and app.presented_scene is not None
                                and len(app.presented_scene.layers) == 2,
                                'Bring to laptop did not recover the stranded family')
            assert all(on_screen(layer) for layer in app.presented_scene.layers)
            assert app._selected().key == selected.key
            print('PASS Bring to laptop rescues offscreen app and owned dialog', flush=True)
            command('minimize')
            yield from wait_for(lambda: not app.frame_ready, 'Minimized frame was not rejected')
            app.move_selected(app.monitors[0].device)
            yield from wait_for(lambda: app.frame_ready, 'Move to display did not restore preview')
            assert all(on_screen(layer) for layer in app.presented_scene.layers)
            assert app._selected().key == selected.key
            print('PASS Move to display restores minimized app without changing selection', flush=True)
            app.move_selected('disconnected-test-device')
            assert 'disconnected' in app.status.get().lower()
            assert not app.input.active and app.pass_through._original is None
            print('PASS disconnected destination rejected safely', flush=True)
            success = True
            return
        if '--recovery-only' in sys.argv:
            for cycle in range(3):
                yield from start_control()
                root.focus_force()
                yield from wait_for(
                    lambda: not app.input.active and app.pass_through._original is None,
                    'Focus loss did not restore viewer hit testing',
                )
                yield 200
                assert not app.input.active, 'Unexpected automatic focus reacquisition'
                yield from start_control()
                command('minimize')
                yield from wait_for(lambda: not app.input.active and not app.frame_ready,
                                    'Minimized target did not release control')
                yield from start_control(app.recover_button)
                assert len(recovery_calls) == cycle + 1, (
                    'Recovery button did not receive the test click', cycle,
                    native.position(),
                    (app.recover_button.winfo_rootx(), app.recover_button.winfo_rooty(),
                     app.recover_button.winfo_width(), app.recover_button.winfo_height()),
                    app.recover_button.cget('state'), app.status.get(),
                )
                yield from wait_for(lambda: app.frame_ready,
                                    f'Restore cycle {cycle + 1} did not produce a fresh preview')
                assert app._selected().key == selected.key
                assert app._target_rect() == bounds, 'Recovery changed application layout'
                yield 300
            yield from start_control()
            command('close')
            yield from wait_for(lambda: not app.input.active and not app.frame_ready,
                                'Closed target did not release control')
            print('PASS repeated focus loss, minimize/Recover, and closed-window release',
                  flush=True)
            success = True
            return
        if '--edge-only' in sys.argv:
            for size in ('1100x800', '700x700', '520x540'):
                root.geometry(size)
                yield from wait_for(lambda: app.frame_ready, 'Resized preview unavailable')
                yield 300
                for side in ('left', 'right', 'top', 'bottom'):
                    yield from start_control()
                    yield 250  # Include layout changes caused by active status text.
                    preview = app._preview_rect()
                    px, py = app.input._point
                    dx, dy = {
                        'left': (-px - 50, 0), 'right': (preview[2] - px + 50, 0),
                        'top': (0, -py - 50), 'bottom': (0, preview[3] - py + 50),
                    }[side]
                    app.input._capture_motion(dx, dy, 0, 0)
                    yield from wait_for(lambda: not app.input.active and app.active_geometry is None,
                                        'Edge exit did not release')
                    yield 300  # Check against the image AFTER idle layout settles.
                    shown = app._preview_rect()
                    axis = 0 if side in ('left', 'right') else 1
                    expected = shown[axis] + (
                        -1 if side in ('left', 'top') else shown[axis + 2]
                    )
                    assert not app.input.active, ('Exit re-entered after layout', size, side)
                    assert native.position()[axis] == expected, (
                        'Visible edge moved after release', size, side, preview, shown,
                        native.position(), expected,
                    )
                print(f'PASS displayed edge after idle layout settles: {size}', flush=True)
            success = True
            return
        yield from start_control()
        yield from wait_for(lambda: app.input.active, "Window control did not start")
        yield 100
        assert not cursor_visible(), 'Real target pointer was not hidden'
        # Leave the image without a shortcut, then re-enter. Validate native hit
        # testing immediately, including balanced release during a drag.
        for side in ('left', 'right', 'top', 'bottom'):
            preview = app._preview_rect()
            px, py = app.input._point
            dx, dy, expected = {
                'left': (-px - 500, 0, (preview[0] - 1, round(preview[1] + py))),
                'right': (preview[2] - px + 500, 0,
                          (preview[0] + preview[2], round(preview[1] + py))),
                'top': (0, -py - 500, (round(preview[0] + px), preview[1] - 1)),
                'bottom': (0, preview[3] - py + 500,
                           (round(preview[0] + px), preview[1] + preview[3])),
            }[side]
            app.input._capture_motion(dx, dy, 0, 0)
            yield from wait_for(lambda: not app.input.active and app.active_geometry is None,
                                "Leaving the preview did not return normal pointer use")
            assert native.position() == expected, (side, native.position(), expected)
            assert cursor_visible(), 'Pointer was not shown after returning to the viewer'
            assert app.pass_through._original is None
            yield from start_control()
        print('PASS all four image-edge returns despite 500-pixel movement overshoot', flush=True)
        print('PASS hover entry, edge exit, and re-entry without buttons or shortcuts', flush=True)
        print('PASS target cursor hidden during interaction and visible at viewer exit', flush=True)
        positions = yield from state()
        yield from move(positions["canvas"])
        hit = native.user32.WindowFromPoint(_POINT(*native.position()))
        assert top_level_hwnd(hit) == selected.hwnd, "Refusing to click outside the test fixture"
        for flags in (0x0002, 0x0004):
            event = _INPUT(type=0)
            event.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0x57435454)
            assert native.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1
            yield 60
        yield from wait_for(lambda: not messages.empty(), "Click was not received")
        assert messages.get_nowait() == "clicked"
        yield from wait_for(
            lambda: app.image.get(app.image.width() * 9 // 10, app.image.height() // 4)[2] > 140,
            "Covered preview did not update after interaction",
        )
        print("PASS click through preview to separate application process", flush=True)
        yield from move(positions["entry"])
        mouse(0x0002)
        yield 50
        mouse(0x0004)
        yield 100
        key(ord("W"))
        key(ord("W"), up=True)
        yield 150
        assert (yield from state())["text"].lower() == "w"
        yield from move(positions["list"])
        for _ in range(4):
            mouse(0x0800, -120)
            yield 40
        yield 100
        assert (yield from state())["scroll"] > 0
        print("PASS native typing and scrolling while target is covered", flush=True)
        command("move")
        yield from wait_for(lambda: app.frame_ready and app.last_target[0] != bounds[0],
                            "Controlled window movement did not update preview")
        assert app.input.active, app.status.get()
        bounds = app._target_rect()
        command("resize-again")
        yield from wait_for(lambda: app.frame_ready and app.last_target[2] != bounds[2],
                            "Controlled window resize did not update preview")
        assert app.input.active, app.status.get()
        bounds = app._target_rect()
        positions = yield from state()
        print("PASS controlled window movement and resize preserve input session", flush=True)
        yield from move((bounds[0] + 100, bounds[1] + 16))
        mouse(0x0002)
        yield 100
        for _ in range(3):
            app.input._capture_motion(8, 3, 0, 0)
            yield 100
        mouse(0x0004)
        yield from wait_for(lambda: app.frame_ready and app.last_target[:2] != bounds[:2],
                            "Dragging the title bar did not move the target")
        assert app.input.active, app.status.get()
        bounds = app._target_rect()
        positions = yield from state()
        print("PASS native title-bar drag through preview preserves control", flush=True)

        def wait_layers(count):
            yield from wait_for(lambda: app.input.active and app.presented_scene is not None
                                and len(app.presented_scene.layers) == count and app.frame_ready,
                                f"Expected {count} captured family windows")
            yield 150
            assert app.input.active, app.status.get()
            assert not cursor_visible(), 'Target cursor reappeared after a popup changed'

        def open_menu():
            yield from move(positions["canvas"])
            mouse(0x0008)
            yield 60
            mouse(0x0010)
            yield from wait_layers(2)

        def pixel_at(point):
            x, y, w, h = app.presented_scene.bounds
            return app.image.get(round((point[0] - x) * (app.image.width() - 1) / (w - 1)),
                                 round((point[1] - y) * (app.image.height() - 1) / (h - 1)))

        yield from open_menu()
        menu_bounds = app.presented_scene.layers[-1].bounds
        assert app.presented_scene.bounds != bounds, "Menu did not exercise expanded capture bounds"
        rgb = pixel_at((menu_bounds[0] + menu_bounds[2] - 10, menu_bounds[1] + 10))
        assert min(rgb) > 180, ("Native menu pixels are absent from preview", rgb)
        yield from move((menu_bounds[0] + 40, menu_bounds[1] + 15))
        mouse(0x0002)
        yield 60
        mouse(0x0004)
        yield from wait_for(lambda: not messages.empty(), "Menu command was not invoked")
        assert messages.get_nowait() == "menu-clicked"
        yield from wait_layers(1)
        print("PASS native context menu outside parent bounds without release", flush=True)
        yield from open_menu()
        menu_bounds = app.presented_scene.layers[-1].bounds
        yield from move((menu_bounds[0] + 40, menu_bounds[1] + 43))
        yield from wait_layers(3)
        submenu_bounds = app.presented_scene.layers[-1].bounds
        yield from move((submenu_bounds[0] + 40, submenu_bounds[1] + 15))
        mouse(0x0002)
        yield 60
        mouse(0x0004)
        yield from wait_layers(2)
        yield 300
        dialog_state = yield from state()
        assert dialog_state["dialog_entry"] is not None, dialog_state
        dialog_bounds = app.presented_scene.layers[-1].bounds
        rgb = pixel_at((dialog_bounds[0] + dialog_bounds[2] - 20,
                        dialog_bounds[1] + dialog_bounds[3] - 20))
        assert rgb[0] > 160 and rgb[1] < 100 and rgb[2] > 180, ("Dialog not captured", rgb)
        yield from move(dialog_state["dialog_entry"])
        mouse(0x0002)
        yield 60
        mouse(0x0004)
        key(ord("D"))
        key(ord("D"), up=True)
        yield 150
        # Explicit release/resume must also preserve the root picker selection.
        app.release()
        yield 100
        yield from start_control(app.recover_button)
        yield from wait_for(lambda: app.frame_ready, "Dialog preview did not recover")
        assert app._selected() == selected
        yield from start_control()
        yield from wait_for(lambda: app.input.active, "Could not resume with owned dialog open")
        assert app._selected() == selected
        yield from move(dialog_state["dialog_accept"])
        mouse(0x0002)
        yield 60
        mouse(0x0004)
        yield from wait_for(lambda: not messages.empty(), "Dialog did not accept input")
        assert messages.get_nowait().lower() == "dialog-text d"
        yield from wait_layers(1)
        print("PASS cascading submenu and owned dialog; release/resume preserves selection",
              flush=True)
        if "--cross-display" in sys.argv:
            # Simulate two physical-pixel display rectangles inside one real
            # output. This exercises native HWND capture/input without changing
            # the user's display configuration or installing a virtual driver.
            original_enumerator = viewer_module.enumerate_monitors
            screen = next(m for m in app.monitors if m.primary)
            assert screen.width >= 1800 and screen.height >= 900, "Test needs a large output"
            seam = screen.left + screen.width // 2
            panels = [Monitor("test-left", "Simulated left", screen.left, screen.top,
                              seam - screen.left, screen.height, True),
                      Monitor("test-right", "Simulated offset right", seam, screen.top + 160,
                              screen.left + screen.width - seam, screen.height - 160, False)]
            def place(x, y):
                assert user32.SetWindowPos(selected.hwnd, None, x, y, 620, 410, 0x0014)
                yield 100
                expected = window_rect(selected)[:2]  # DWM omits invisible resize borders.
                yield from wait_for(lambda: app.frame_ready and app.last_target[:2] == expected,
                                    "Cross-display window position did not reach preview")
                assert app.input.active, app.status.get()
            try:
                viewer_module.enumerate_monitors = lambda: panels
                app._update_monitors()
                yield from wait_for(lambda: app.frame_ready, "Simulated layout preview missing")
                yield from start_control()
                yield from wait_for(lambda: app.input.active, "Could not control simulated layout")
                yield from place(seam - 300, screen.top + 80)
                rgb = pixel_at((seam + 20, screen.top + 100))
                assert max(rgb) < 30, ("Gap between displays was not masked", rgb)
                guard = app.window_guard
                assert guard((seam + 20, screen.top + 100)) is None
                yield from place(seam + 40, screen.top + 220)
                positions = yield from state()
                yield from move(positions["entry"])
                mouse(0x0002)
                yield 50
                mouse(0x0004)
                key(ord("X"))
                key(ord("X"), up=True)
                yield 100
                assert (yield from state())["text"].lower().endswith("x")
                yield from place(screen.left + 100, screen.top + 220)
                command(f"dialog-at {seam + 80} {screen.top + 320}")
                yield from wait_layers(2)
                dialog_state = yield from state()
                assert app.presented_scene.layers[-1].bounds[0] > seam
                rgb = pixel_at((seam + 100, screen.top + 470))
                assert rgb[0] > 160 and rgb[2] > 180, rgb
                yield from move(dialog_state["dialog_entry"])
                mouse(0x0002)
                yield 50
                mouse(0x0004)
                key(ord("C"))
                key(ord("C"), up=True)
                yield 100
                yield from move(dialog_state["dialog_accept"])
                mouse(0x0002)
                yield 50
                mouse(0x0004)
                yield from wait_for(lambda: not messages.empty(), "Cross-display dialog failed")
                assert messages.get_nowait().lower() == "dialog-text c"
                yield from wait_layers(1)
                assert app._selected().key == selected.key
                print("PASS simulated display crossing, gap masking, and dialog on other display "
                      "with native clicks/typing and no release", flush=True)
            finally:
                viewer_module.enumerate_monitors = original_enumerator
                app._update_monitors()
            yield from wait_for(lambda: app.frame_ready, "Original layout did not recover")
            bounds = app._target_rect()
            yield from start_control()
            yield from wait_for(lambda: app.input.active, "Original layout control did not resume")
        key(0x11)
        key(0x12)
        key(ord("Q"))
        key(ord("Q"), up=True)
        key(0x12, up=True)
        key(0x11, up=True)
        yield from wait_for(lambda: not app.input.active and app.pass_through._original is None,
                            "Emergency release did not restore the viewer")
        hit = native.user32.WindowFromPoint(_POINT(*center))
        assert top_level_hwnd(hit) == top_level_hwnd(root.winfo_id())
        print("PASS Ctrl+Alt+Q restores viewer hit testing", flush=True)
        yield from start_control()
        yield from wait_for(lambda: app.input.active, "Control did not resume")
        time.sleep(2.3)
        assert not app.input.active, "Input watchdog did not release during UI stall"
        yield from wait_for(lambda: app.pass_through._original is None,
                            "Viewer hit testing did not recover after UI stall")
        print("PASS window-mode watchdog and viewer recovery", flush=True)
        yield from start_control()
        yield from wait_for(lambda: app.input.active, "Control did not resume after stall")
        root.focus_force()
        yield from wait_for(lambda: not app.input.active and app.pass_through._original is None,
                            "Focus loss did not release control and restore the viewer")
        yield 200
        assert not app.input.active, "Control resumed without an explicit request"
        assert app._selected() == selected
        yield from start_control()
        yield from wait_for(lambda: app.input.active, "Could not resume after focus loss")
        print("PASS focus loss and explicit resume without restarting or reselecting", flush=True)
        command("minimize")
        yield from wait_for(lambda: not app.input.active and not app.frame_ready,
                            "Minimized target did not release control")
        print("PASS minimized application releases control", flush=True)
        assert app.pass_through._original is None, "Viewer stayed click-through after release"
        yield from start_control(app.recover_button)
        yield from wait_for(lambda: app.frame_ready, "Restored window did not resume preview")
        assert app._selected() == selected
        assert app._target_rect() == bounds, "Recovery changed the application's layout"
        yield from start_control()
        yield from wait_for(lambda: app.input.active, "Restored application did not accept control")
        command("close")
        yield from wait_for(lambda: not app.input.active and not app.frame_ready,
                            "Closed target did not release control")
        print("PASS restore and closed-window release", flush=True)
        success = True

    steps = exercise()

    def advance():
        try:
            delay = next(steps)
        except StopIteration:
            app.close()
        except BaseException:  # noqa: BLE001 - always release input after a failed native test
            traceback.print_exc()
            if app.image is not None:
                output = Path(__file__).resolve().parents[1] / "_work/monitor-control"
                output.mkdir(parents=True, exist_ok=True)
                app.image.write(str(output / "window-preview-failure.png"), format="png")
            app.close()
        else:
            root.after(delay, advance)

    root.after(100, advance)
    root.after(120000, app.close)
    try:
        root.mainloop()
    finally:
        app.close()
        if process.poll() is None:
            command("close")
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)
        native.restore(initial_cursor)
        native.user32.SetForegroundWindow(initial_focus)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(fixture() if "--fixture" in sys.argv else main())
