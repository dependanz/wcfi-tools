"""A tiny local web app for speaker attribution.

The CLI hands it per-speaker audio snippets; the user plays them and types who is speaking
(or marks "Unsure"). It runs on localhost, blocks until submitted, then returns the roster.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


@dataclass
class AnnotatorState:
    speakers: list[dict]  # {label, seconds, count}
    files: dict[str, list[Path]]  # label -> snippet paths
    known_names: list[str] = field(default_factory=list)
    result: dict[str, str] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)


def build_app(state: AnnotatorState) -> FastAPI:
    app = FastAPI(title="wcfi speaker attribution")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE.replace("__DATA__", json.dumps({
            "speakers": state.speakers,
            "known": state.known_names,
        }))

    @app.get("/snippet")
    def snippet(speaker: str, idx: int):
        clips = state.files.get(speaker, [])
        if 0 <= idx < len(clips) and clips[idx].exists():
            return FileResponse(str(clips[idx]), media_type="audio/wav")
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/submit")
    async def submit(request: Request):
        body = await request.json()
        labels = body.get("labels", {}) if isinstance(body, dict) else {}
        state.result = {k: (v or "").strip() for k, v in labels.items()}
        state.done.set()
        return {"ok": True}

    return app


def run_annotator(
    snippets: dict[str, list[Path]],
    durations: dict[str, float],
    *,
    known_names: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    timeout: float = 1800.0,
) -> dict[str, str]:
    """Serve the annotator, block until the user submits (or timeout), return {speaker: name}."""
    import uvicorn

    speakers = [
        {"label": label, "seconds": round(durations.get(label, 0.0)), "count": len(clips)}
        for label, clips in snippets.items()
    ]
    state = AnnotatorState(speakers=speakers, files=snippets, known_names=known_names or [])
    app = build_app(state)

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # wait for the server to come up
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    url = f"http://{host}:{port}/"
    print(f"  Opening speaker attribution in your browser: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover
            pass

    completed = state.done.wait(timeout=timeout)
    server.should_exit = True
    thread.join(timeout=5)
    if not completed:
        print("  Attribution timed out — continuing with speakers unlabeled.")
    return state.result


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>wcfi — who is speaking?</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;
      background:#0f1115;color:#e6e6e6}
 h1{font-size:1.4rem} .sub{color:#9aa0a6;margin-bottom:1.5rem}
 .card{background:#1a1d24;border:1px solid #2a2e37;border-radius:10px;padding:1rem;margin:.75rem 0}
 .lbl{font-weight:600} .meta{color:#9aa0a6;font-size:.85rem}
 audio{width:100%;margin:.4rem 0}
 input{width:70%;padding:.5rem;border-radius:6px;border:1px solid #3a3f4b;background:#0f1115;color:#e6e6e6}
 button{padding:.5rem .8rem;border-radius:6px;border:1px solid #3a3f4b;background:#232833;color:#e6e6e6;cursor:pointer}
 .unsure{margin-left:.4rem}
 #go{background:#2f6feb;border-color:#2f6feb;font-weight:600;padding:.7rem 1.4rem;margin-top:1rem}
 #done{display:none;color:#7ee787;font-weight:600;margin-top:1rem}
</style></head><body>
<h1>Who is speaking?</h1>
<div class="sub">Play each speaker's clips and type who it is. Not sure? Click <b>Unsure</b>.</div>
<div id="list"></div>
<button id="go">Save attributions</button>
<div id="done">Saved. You can close this tab.</div>
<script>
const DATA = __DATA__;
const list = document.getElementById('list');
DATA.speakers.forEach(sp => {
  const card = document.createElement('div'); card.className='card';
  const secs = sp.seconds; const mm = Math.floor(secs/60), ss = secs%60;
  let html = `<div class="lbl">${sp.label}</div>`+
             `<div class="meta">${mm}m ${ss}s of talking · ${sp.count} clip(s)</div>`;
  for(let i=0;i<sp.count;i++){
    html += `<audio controls preload="none" src="/snippet?speaker=${encodeURIComponent(sp.label)}&idx=${i}"></audio>`;
  }
  html += `<div><input list="names" placeholder="Name…" data-label="${encodeURIComponent(sp.label)}">`+
          `<button class="unsure" type="button">Unsure</button></div>`;
  card.innerHTML = html;
  card.querySelector('.unsure').onclick = () => { card.querySelector('input').value = 'Unsure'; };
  list.appendChild(card);
});
if(DATA.known && DATA.known.length){
  const dl=document.createElement('datalist'); dl.id='names';
  DATA.known.forEach(n=>{const o=document.createElement('option');o.value=n;dl.appendChild(o);});
  document.body.appendChild(dl);
}
document.getElementById('go').onclick = async () => {
  const labels = {};
  document.querySelectorAll('input[data-label]').forEach(inp => {
    labels[decodeURIComponent(inp.dataset.label)] = inp.value.trim();
  });
  await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},
                         body:JSON.stringify({labels})});
  document.getElementById('go').style.display='none';
  document.getElementById('done').style.display='block';
};
</script></body></html>"""
