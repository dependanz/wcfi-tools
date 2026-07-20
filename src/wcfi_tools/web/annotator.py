"""Local web app for speaker attribution — paginated, one voice per page.

The CLI hands it per-voice audio snippets; the user pages through them and picks who each is
(from a dropdown of already-known names, a new name, or "Unsure"). It runs on localhost, blocks
until the user submits or cancels, then returns the roster. Ctrl-C in the terminal cancels too.
"""

from __future__ import annotations

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
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)


def build_app(state: AnnotatorState) -> FastAPI:
    app = FastAPI(title="wcfi speaker attribution")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/data")
    def data():
        return {"voices": state.speakers, "known": state.known_names}

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

    @app.post("/cancel")
    def cancel():
        state.cancelled = True
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
    timeout: float = 3600.0,
) -> tuple[dict[str, str], bool]:
    """Serve the annotator, block until submit/cancel/Ctrl-C. Returns (labels, cancelled)."""
    import uvicorn

    speakers = [
        {"label": label, "seconds": round(durations.get(label, 0.0)), "count": len(clips)}
        for label, clips in snippets.items()
    ]
    state = AnnotatorState(speakers=speakers, files=snippets, known_names=known_names or [])
    server = uvicorn.Server(uvicorn.Config(build_app(state), host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    url = f"http://{host}:{port}/"
    print(f"  Opening speaker attribution: {url}   (Ctrl-C here to cancel)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover
            pass

    deadline = time.monotonic() + timeout
    try:
        while not state.done.wait(0.3):  # short waits so Ctrl-C is caught promptly
            if time.monotonic() > deadline:
                print("  Attribution timed out — continuing without new names.")
                state.cancelled = True
                break
    except KeyboardInterrupt:
        print("  Cancelled — continuing without speaker names.")
        state.cancelled = True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    return state.result, state.cancelled


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>wcfi — who is speaking?</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;
      background:#0f1115;color:#e6e6e6}
 h1{font-size:1.3rem;margin-bottom:.2rem}
 .prog{color:#9aa0a6;margin-bottom:1rem}
 .card{background:#1a1d24;border:1px solid #2a2e37;border-radius:10px;padding:1.1rem;margin:.6rem 0}
 .meta{color:#9aa0a6;font-size:.85rem;margin-bottom:.4rem}
 audio{width:100%;margin:.3rem 0}
 input{width:60%;padding:.55rem;border-radius:6px;border:1px solid #3a3f4b;background:#0f1115;color:#e6e6e6}
 button{padding:.55rem .9rem;border-radius:6px;border:1px solid #3a3f4b;background:#232833;color:#e6e6e6;cursor:pointer;margin:.2rem .2rem 0 0}
 .row{margin-top:.6rem;display:flex;justify-content:space-between;align-items:center}
 #next{background:#2f6feb;border-color:#2f6feb;font-weight:600}
 #cancel{background:transparent;border-color:#5a3a3a;color:#e79aa0}
 #done{display:none;color:#7ee787;font-weight:600;margin-top:1rem}
</style></head><body>
<h1>Who is speaking?</h1>
<div class="prog" id="prog"></div>
<div id="card" class="card"></div>
<div class="row">
  <div><button id="back">← Back</button><button id="unsure">Unsure</button></div>
  <button id="next">Next →</button>
</div>
<div style="margin-top:1rem"><button id="cancel">Cancel attribution</button></div>
<div id="done"></div>
<datalist id="names"></datalist>
<script>
let DATA=null, i=0; const names={};
async function load(){ DATA=await (await fetch('/data')).json(); render(); }
function knownList(){
  const s=new Set(DATA.known||[]); Object.values(names).forEach(n=>{ if(n) s.add(n); });
  return [...s].sort();
}
function render(){
  const v=DATA.voices[i];
  document.getElementById('prog').textContent=`Voice ${i+1} of ${DATA.voices.length}`;
  const secs=v.seconds, mm=Math.floor(secs/60), ss=secs%60;
  let h=`<div class="meta">${mm}m ${ss}s of talking · ${v.count} clip(s) · same voice in each clip</div>`;
  for(let k=0;k<v.count;k++) h+=`<audio controls preload="none" src="/snippet?speaker=${encodeURIComponent(v.label)}&idx=${k}"></audio>`;
  h+=`<div style="margin-top:.5rem"><input id="nm" list="names" placeholder="Type or pick a name…" value="${(names[v.label]||'').replace(/"/g,'&quot;')}"></div>`;
  document.getElementById('card').innerHTML=h;
  const dl=document.getElementById('names'); dl.innerHTML=''; knownList().forEach(n=>{const o=document.createElement('option');o.value=n;dl.appendChild(o);});
  document.getElementById('back').disabled=(i===0);
  document.getElementById('next').textContent=(i===DATA.voices.length-1)?'Finish & save':'Next →';
  document.getElementById('nm').focus();
}
function save(){ names[DATA.voices[i].label]=document.getElementById('nm').value.trim(); }
document.getElementById('unsure').onclick=()=>{document.getElementById('nm').value='Unsure';};
document.getElementById('back').onclick=()=>{save(); if(i>0){i--;render();}};
document.getElementById('next').onclick=async()=>{
  save();
  if(i<DATA.voices.length-1){ i++; render(); return; }
  await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({labels:names})});
  finish('Saved. You can close this tab.');
};
document.getElementById('cancel').onclick=async()=>{
  await fetch('/cancel',{method:'POST'}); finish('Cancelled. You can close this tab.');
};
function finish(msg){ for(const id of ['card','back','unsure','next','cancel','prog']) document.getElementById(id).style.display='none';
  const d=document.getElementById('done'); d.textContent=msg; d.style.display='block'; }
load();
</script></body></html>"""
