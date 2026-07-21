"""Local web app for speaker attribution — one voice per page, with a spectrogram whose
highlighted band shows where *this* voice is speaking (so a mixed clip is obvious).

The CLI hands it a list of voices, each with representative clips (wav + spectrogram + per-window
target-similarity scores). The user pages through, picks who each is (dropdown of known names, a new
name, or "Unsure"), then submits or cancels. Ctrl-C in the terminal cancels too.
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
    voices: list[dict]  # {label, seconds, clips: [{path, spec, scores, dur}]}
    known_names: list[str] = field(default_factory=list)
    result: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def by_label(self, label: str) -> dict | None:
        return next((v for v in self.voices if v["label"] == label), None)


def build_app(state: AnnotatorState) -> FastAPI:
    app = FastAPI(title="wcfi speaker attribution")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/data")
    def data():
        return {
            "voices": [{"label": v["label"], "seconds": v["seconds"], "count": len(v["clips"])} for v in state.voices],
            "known": state.known_names,
        }

    @app.get("/clip")
    def clip(speaker: str, idx: int):
        v = state.by_label(speaker)
        if v and 0 <= idx < len(v["clips"]):
            c = v["clips"][idx]
            return {"spec": c["spec"], "scores": c["scores"], "dur": c["dur"]}
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/snippet")
    def snippet(speaker: str, idx: int):
        v = state.by_label(speaker)
        if v and 0 <= idx < len(v["clips"]) and Path(v["clips"][idx]["path"]).exists():
            return FileResponse(str(v["clips"][idx]["path"]), media_type="audio/wav")
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
    voices: list[dict],
    *,
    known_names: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    timeout: float = 3600.0,
) -> tuple[dict[str, str], bool]:
    """Serve the annotator, block until submit/cancel/Ctrl-C. Returns (labels, cancelled)."""
    import uvicorn

    state = AnnotatorState(voices=voices, known_names=known_names or [])
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
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:660px;margin:1.5rem auto;padding:0 1rem;
      background:#0f1115;color:#e6e6e6}
 h1{font-size:1.3rem;margin-bottom:.2rem}
 .prog{color:#9aa0a6;margin-bottom:.3rem}
 .hint{color:#7ea6ff;font-size:.82rem;margin-bottom:.8rem}
 .card{background:#1a1d24;border:1px solid #2a2e37;border-radius:10px;padding:1rem;margin:.6rem 0}
 .meta{color:#9aa0a6;font-size:.85rem;margin-bottom:.5rem}
 .clip{margin-bottom:.8rem}
 canvas{width:100%;height:110px;border-radius:6px;background:#000;display:block}
 audio{width:100%;margin-top:.3rem}
 input{width:60%;padding:.55rem;border-radius:6px;border:1px solid #3a3f4b;background:#0f1115;color:#e6e6e6}
 button{padding:.55rem .9rem;border-radius:6px;border:1px solid #3a3f4b;background:#232833;color:#e6e6e6;cursor:pointer;margin:.2rem .2rem 0 0}
 .row{margin-top:.6rem;display:flex;justify-content:space-between;align-items:center}
 #next{background:#2f6feb;border-color:#2f6feb;font-weight:600}
 #cancel{background:transparent;border-color:#5a3a3a;color:#e79aa0}
 #done{display:none;color:#7ee787;font-weight:600;margin-top:1rem}
 .key{color:#5ddc78}
</style></head><body>
<h1>Who is speaking?</h1>
<div class="prog" id="prog"></div>
<div class="hint">The <span class="key">green highlight</span> marks where this voice is talking. If a clip has two people, name the person in the green part (or mark Unsure).</div>
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
function knownList(){ const s=new Set(DATA.known||[]); Object.values(names).forEach(n=>{if(n)s.add(n);}); return [...s].sort(); }
function drawClip(cv, d){
  const spec=d.spec; if(!spec||!spec.length) return;
  const T=spec.length, B=spec[0].length;
  const off=document.createElement('canvas'); off.width=T; off.height=B;
  const octx=off.getContext('2d'); const img=octx.createImageData(T,B);
  for(let x=0;x<T;x++){ for(let y=0;y<B;y++){ const v=spec[x][B-1-y]; const p=(y*T+x)*4;
    const g=Math.round(30+v*225); img.data[p]=g*0.5; img.data[p+1]=g*0.7; img.data[p+2]=g; img.data[p+3]=255; } }
  octx.putImageData(img,0,0);
  const W=cv.width=cv.clientWidth||560, H=cv.height=110; const g=cv.getContext('2d');
  g.imageSmoothingEnabled=false; g.clearRect(0,0,W,H); g.drawImage(off,0,0,W,H);
  (d.scores||[]).forEach(s=>{ const x0=s.t0/d.dur*W, x1=s.t1/d.dur*W;
    const a=Math.max(0,Math.min(1,(s.score-0.4)/0.4)); if(a<=0)return;
    g.fillStyle='rgba(93,220,120,'+(0.40*a)+')'; g.fillRect(x0,0,x1-x0,H); });
}
function render(){
  const v=DATA.voices[i];
  document.getElementById('prog').textContent=`Voice ${i+1} of ${DATA.voices.length}`;
  const mm=Math.floor(v.seconds/60), ss=v.seconds%60;
  let h=`<div class="meta">${mm}m ${ss}s total across the meeting · ${v.count} sample clip(s)</div>`;
  for(let k=0;k<v.count;k++) h+=`<div class="clip"><canvas id="cv${k}"></canvas><audio controls preload="none" src="/snippet?speaker=${encodeURIComponent(v.label)}&idx=${k}"></audio></div>`;
  h+=`<div><input id="nm" list="names" placeholder="Type or pick a name…" value="${(names[v.label]||'').replace(/"/g,'&quot;')}"></div>`;
  document.getElementById('card').innerHTML=h;
  const dl=document.getElementById('names'); dl.innerHTML=''; knownList().forEach(n=>{const o=document.createElement('option');o.value=n;dl.appendChild(o);});
  for(let k=0;k<v.count;k++){ (async()=>{ const d=await (await fetch(`/clip?speaker=${encodeURIComponent(v.label)}&idx=${k}`)).json(); drawClip(document.getElementById('cv'+k), d); })(); }
  document.getElementById('back').disabled=(i===0);
  document.getElementById('next').textContent=(i===DATA.voices.length-1)?'Finish & save':'Next →';
  document.getElementById('nm').focus();
}
function save(){ names[DATA.voices[i].label]=document.getElementById('nm').value.trim(); }
document.getElementById('unsure').onclick=()=>{document.getElementById('nm').value='Unsure';};
document.getElementById('back').onclick=()=>{save(); if(i>0){i--;render();}};
document.getElementById('next').onclick=async()=>{ save();
  if(i<DATA.voices.length-1){ i++; render(); return; }
  await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({labels:names})});
  finish('Saved.'); };
document.getElementById('cancel').onclick=async()=>{ await fetch('/cancel',{method:'POST'}); finish('Cancelled.'); };
function finish(msg){ for(const id of ['card','back','unsure','next','cancel','prog']){const el=document.getElementById(id); if(el)el.style.display='none';}
  const d=document.getElementById('done'); d.textContent=msg+' Closing…'; d.style.display='block';
  setTimeout(()=>{ try{window.close();}catch(e){} d.textContent=msg+' You can close this tab.'; }, 400); }
load();
</script></body></html>"""
