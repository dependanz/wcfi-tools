import {useEffect, useState} from 'react';
import {AudioLines, FileAudio, LoaderCircle, RotateCcw, X} from 'lucide-react';
import {api, type State, type TakeSummary} from './api';

const seconds = (frame:number, rate:number) => (frame/rate).toFixed(6).replace(/\.?0+$/,'');
const clock = (frame:number, rate:number) => {
  const ms=Math.round(frame*1000/rate);
  return `${Math.floor(ms/60000)}:${((ms%60000)/1000).toFixed(3).padStart(6,'0')}`;
};

export function RecordedTakeControls({state,busy,action,fail}: {state:State;busy:boolean;
  action:(path:string,data?:unknown)=>Promise<boolean>;fail:(message:string)=>void}) {
  const [open,setOpen]=useState(false);
  const [takes,setTakes]=useState<TakeSummary[]>([]);
  const [selected,setSelected]=useState('');
  const [reading,setReading]=useState(false);
  const [start,setStart]=useState('0');
  const [end,setEnd]=useState('0');
  const [smooth,setSmooth]=useState(true);
  const take=state.take;
  useEffect(()=>{
    if(take){setStart(seconds(take.start_frame,take.sample_rate));
      setEnd(seconds(take.end_frame,take.sample_rate));setSmooth(take.fade_ms>0);}
  },[take?.id,take?.start_frame,take?.end_frame,take?.fade_ms]);
  async function refresh(){
    setReading(true);
    try{const result=await api<{takes:TakeSummary[]}>('/takes');setTakes(result.takes);
      setSelected(previous=>result.takes.some(t=>t.id===previous&&!t.error)?previous:result.takes.find(t=>!t.error)?.id||'');}
    catch(e){fail((e as Error).message);}finally{setReading(false);}
  }
  const chosen=takes.find(t=>t.id===selected);
  return <div className="take-workspace">
    {!take&&<button className="take-launch" disabled={busy} onClick={()=>{setOpen(!open);if(!open)refresh();}}>
      <FileAudio size={20}/><span><strong>Load recorded take</strong><small>Play real stems together with their recorded input mapping.</small></span></button>}
    {open&&<section className="take-library" aria-label="Recorded take library">
      <div className="section-row"><h3>Your recorded takes</h3><button className="icon-button" aria-label="Close take library" onClick={()=>setOpen(false)}><X size={17}/></button></div>
      <div className="take-select-row"><label>Recorded take<select aria-label="Recorded take selection" value={selected}
        disabled={reading||busy} onChange={e=>setSelected(e.target.value)}>
        <option value="">{reading?'Reading library…':'Choose a recorded take'}</option>
        {takes.map(t=><option key={t.id} value={t.id} disabled={Boolean(t.error)}>{t.title}{t.error?' · unavailable':''}</option>)}
      </select></label><button className="button primary" disabled={busy||reading||!chosen||Boolean(chosen.error)}
        onClick={async()=>{if(await action('/take/load',{take_id:selected}))setOpen(false);}}>
        {busy?<LoaderCircle className="spin" size={16}/>:<FileAudio size={16}/>}Load selected take</button>
        <button className="button secondary" disabled={reading||busy} onClick={refresh}>Refresh</button></div>
      {chosen&&!chosen.error&&<p>{chosen.source_count} stems · {chosen.input_channel_count} inputs · {clock(chosen.frames,chosen.sample_rate)} · {chosen.sample_rate/1000} kHz</p>}
      {!reading&&!takes.length&&<p>No recorded takes found in your local source library.</p>}
      <p>Loading pauses sending and replaces the input setup. Unloading restores your previous setup.</p>
      {takes.filter(t=>t.error).map(t=><p className="take-error" key={t.id}>{t.title}: {t.error}</p>)}
    </section>}
    {take&&<section className="recorded-take" aria-label="Recorded take playback">
      <div className="take-title"><span className="take-icon"><AudioLines size={22}/></span><div><span className="eyebrow">LINKED RECORDED TAKE</span>
        <h3>{take.title}</h3><p>{take.source_count} stems · {take.input_channel_count} mapped inputs · original speed</p></div>
        <button className="button secondary" disabled={busy} onClick={()=>{setOpen(!open);if(!open)refresh();}}>Change take</button></div>
      <div className="take-progress"><progress aria-label="Recorded take playhead" max={take.end_frame-take.start_frame}
        value={Math.max(0,take.position_frame-take.start_frame)}/><span>{clock(take.position_frame,take.sample_rate)} / {clock(take.frames,take.sample_rate)}</span></div>
      <form className="take-loop" onSubmit={async e=>{e.preventDefault();await action('/take/loop',{
        start_frame:Math.round(Number(start)*take.sample_rate),end_frame:Math.round(Number(end)*take.sample_rate),fade_ms:smooth?5:0});}}>
        <label>Loop start (seconds)<input type="number" min="0" max={take.duration_seconds} step="any" required value={start}
          onChange={e=>setStart(e.target.value)}/></label>
        <label>Loop end (seconds)<input type="number" min="0.1" max={take.duration_seconds} step="any" required value={end}
          onChange={e=>setEnd(e.target.value)}/></label>
        <label className="take-smooth"><input type="checkbox" checked={smooth} onChange={e=>setSmooth(e.target.checked)}/><span>Smooth loop edges<small>5 ms, shared across all inputs</small></span></label>
        <button className="button primary" disabled={busy} type="submit">Apply shared loop</button>
      </form>
      <div className="take-actions"><button className="button secondary" disabled={busy} onClick={()=>action('/take/restart')}><RotateCcw size={14}/>Restart take</button>
        <button className="button secondary" disabled={busy} onClick={()=>action('/take/loop',{start_frame:0,end_frame:take.frames,fade_ms:smooth?5:0})}>Use full take</button>
        <button className="button secondary" disabled={busy} onClick={()=>action('/take/unload')}>Unload take</button>
        <span>{clock(take.end_frame-take.start_frame,take.sample_rate)} loop · {state.sending?'Sending':'Paused'}</span></div>
      <p className="take-explainer">Every stem shares this range and playhead, including muted inputs. Applying a range or restarting pauses sending at the loop start.</p>
      <p className="take-mapping">Mapped inputs: {take.mapped_inputs.join(', ')}. Use each channel’s switches to choose what to send.</p>
    </section>}
  </div>;
}
