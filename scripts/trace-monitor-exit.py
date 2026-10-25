"""Run the normal CLI with opt-in, local pointer-handoff diagnostics.

Records only recent pointer coordinates, input flags, preview geometry, and
release reasons. No keyboard events, window titles, or captured pixels.
"""

from __future__ import annotations

import ctypes
import json
import queue
import runpy
import sys
import time
from collections import deque
from pathlib import Path

from wcfi_tools.monitor_control import app as viewer
from wcfi_tools.monitor_control import input as pointer


def main():
    output = Path(__file__).resolve().parents[1] / '_work/monitor-control/exit-trace.jsonl'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('', encoding='utf-8')
    pending = queue.Queue()
    hooks, raw = deque(maxlen=12), deque(maxlen=12)
    stages = deque(maxlen=8)
    original_native_init = pointer._Win32.__init__
    original_app_init = viewer.MonitorApp.__init__

    def native_init(native):
        original_native_init(native)
        callback_type = native.hook_type

        def callback_factory(callback):
            def trace(code, message, address):
                if code >= 0:
                    data = ctypes.cast(address, ctypes.POINTER(pointer._MSLLHOOKSTRUCT)).contents
                    if data.dwExtraInfo != pointer.INPUT_MARKER:
                        hooks.append((time.monotonic(), message, data.pt.x, data.pt.y,
                                      data.flags, data.dwExtraInfo))
                return callback(code, message, address)
            return callback_type(trace)

        native.hook_type = callback_factory
        for name in ('restore', 'show_cursor', 'uninstall'):
            method = getattr(native, name)

            def traced(*args, _method=method, _name=name):
                result = _method(*args)
                stages.append((_name, time.monotonic(), native.position()))
                return result

            setattr(native, name, traced)

    def app_init(app, *args, **kwargs):
        original_app_init(app, *args, **kwargs)
        receive, release = app.input._capture_motion, app.input._release
        sequence = 0

        def motion(dx, dy, flags, device, source=0):
            raw.append((time.monotonic(), dx, dy, flags, device, source))
            receive(dx, dy, flags, device, source)

        def end(reason, return_point=None):
            nonlocal sequence
            had_control = app.input.active or app.input._return_point is not None
            if not had_control:
                return release(reason, return_point)
            sequence += 1
            stages.clear()
            record = {'event': 'release', 'sequence': sequence, 'time': time.monotonic(),
                      'reason': reason, 'preview': app.input._preview, 'target': app.input._target,
                      'point': app.input._point, 'destination': return_point,
                      'before': app.input._native.position(), 'hooks': list(hooks), 'raw': list(raw)}
            release(reason, return_point)
            record.update(after=app.input._native.position(), finished=time.monotonic())
            record['stages'] = list(stages)
            pending.put(record)

        def snapshot(number, delay):
            if app.closed:
                return
            pending.put({'event': 'after_release', 'sequence': number, 'delay_ms': delay,
                         'time': time.monotonic(), 'active': app.input.active,
                         'position': app.input._native.position(),
                         'preview': app._preview_rect() if app.preview else None})

        def drain():
            if app.closed:
                return
            while not pending.empty():
                item = pending.get_nowait()
                with output.open('a', encoding='utf-8') as log:
                    log.write(json.dumps(item) + '\n')
                if item['event'] == 'release':
                    for delay in (0, 20, 100, 300):
                        app.root.after(delay, snapshot, item['sequence'], delay)
            app.root.after(20, drain)

        app.input._capture_motion, app.input._release = motion, end
        app.root.after(20, drain)

    pointer._Win32.__init__ = native_init
    viewer.MonitorApp.__init__ = app_init
    sys.argv = ['wcfi', 'monitor-control', 'launch']
    runpy.run_module('wcfi_tools.cli', run_name='__main__')


if __name__ == '__main__':
    main()
