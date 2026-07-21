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
 :root{--bg:#0f1115;--card:#171a21;--line:#272b34;--muted:#9aa0a6;--accent:#2f6feb;--green:#5ddc78}
 *{box-sizing:border-box}
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:680px;margin:1.4rem auto;padding:0 1rem;background:var(--bg);color:#e8e8ea}
 h1{font-size:1.3rem;margin:0 0 .15rem}
 .prog{color:var(--muted);margin-bottom:.3rem;font-size:.9rem}
 .hint{color:#8fb0ff;font-size:.82rem;margin-bottom:.9rem;line-height:1.4}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1rem;margin:.6rem 0}
 .meta{color:var(--muted);font-size:.85rem;margin-bottom:.7rem}
 .clip{margin-bottom:1rem}
 .specwrap{position:relative;line-height:0}
 canvas.spec{width:100%;height:150px;display:block;border-radius:8px 8px 0 0;background:#000}
 canvas.hl{width:100%;height:14px;display:block;border-radius:0 0 8px 8px;background:#0c0d10}
 .playhead{position:absolute;top:0;bottom:0;width:2px;background:rgba(255,255,255,.85);left:0;display:none;pointer-events:none;box-shadow:0 0 4px rgba(255,255,255,.6)}
 .player{display:flex;align-items:center;gap:.7rem;margin-top:.55rem}
 .pp{width:40px;height:40px;min-width:40px;border-radius:50%;border:1px solid var(--line);background:#232833;color:#fff;font-size:.95rem;cursor:pointer;padding:0}
 .pp:hover{background:#2b3140}
 .seek{flex:1;height:7px;background:#2a2e37;border-radius:4px;position:relative;cursor:pointer}
 .fill{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--accent);border-radius:4px}
 .tm{color:var(--muted);font-size:.78rem;font-variant-numeric:tabular-nums;min-width:74px;text-align:right}
 .lbl{color:var(--green);font-size:.72rem;margin:.25rem 0 0;letter-spacing:.02em}
 input#nm{width:100%;padding:.6rem;border-radius:8px;border:1px solid var(--line);background:#0f1115;color:#e8e8ea;font-size:.95rem;margin-top:.4rem}
 .btns{margin-top:.7rem;display:flex;justify-content:space-between;align-items:center}
 button{padding:.55rem 1rem;border-radius:8px;border:1px solid var(--line);background:#232833;color:#e8e8ea;cursor:pointer;margin-right:.35rem}
 button:disabled{opacity:.4;cursor:default}
 #next{background:var(--accent);border-color:var(--accent);font-weight:600}
 #cancel{background:transparent;border-color:#5a3a3a;color:#e79aa0;margin-top:.9rem}
 #done{display:none;color:var(--green);font-weight:600;margin-top:1rem}
 .k{color:var(--green)}
</style></head><body>
<h1>Who is speaking?</h1>
<div class="prog" id="prog"></div>
<div class="hint">The <span class="k">green strip</span> under each spectrogram marks where <b>this</b> voice is talking. If a clip has two people, name the one in the green part — or mark Unsure.</div>
<div id="card" class="card"></div>
<div class="btns">
  <div><button id="back">← Back</button><button id="unsure">Unsure</button></div>
  <button id="next">Next →</button>
</div>
<button id="cancel">Cancel attribution</button>
<div id="done"></div>
<datalist id="names"></datalist>
<script>
let DATA=null, i=0; const names={};
const $=id=>document.getElementById(id);
const el=(t,c)=>{const e=document.createElement(t); if(c)e.className=c; return e;};
const fmt=s=>{s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');};
function inferno(v){ v=Math.max(0,Math.min(1,v));
  const s=[[0,0,4],[60,15,110],[150,44,90],[221,81,58],[249,153,29],[252,255,164]];
  const t=v*(s.length-1), j=Math.min(Math.floor(t),s.length-2), f=t-j, a=s[j], b=s[j+1];
  return [a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f,a[2]+(b[2]-a[2])*f]; }
function drawSpec(cv, spec){ if(!spec||!spec.length)return;
  const T=spec.length, M=spec[0].length;
  const off=el('canvas'); off.width=T; off.height=M; const octx=off.getContext('2d'); const img=octx.createImageData(T,M);
  for(let x=0;x<T;x++)for(let y=0;y<M;y++){ const [r,g,b]=inferno(spec[x][M-1-y]); const p=(y*T+x)*4; img.data[p]=r;img.data[p+1]=g;img.data[p+2]=b;img.data[p+3]=255; }
  octx.putImageData(img,0,0);
  const W=cv.width=Math.max(1,cv.clientWidth), H=cv.height=150, g=cv.getContext('2d'); g.imageSmoothingEnabled=true; g.drawImage(off,0,0,W,H); }
function drawHl(cv, scores, dur){ const W=cv.width=Math.max(1,cv.clientWidth), H=cv.height=14, g=cv.getContext('2d');
  g.fillStyle='#0c0d10'; g.fillRect(0,0,W,H); if(!scores||!scores.length)return;
  const c=scores.map(s=>({t:(s.t0+s.t1)/2, v:s.score}));
  for(let x=0;x<W;x++){ const t=x/W*dur; let v=c[0].v;
    if(t>=c[c.length-1].t) v=c[c.length-1].v;
    else for(let j=0;j<c.length-1;j++){ if(t>=c[j].t&&t<=c[j+1].t){ const f=(t-c[j].t)/((c[j+1].t-c[j].t)||1); v=c[j].v+(c[j+1].v-c[j].v)*f; break; } }
    const a=Math.max(0,Math.min(1,(v-0.35)/0.45)); g.fillStyle='rgba(93,220,120,'+a+')'; g.fillRect(x,0,1,H); } }
function makeClip(label,k){
  const wrap=el('div','clip'), sw=el('div','specwrap'), spec=el('canvas','spec'), ph=el('div','playhead'), hl=el('canvas','hl');
  sw.append(spec,ph);
  const player=el('div','player'), pp=el('button','pp'), seek=el('div','seek'), fill=el('div','fill'), tm=el('span','tm');
  pp.textContent='▶'; seek.append(fill); tm.textContent='0:00 / 0:00'; player.append(pp,seek,tm);
  wrap.append(sw,hl,player);
  const audio=new Audio(`/snippet?speaker=${encodeURIComponent(label)}&idx=${k}`); audio.preload='metadata';
  const redraw=d=>{drawSpec(spec,d.spec); drawHl(hl,d.scores,d.dur);};
  fetch(`/clip?speaker=${encodeURIComponent(label)}&idx=${k}`).then(r=>r.json()).then(d=>{ requestAnimationFrame(()=>redraw(d)); });
  pp.onclick=()=>{ document.querySelectorAll('audio').forEach(a=>{if(a!==audio)a.pause();}); if(audio.paused){audio.play();pp.textContent='⏸';}else{audio.pause();pp.textContent='▶';} };
  audio.onloadedmetadata=()=>{ tm.textContent='0:00 / '+fmt(audio.duration); };
  audio.ontimeupdate=()=>{ const p=(audio.currentTime/(audio.duration||1)); fill.style.width=(p*100)+'%'; ph.style.display='block'; ph.style.left=(p*100)+'%'; tm.textContent=fmt(audio.currentTime)+' / '+fmt(audio.duration); };
  audio.onended=()=>{pp.textContent='▶'; ph.style.display='none';};
  audio.onpause=()=>{pp.textContent='▶';};
  seek.onclick=e=>{ const r=seek.getBoundingClientRect(); audio.currentTime=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*(audio.duration||0); };
  return wrap;
}
function knownList(){ const s=new Set(DATA.known||[]); Object.values(names).forEach(n=>{if(n)s.add(n);}); return [...s].sort(); }
function render(){
  const v=DATA.voices[i];
  $('prog').textContent=`Voice ${i+1} of ${DATA.voices.length}`;
  const mm=Math.floor(v.seconds/60), ss=v.seconds%60;
  const card=$('card'); card.innerHTML='';
  const meta=el('div','meta'); meta.textContent=`${mm}m ${ss}s total across the meeting · ${v.count} sample clip(s)`; card.append(meta);
  for(let k=0;k<v.count;k++) card.append(makeClip(v.label,k));
  const inp=el('input'); inp.id='nm'; inp.setAttribute('list','names'); inp.placeholder='Type or pick a name…'; inp.value=names[v.label]||''; card.append(inp);
  const dl=$('names'); dl.innerHTML=''; knownList().forEach(n=>{const o=el('option'); o.value=n; dl.append(o);});
  $('back').disabled=(i===0); $('next').textContent=(i===DATA.voices.length-1)?'Finish & save':'Next →';
  inp.focus();
}
const save=()=>{ const inp=$('nm'); if(inp) names[DATA.voices[i].label]=inp.value.trim(); };
$('unsure').onclick=()=>{ const inp=$('nm'); if(inp) inp.value='Unsure'; };
$('back').onclick=()=>{ save(); if(i>0){i--;render();} };
$('next').onclick=async()=>{ save(); if(i<DATA.voices.length-1){ i++; render(); return; }
  await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({labels:names})}); finish('Saved.'); };
$('cancel').onclick=async()=>{ await fetch('/cancel',{method:'POST'}); finish('Cancelled.'); };
function finish(msg){ document.querySelectorAll('audio').forEach(a=>a.pause());
  for(const id of ['card','back','unsure','next','cancel','prog']){const e=$(id); if(e)e.style.display='none';}
  const d=$('done'); d.textContent=msg+' Closing…'; d.style.display='block';
  setTimeout(()=>{ try{window.close();}catch(e){} d.textContent=msg+' You can close this tab.'; }, 400); }
async function load(){ DATA=await (await fetch('/data')).json(); render(); }
load();
</script></body></html>"""
