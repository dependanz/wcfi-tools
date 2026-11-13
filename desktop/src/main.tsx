import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, ArrowDownToLine, ArrowRight, AudioLines, Check, ChevronDown, CircleHelp,
  FileAudio, Headphones, LayoutGrid, LoaderCircle, MoreHorizontal, Pause, Play, Power,
  Radio, SlidersHorizontal, Waves, X} from 'lucide-react';
import '@fontsource/manrope/latin-400.css';
import '@fontsource/manrope/latin-500.css';
import '@fontsource/manrope/latin-600.css';
import '@fontsource/manrope/latin-700.css';
import '@fontsource/montserrat/latin-700.css';
import '@fontsource/montserrat/latin-800.css';
import {api, importAudio, level, sourceNames, type Analysis, type Channel, type Meter, type State} from './api';
import './styles.css';
import {RecordedTakeControls} from './RecordedTake';
import {NodesPage} from './NodesPage';
import {NodeListenBar,useNodeListening} from './NodeListening';
import {RehearsalPage} from './RehearsalPage';

const zero: Meter = {peak_dbfs:-120,rms_dbfs:-120,clips:0};
const roles: Record<string,string> = {vocal:'Vocal',speech:'Speech',electric_guitar:'Electric guitar',
  acoustic_guitar:'Acoustic guitar',keys:'Keys',bass:'Bass',drums:'Drums',generic:'Other'};

function MeterBar({meter=zero, horizontal=false}: {meter?:Meter;horizontal?:boolean}) {
  const percent = Math.max(0, Math.min(100,(meter.peak_dbfs+60)/60*100));
  return <div className={`meter ${horizontal?'horizontal':''}`} role="meter" aria-label="Sample peak"
    aria-valuemin={-60} aria-valuemax={0} aria-valuenow={Math.min(0,Math.max(-60,meter.peak_dbfs))}>
    <span style={horizontal?{width:`${percent}%`}:{height:`${percent}%`}} className={meter.clips?'clipping':''}/>
  </div>;
}

function InputCard({channel, meter, onEdit, update}: {channel:Channel;meter:Meter;onEdit:()=>void;
  update:(port:number,data:Partial<Channel>)=>void}) {
  const [gain,setGain] = useState(channel.gain_db);
  useEffect(()=>setGain(channel.gain_db),[channel.gain_db]);
  return <article className={`input-card ${channel.enabled?'configured':''} ${channel.muted?'muted':''}`}>
    <div className="card-top"><span className="port-label">INPUT {String(channel.id).padStart(2,'0')}</span>
      <button className="icon-button" aria-label={`Configure input ${channel.id}`} onClick={onEdit}><MoreHorizontal size={19}/></button></div>
    <button className="channel-name" onClick={onEdit}>{channel.name}</button>
    <div className="source-label"><span className={`source-dot ${channel.source.kind==='silence'?'empty':''}`}/>
      {sourceNames[channel.source.kind] || channel.source.kind}</div>
    <div className="channel-control"><div className="gain-control"><span className="small-label">INPUT GAIN</span>
      <div className="gain-number">{gain>0?'+':''}{gain.toFixed(1)}<span>dB</span></div>
      <input aria-label={`Input ${channel.id} gain`} type="range" min="-60" max="12" step="0.5" value={gain}
        onChange={e=>setGain(Number(e.target.value))}
        onPointerUp={e=>update(channel.id,{gain_db:Number(e.currentTarget.value)})}
        onKeyUp={e=>update(channel.id,{gain_db:Number(e.currentTarget.value)})}/>
      <div className="range-labels"><span>−60</span><span>+12</span></div>
    </div><div className="channel-meter"><MeterBar meter={meter}/><div className="meter-scale"><span>0</span><span>−24</span><span>−60</span></div></div></div>
    <div className="card-bottom"><span className="peak-readout">{level(meter.peak_dbfs)} <small>dBFS</small></span>
      <div className="channel-buttons"><button className={channel.muted?'mute-active':''} aria-label={`Mute input ${channel.id}`}
        aria-pressed={channel.muted} onClick={()=>update(channel.id,{muted:!channel.muted})}>M</button>
        <button className={channel.solo?'solo-active':''} aria-label={`Solo input ${channel.id}`} aria-pressed={channel.solo}
          onClick={()=>update(channel.id,{solo:!channel.solo})}>S</button>
        <button className={`channel-power ${channel.enabled?'enabled':''}`} aria-label={`Enable input ${channel.id}`}
          aria-pressed={channel.enabled} onClick={()=>update(channel.id,{enabled:!channel.enabled})}><Power size={14}/></button></div>
    </div>
  </article>;
}

function ChannelEditor({channel,close,save,fail}: {channel:Channel;close:()=>void;
  save:(id:number,data:Partial<Channel>)=>Promise<void>;fail:(message:string)=>void}) {
  const [draft,setDraft]=useState<Channel>(JSON.parse(JSON.stringify(channel)));
  const [busy,setBusy]=useState(false);
  const upload=useRef<HTMLInputElement>(null);
  const panel=useRef<HTMLElement>(null);
  const linked=channel.source.kind==='take';
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    const controls=()=>Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not([hidden]),select')||[])
      .filter(e=>e.getClientRects().length>0);
    controls()[0]?.focus();
    const key=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){event.preventDefault();close();}
      if(event.key==='Tab'){
        const list=controls();const first=list[0],last=list[list.length-1];
        if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
        if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
      }
    };
    document.addEventListener('keydown',key);
    return()=>{document.removeEventListener('keydown',key);previous?.focus();};
  },[]);
  const source=(key:string,value:unknown)=>setDraft(d=>({...d,source:{...d.source,[key]:value}}));
  const fault=(kind:string, enabled:boolean)=>setDraft(d=>({...d,faults: enabled
    ? [...d.faults.filter(f=>f.kind!==kind), kind==='clip'?{kind,drive_db:30,ceiling:.7,post_db:-6}:
      kind==='hum'?{kind,hz:60,dbfs:-30,harmonics:5}:kind==='mud'?{kind,hz:300,gain_db:12,lowpass_hz:4000}:
      {kind,dbfs:-38}] : d.faults.filter(f=>f.kind!==kind)}));
  async function imported(file?:File) {
    if(!file)return;
    setBusy(true);
    try {
      const info=await importAudio(file);
      setDraft(d=>({...d,seconds:Math.min(10,Math.floor(info.seconds)),source:{kind:'file',path:info.path,
        original_name:info.name,file_channel:1,file_channels_count:info.channels,start_seconds:0}}));
    } catch(e){fail((e as Error).message);} finally{setBusy(false);}
  }
  return <div className="drawer-scrim" onMouseDown={e=>{if(e.target===e.currentTarget)close();}}>
    <section ref={panel} className="drawer" role="dialog" aria-modal="true" aria-labelledby="editor-title">
      <header><div><span className="eyebrow">INPUT {String(channel.id).padStart(2,'0')}</span><h2 id="editor-title">Shape the source</h2></div>
        <button className="icon-button" aria-label="Close input settings" onClick={close}><X/></button></header>
      <form onSubmit={async e=>{e.preventDefault();setBusy(true);try{await save(channel.id,draft);close();}
        catch(e){fail((e as Error).message);}finally{setBusy(false);}}}>
        <div className="editor-body"><div className="field-row"><label>Channel name<input value={draft.name} required maxLength={100}
          onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
          <label>Source role<select value={draft.role} onChange={e=>setDraft({...draft,role:e.target.value})}>
            {Object.entries(roles).map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label></div>
        <h3>Audio source</h3>{linked?<div className="linked-source"><AudioLines size={22}/><strong>{draft.source.track_name} · file channel {draft.source.file_channel}</strong>
          <p>This source follows the recorded take’s shared playhead and loop range. Gain, mute, solo and enable controls stay available. Unload the take to change individual sources or input conditions.</p></div>:<>
        <div className="source-choices">{Object.entries(sourceNames).filter(([kind])=>kind!=='take').map(([kind,name])=><button type="button" key={kind}
          className={draft.source.kind===kind?'selected':''} onClick={()=>setDraft(d=>({...d,source:{kind,level_dbfs:-30,
            frequency_hz:220,bpm:105,midi_notes:[60,64,67,64],kick_db:-26,snare_db:-30,hat_db:-38}}))}>
          {kind==='file'?<FileAudio size={17}/>:kind==='silence'?<Pause size={17}/>:<AudioLines size={17}/>} {name}</button>)}</div>
        {draft.source.kind==='file'?<>
          <input ref={upload} type="file" accept=".wav,.flac,.aif,.aiff,.ogg,.mp3" hidden onChange={e=>imported(e.target.files?.[0])}/>
          <button type="button" className="file-picker" onClick={()=>upload.current?.click()} disabled={busy}>
            <ArrowDownToLine size={24}/><strong>{draft.source.original_name||'Choose a local audio file'}</strong>
            <span>WAV, FLAC, AIFF, OGG or MP3 · up to 128 MB</span></button>
          {draft.source.path&&<div className="field-row"><label>File channel<select value={draft.source.file_channel||1}
            onChange={e=>source('file_channel',Number(e.target.value))}>{Array.from({length:draft.source.file_channels_count||1},(_,i)=><option key={i} value={i+1}>Channel {i+1}</option>)}</select></label>
            <label>Start at (seconds)<input type="number" min="0" max="86400" step="0.1" value={draft.source.start_seconds||0}
              onChange={e=>source('start_seconds',Number(e.target.value))}/></label></div>}
        </>:draft.source.kind==='silence'?<p className="field-hint">This input sends silence. Pick a source to begin.</p>:<>
          <div className="field-row">{draft.source.kind==='tone'?<label>Frequency (Hz)<input type="number" min="20" max="23999"
            value={draft.source.frequency_hz||220} onChange={e=>source('frequency_hz',Number(e.target.value))}/></label>:
            draft.source.kind!=='noise'?<label>Tempo (BPM)<input type="number" min="30" max="240" value={draft.source.bpm||105}
              onChange={e=>source('bpm',Number(e.target.value))}/></label>:null}
          <label>Source level (dBFS)<input type="number" min="-90" max="0" value={draft.source.level_dbfs??-30}
            onChange={e=>source('level_dbfs',Number(e.target.value))}/></label></div>
          {draft.source.kind==='notes'&&<label>Notes (MIDI numbers)<input value={(draft.source.midi_notes||[60,64,67,64]).join(', ')}
            onChange={e=>source('midi_notes',e.target.value.split(',').map(n=>Number(n.trim())))}/></label>}
          <p className="field-hint">Procedural signals are engineering probes. Use real stems to evaluate musical mixing.</p>
        </>}
        {draft.source.kind!=='silence'&&<label>Loop length (seconds)<input type="number" min="1" max="30" step="1" value={draft.seconds}
          onChange={e=>setDraft({...draft,seconds:Number(e.target.value)})}/></label>}
        <h3>Input conditions</h3><p className="field-hint">Add a repeatable problem before the signal reaches Ableton.</p>
        <div className="fault-list">{[['clip','Overdriven preamp','Hard clipping followed by a quieter trim.'],
          ['hum','Electrical hum','60 Hz hum and its harmonics.'],['noise','Background hiss','Broadband noise added to the source.'],
          ['mud','Muddy signal','Low-mid buildup with a reduced top end.']].map(([kind,name,note])=><label key={kind} className="check-row">
            <span><strong>{name}</strong><small>{note}</small></span><input type="checkbox" checked={draft.faults.some(f=>f.kind===kind)}
              onChange={e=>fault(kind,e.target.checked)}/></label>)}</div></>}
        <label className="check-row enable-row"><span><strong>Enable this input</strong><small>Include it when sending inputs.</small></span>
          <input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/></label>
        </div><footer><button type="button" className="button secondary" onClick={close}>Cancel</button>
          <button className="button primary" disabled={busy}>{busy?<LoaderCircle className="spin" size={17}/>:<Check size={17}/>} Apply changes</button></footer>
      </form>
    </section>
  </div>;
}

function Spectrum({analysis}:{analysis:Analysis|null}) {
  const colors=['#094773','#7e68a5'];
  return <svg className="spectrum" viewBox="0 0 800 270" role="img" aria-label="Measured output frequency spectrum">
    <defs>{colors.map((color,i)=><linearGradient key={i} id={`fill${i}`} x1="0" y1="0" x2="0" y2="1">
      <stop stopColor={color} stopOpacity=".20"/><stop offset="1" stopColor={color} stopOpacity=".015"/></linearGradient>)}</defs>
    {[-12,-36,-60,-84].map(db=><g key={db}><line x1="42" x2="780" y1={20-db/96*210} y2={20-db/96*210}/>
      <text x="2" y={24-db/96*210}>{db}</text></g>)}
    {[20,100,1000,10000,20000].map(hz=><g key={hz}><line x1={42+Math.log10(hz/20)/3*738} x2={42+Math.log10(hz/20)/3*738} y1="20" y2="230"/>
      <text x={42+Math.log10(hz/20)/3*738} y="255" textAnchor="middle">{hz>=1000?`${hz/1000}k`:hz}</text></g>)}
    {analysis?.available&&analysis.results.map((r,i)=>{
      const points=r.spectrum.map(s=>`${42+Math.log10(s.hz/20)/3*738},${20-Math.max(-96,Math.min(0,s.dbfs))/96*210}`).join(' ');
      return <g key={r.channel}><polygon points={`42,230 ${points} 780,230`} fill={`url(#fill${i})`}/>
        <polyline points={points} fill="none" stroke={colors[i]} strokeWidth="2" strokeLinejoin="round"/></g>;
    })}
    {!analysis?.available&&<text className="empty-graph" x="412" y="125" textAnchor="middle">Waiting for audio from the DAW</text>}
  </svg>;
}

function OutputPage({state,fail}:{state:State;fail:(message:string)=>void}) {
  const [ports,setPorts]=useState([1,2]);
  const [analysis,setAnalysis]=useState<Analysis|null>(null);
  useEffect(()=>{let stopped=false;let timer:ReturnType<typeof setTimeout>;
    async function poll(){try{const result=await api<Analysis>(`/analysis?channels=${ports.join(',')}`);if(!stopped)setAnalysis(result);}
      catch(e){if(!stopped)fail((e as Error).message);}finally{if(!stopped)timer=setTimeout(poll,500);}}
    poll();return()=>{stopped=true;clearTimeout(timer);};},[ports.join(',')]);
  const selected=(port:number)=>setPorts(p=>p.includes(port)?p.length>1?p.filter(n=>n!==port):p:p.length===2?[p[1],port]:[...p,port]);
  return <><div className="page-heading"><div><span className="eyebrow">WHAT COMES BACK</span><h1>Output analysis</h1>
    <p>See what your DAW is sending, one channel or pair at a time.</p></div><span className={`connection-badge ${state.output_connected?'connected':''}`}><span/>
      {state.output_connected?'Receiving DAW audio':'Waiting for the DAW'}</span></div>
    <section className="output-picker"><div className="section-row"><h3>Output channels</h3><div className="pair-buttons">
      <button className={ports.join(',')==='1,2'?'active':''} onClick={()=>setPorts([1,2])}>1 / 2</button>
      <button className={ports.join(',')==='23,24'?'active':''} onClick={()=>setPorts([23,24])}>23 / 24</button></div></div>
      <div className="output-grid">{state.output_meters.map((m,i)=><button key={i} className={ports.includes(i+1)?'selected':''}
        aria-label={`Analyze output ${i+1}`} aria-pressed={ports.includes(i+1)} onClick={()=>selected(i+1)}>
        <span>{String(i+1).padStart(2,'0')}</span><MeterBar meter={m} horizontal/></button>)}</div>
    </section>
    <div className="analysis-layout"><section className="analysis-panel"><div className="section-row"><div><h3>Frequency spectrum</h3>
      <p>Last second · 20 Hz – 20 kHz</p></div><div className="legend">{ports.map((p,i)=><span key={p}><i className={`color-${i}`}/>OUT {p}</span>)}</div></div>
      <Spectrum analysis={analysis}/><div className="waveform-heading"><h3>Waveform envelope</h3><span>−1 to +1</span></div>
      <svg className="waveform" viewBox="0 0 800 120" aria-label="Measured output waveform envelope" role="img">
        <line x1="0" x2="800" y1="60" y2="60"/>
        {analysis?.available&&analysis.results.map((r,i)=><path key={r.channel} fill={i?'#7e68a5':'#094773'} opacity=".6"
          d={`M ${r.waveform.map((w,n)=>`${n*4},${60-w.max*55}`).join(' L ')} L ${[...r.waveform].reverse().map((w,n)=>`${(199-n)*4},${60-w.min*55}`).join(' L ')} Z`}/>)}
      </svg><p className="analysis-footnote">Measured locally. No Ableton recording or export required.</p>
    </section><aside className="measurement-panel"><span className="eyebrow">SIGNAL DETAILS</span>
      {ports.map((port,i)=>{const r=analysis?.available?analysis.results.find(r=>r.channel===port):undefined;return <div className="measurement-channel" key={port}>
        <h3><i className={`color-${i}`}/>Output {String(port).padStart(2,'0')}</h3>
        <dl><div><dt>Sample peak</dt><dd>{level(r?.peak_dbfs)} <small>dBFS</small></dd></div>
          <div><dt>RMS level</dt><dd>{level(r?.rms_dbfs)} <small>dBFS</small></dd></div>
          <div><dt>Crest factor</dt><dd>{level(r?.crest_db)} <small>dB</small></dd></div>
          <div><dt>Dominant frequency</dt><dd>{r?.dominant_hz?.toFixed(0)||'—'} <small>Hz</small></dd></div>
          <div><dt>Full-scale samples</dt><dd className={r?.clipped_samples?'danger-text':''}>{r?.clipped_samples??'—'}</dd></div></dl>
        {r?.notes.map((note,n)=><p className="measurement-note" key={n}>{note}</p>)}</div>;})}
      {ports.length===2&&<div className="correlation"><span>Stereo correlation</span><strong>{analysis?.correlation?.toFixed(2)??'—'}</strong></div>}
      <p className="field-hint">Levels use sample measurements. RMS is not perceptual loudness.</p>
    </aside></div>
  </>;
}

function App(){
  const monitor=useNodeListening();
  const [state,setState]=useState<State|null>(null);
  const [page,setPage]=useState('inputs');
  const [bank,setBank]=useState(0);
  const [editing,setEditing]=useState<number|null>(null);
  const [error,setError]=useState('');
  const [pending,setPending]=useState('');
  const [help,setHelp]=useState(false);
  const [reachable,setReachable]=useState(false);
  useEffect(()=>{let stopped=false;let timer:ReturnType<typeof setTimeout>;
    async function poll(){try{const result=await api('/state');if(!stopped){setState(result);setReachable(true);}}
      catch(e){if(!stopped){setReachable(false);setError((e as Error).message);}}
      finally{if(!stopped)timer=setTimeout(poll,250);}}
    poll();return()=>{stopped=true;clearTimeout(timer);};},[]);
  async function action(path:string,data:unknown={}){setPending(path);setError('');try{const result=await api(path,data);if(result.channels)setState(result);return true;}
    catch(e){setError((e as Error).message);return false;}finally{setPending('');}}
  async function save(port:number,data:Partial<Channel>){await api<Channel>(`/inputs/${port}`,data);setState(await api('/state'));}
  const update=(port:number,data:Partial<Channel>)=>{save(port,data).catch(e=>setError((e as Error).message));};
  const connected=Boolean(state?.output_connected);
  const active=state?.channels.filter(c=>c.enabled&&!c.muted).length||0;
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><span className="brand-mark"><Waves size={28}/></span>
    <div><strong>WCFI<span>AUDIO</span></strong><small>WORD CHRISTIAN FELLOWSHIP</small></div></div>
    <div className="workspace-label">YOUR WORKSPACE</div><nav><button className={page==='inputs'?'active':''} onClick={()=>setPage('inputs')}><SlidersHorizontal size={18}/>Input channels<span>32</span></button>
      <button className={page==='outputs'?'active':''} onClick={()=>setPage('outputs')}><Activity size={18}/>Output analysis<ArrowRight size={15}/></button>
      <button className={page==='taps'?'active':''} onClick={()=>setPage('taps')}><AudioLines size={18}/>Analysis nodes<ArrowRight size={15}/></button>
      <button className={page==='rehearsal'?'active':''} onClick={()=>setPage('rehearsal')}><Waves size={18}/>Rehearsal lab<ArrowRight size={15}/></button></nav>
    <div className="sidebar-note"><span className="note-icon"><Headphones size={19}/></span><h3>Bring rehearsal home.</h3><p>Build your input signals. Test your mix. Keep the real stage out of the equation.</p><span>LOCAL AUDIO WORKSPACE</span></div>
    <div className="sidebar-bottom"><button onClick={()=>setHelp(!help)}><CircleHelp size={17}/>Connection guide</button><span>WCFI TOOLS <i/> AUDIO EMULATOR</span></div>
    </aside><div className="workspace"><header className="topbar"><div className="breadcrumb">Audio workspace <span>/</span><strong>Emulator</strong></div>
      <div className="engine-controls"><span className={`engine-status ${state?.running&&reachable?'online':''}`}><i/>{!reachable?'Service disconnected':state?.running?'Emulator online':'Emulator stopped'}</span>
        <button className={`button ${state?.running?'secondary':'primary'}`} disabled={Boolean(pending)||!state||!reachable}
          onClick={()=>action(state?.running?'/stop':'/start')}><Power size={15}/>{pending==='/start'||pending==='/stop'?'Updating…':state?.running?'Stop emulator':'Start emulator'}</button></div></header>
      <main>{(page==='taps'||monitor.state.node||monitor.state.error)&&<NodeListenBar monitor={monitor}/>}
        {error&&<div className="error-banner" role="alert"><span>{error}</span><button aria-label="Dismiss error" onClick={()=>setError('')}><X size={17}/></button></div>}
        {state?.error&&<div className="error-banner" role="status">{state.error}</div>}
        {state?.warning&&<div className="error-banner" role="status">{state.warning}</div>}
        {help&&<section className="connection-guide"><div><h3>Connect to your DAW</h3><p>Start the emulator, select <strong>ASIO → WCFI Emulator</strong> in Ableton, then set audio tracks to the matching external inputs with <strong>Monitor In</strong>. Choose a source and send inputs. Arrangement Play stays stopped.</p><p>The registered driver may stay listed while stopped; opening it requires an online emulator. To hear a track through your computer, choose Listen on its Analysis Node. Listen volume controls that local copy.</p></div>
          <button className="icon-button" aria-label="Close connection guide" onClick={()=>setHelp(false)}><X size={18}/></button></section>}
        {!state||!reachable?<div className="loading"><LoaderCircle className="spin"/><h2>Connecting to your audio workspace</h2><p>Start from <code>wcfi audio emulator launch</code>.</p></div>:
        page==='inputs'?<><div className="page-heading"><div><span className="eyebrow">THE SIGNAL STARTS HERE</span><h1>Input channels<span className="heading-dot">.</span></h1>
          <p>Give every input a source. Put your mix through its paces.</p></div><button className={`button send-button ${state.sending?'sending':'primary'}`}
            disabled={!state.running||Boolean(pending)} onClick={()=>action('/sending',{enabled:!state.sending})}>
            {state.sending?<Pause size={16}/>:<Play size={16}/>} {state.sending?'Pause inputs':'Send inputs'}</button></div>
          <RecordedTakeControls state={state} busy={Boolean(pending)} action={action} fail={setError}/>
          <div className="summary-strip"><div><span className={`summary-icon ${connected?'connected':''}`}><Radio size={20}/></span><section><strong>{connected?'DAW connected':'Waiting for your DAW'}</strong><small>{connected?'WCFI Emulator · ASIO':'Select WCFI Emulator in your DAW'}</small></section></div>
            <div><span className="summary-value">48<span>kHz</span></span><section><strong>Sample rate</strong><small>32-bit float</small></section></div>
            <div><span className="summary-value">{state.driver?.running?state.driver.blocksize:'—'}</span><section><strong>Buffer size</strong><small>frames</small></section></div>
            <div><span className="summary-value">{String(active).padStart(2,'0')}<span>/ 32</span></span><section><strong>Inputs enabled</strong><small>{state.sending?'Sending configured sources':'Ready when you are'}</small></section></div></div>
          <div className="input-toolbar"><div className="bank-tabs" aria-label="Input channel bank">{[0,1,2,3].map(b=><button className={bank===b?'active':''} key={b}
            onClick={()=>setBank(b)}>{b*8+1}–{b*8+8}</button>)}</div><div className="preset-select"><LayoutGrid size={15}/><select aria-label="Load an input scenario" value=""
              disabled={Boolean(pending)} onChange={e=>{if(e.target.value)action('/preset',{name:e.target.value});}}>
              <option value="">Load a scenario</option><option value="two-tones">Two-tone routing test</option><option value="band-probes">Full-band probes (synthetic)</option><option value="silence">Clear to silence</option></select><ChevronDown size={14}/></div></div>
          <div className="input-grid">{state.channels.slice(bank*8,bank*8+8).map(c=><InputCard key={c.id} channel={c} meter={state.input_meters[c.id-1]||zero}
            onEdit={()=>setEditing(c.id)} update={update}/>)}</div>
          <div className="workspace-footnote"><span><AudioLines size={15}/>32 independent mono inputs. Pair adjacent channels in your DAW for stereo.</span><span>All levels in dBFS</span></div>
          <div className="tip-card"><span className="tip-icon"><Waves size={23}/></span><div><strong>Start with a signal you know.</strong><p>Load the two-tone routing test to check your connections, then choose real stems or add input conditions.</p></div><button onClick={()=>setEditing(bank*8+1)} aria-label="Configure the first input in this bank"><ArrowRight size={20}/></button></div>
        </>:page==='taps'?<NodesPage state={state} monitor={monitor}/>:page==='rehearsal'?<RehearsalPage state={state} monitor={monitor}/>:<OutputPage state={state} fail={setError}/>}
      </main><footer className="statusbar"><span><span className={`tiny-dot ${connected?'connected':''}`}/>{connected?'Receiving from DAW':state?.running?'Ready for DAW connection':'Emulator offline'}</span>
        <span>LOCAL ONLY <i/> {state?.sending?'INPUTS SENDING':'INPUTS PAUSED'}</span></footer></div>
    {editing&&state&&<ChannelEditor key={editing} channel={state.channels[editing-1]} close={()=>setEditing(null)} save={save} fail={setError}/>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
