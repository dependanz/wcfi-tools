"""Launch an isolated, local-only Chrome fixture for interactive monitor-control QA.

No input is injected by this launcher. Use the viewer normally (or Computer Use)
to exercise Chrome's context menu, native select popup, and Open file dialog.
Only a generated text file is selected. Test outcomes go to a loopback-only
endpoint; no file contents are read or uploaded.
Close the viewer and the separate Chrome fixture window when finished.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import tkinter as tk
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wcfi_tools.monitor_control.app import MonitorApp
from wcfi_tools.monitor_control.capture import enable_dpi_awareness
from wcfi_tools.monitor_control.windows import enumerate_windows

TITLE = "WCFI Chrome popup fixture"
HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>WCFI Chrome popup fixture</title>
<style>body{font:22px system-ui;background:#e8f1ff;color:#14243b;padding:25px}
select,input,button{font:22px system-ui;margin:12px 0;padding:10px}
label{display:block}output{display:block;margin:15px 0;color:#164f27}
</style></head><body><h1>Chrome popup checks</h1>
<p>Right-click this page to open Chrome's real context menu.</p>
<label>Native dropdown<br><select id="choice"><option>Alpha</option><option>Bravo</option>
<option>Charlie</option></select></label><output id="selected">Selected: Alpha</output>
<label>Local file picker (no upload)<br><input id="file" type="file" accept=".txt"></label>
<output id="filename">No file chosen</output>
<script>
const report=(event,value='')=>fetch('/result', {method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({event,value})});
report('loaded'); requestAnimationFrame(()=>requestAnimationFrame(()=>report('paint')));
document.addEventListener('contextmenu',()=>report('contextmenu'));
choice.onchange=()=>{selected.textContent='Selected: '+choice.value;
 report('choice',choice.value);};
file.onchange=()=>{filename.textContent='Chosen: '+(file.files[0]?.name || 'none');
 report('file',file.files[0]?.name==='wcfi-popup-test.txt'?'fixture':'other');};
</script>
</body></html>"""


class FixtureHandler(SimpleHTTPRequestHandler):
    """Serve only generated fixture files, never the browser profile or reports."""

    def do_GET(self):
        if self.path not in ('/', '/index.html', '/wcfi-popup-test.txt'):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        if self.path != '/result':
            self.send_error(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 256:
                raise ValueError('Invalid report size')
            result = json.loads(self.rfile.read(size))
            event, value = result['event'], result.get('value', '')
            allowed = {'loaded': [''], 'paint': [''], 'contextmenu': [''],
                       'choice': ['Alpha', 'Bravo', 'Charlie'], 'file': ['fixture', 'other']}
            if value not in allowed.get(event, []):
                raise ValueError('Invalid test result')
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        # Only fixed test labels, never entered text, filenames, or file contents.
        with (Path(self.directory).parent / 'events.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'time': time.time(), 'event': event, 'value': value}) + '\n')
        self.send_response(204)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-browser', action='store_true',
                        help='Attach the existing unique fixture window when restarting the viewer.')
    parser.add_argument('--port', type=int, default=0,
                        help='Reuse the fixture URL port when restarting (default: random free port).')
    args = parser.parse_args()
    enable_dpi_awareness()
    output = Path(__file__).resolve().parents[1] / "_work/monitor-control/chrome-fixture"
    output.mkdir(parents=True, exist_ok=True)
    web = output / 'web'
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text(HTML, encoding="utf-8")
    (web / "wcfi-popup-test.txt").write_text("Disposable local WCFI test file.\n", encoding="utf-8")
    chrome = next((p for p in (
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ) if p.exists()), None)
    if chrome is None:
        raise RuntimeError("Install Chrome before running the Chrome fixture.")
    # A loopback origin also makes the fixture URL unambiguous to browser tooling.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(
        FixtureHandler, directory=str(web),
    ))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    (output / "url.txt").write_text(url, encoding="utf-8")
    if not args.reuse_browser:
        subprocess.Popen([
            str(chrome), f"--user-data-dir={output / 'profile'}", "--no-first-run",
            "--no-default-browser-check", "--new-window", "--window-size=900,650",
            "--window-position=80,100", url,
        ])
    deadline = time.perf_counter() + 15
    while time.perf_counter() < deadline:
        matches = [w for w in enumerate_windows() if w.title.startswith(TITLE)]
        if len(matches) == 1:
            break
        time.sleep(.1)
    else:
        raise RuntimeError("The isolated Chrome fixture did not open a unique window.")
    root = tk.Tk()
    app = MonitorApp(root)
    root.title("WCFI · Chrome control test")
    app._populate(window_key=matches[0].key)
    motion_sources = {}
    receive_motion = app.input._capture_motion

    def record_motion(dx, dy, flags, device, source=0):
        # Aggregate source tags/flags only, without recording cursor coordinates.
        key = f'{source:08x}:{flags}'
        motion_sources[key] = motion_sources.get(key, 0) + 1
        receive_motion(dx, dy, flags, device, source)

    app.input._capture_motion = record_motion

    def record():
        if app.closed:
            return
        scene = app.presented_scene
        (output / "state.json").write_text(json.dumps({
            "active": app.input.active, "status": app.status.get(),
            "ready": app.frame_ready,
            "motion_sources": dict(motion_sources),
            "layers": [{"hwnd": layer.window.hwnd, "bounds": layer.bounds}
                       for layer in scene.layers] if scene else [],
        }, indent=2), encoding="utf-8")
        root.after(200, record)

    root.after(200, record)
    try:
        root.mainloop()
    finally:
        app.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
