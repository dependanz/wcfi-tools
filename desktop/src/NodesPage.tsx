import {useEffect, useState} from 'react';
import {AudioLines, Clock3, Headphones, Radio, Square} from 'lucide-react';
import {api, level, type State} from './api';
import type {NodeListening} from './NodeListening';

type Delay = {id:string;label:string;available:boolean;lag_ms?:number|null;correlation?:number;reason?:string};
type Stream = {id:string;pid:number;label:string;stage:string;position:number;active:boolean;signal:boolean;
  peak_dbfs:number[];rms_dbfs:number[];full_scale_samples:number;invalid_samples:number;sample_rate:number;
  channels:number;epoch:number;capture_age_ms:number|null;observation_gaps:number;block_ms:number;
  waveform:{min:number;max:number}[][]};
type NodeView = {available:boolean;sequence:number;streams:Stream[];latencies:Delay[];
  view_age_ms:number|null;compute_ms:number;error:string|null};
const ms=(n?:number|null)=>n==null?'—':`${Math.max(0,n).toFixed(1)} ms`;
const signedMs=(n?:number|null)=>n==null?'—':`${n.toFixed(2)} ms`;
const percentile=(v:number[])=>v.length?[...v].sort((a,b)=>a-b)[Math.min(v.length-1,Math.floor(v.length*.95))]:null;

function Wave({stream}:{stream:Stream}) {
  return <svg className="tap-wave" viewBox="0 0 600 100" role="img" aria-label={`${stream.label} waveform, last 500 milliseconds`}>
    {[0,1,2,3,4].map(n=><line key={n} x1={n*150} x2={n*150} y1="5" y2="95"/>)}
    <line x1="0" x2="600" y1="50" y2="50"/>
    {stream.waveform.slice(0,stream.channels).map((points,ch)=>points.length>1&&<path key={ch} fill={ch?'#896ab0':'#125477'} opacity=".48"
      d={`M ${points.map((w,n)=>`${n*600/(points.length-1)},${50-Math.max(-1,Math.min(1,w.max))*45}`).join(' L ')} L ${[...points].reverse().map((w,n)=>`${(points.length-1-n)*600/(points.length-1)},${50-Math.max(-1,Math.min(1,w.min))*45}`).join(' L ')} Z`}/>)}
  </svg>;
}

export function NodesPage({state,monitor}:{state:State;monitor:NodeListening}) {
  const [view,setView]=useState<NodeView|null>(null), [error,setError]=useState('');
  const [rtt,setRtt]=useState<number[]>([]), [intervals,setIntervals]=useState<number[]>([]);
  const [paint,setPaint]=useState<number|null>(null);
  useEffect(()=>{
    let stopped=false, last:number|null=null, frame=0;
    let timer:ReturnType<typeof setTimeout>;
    async function poll(){
      const start=performance.now();
      try {
        const data=await api<NodeView>('/nodes'), received=performance.now();
        if(stopped)return;
        setView(data);setError('');setRtt(v=>[...v.slice(-99),received-start]);
        if(last!==null){const elapsed=received-last;setIntervals(v=>[...v.slice(-99),elapsed]);}
        last=received;
        cancelAnimationFrame(frame);
        frame=requestAnimationFrame(()=>{if(!stopped)setPaint(performance.now()-received);});
      }catch(e){if(!stopped)setError((e as Error).message);}
      finally{if(!stopped)timer=setTimeout(poll,Math.max(0,100-(performance.now()-start)));}
    }
    poll();return()=>{stopped=true;clearTimeout(timer);cancelAnimationFrame(frame);};
  },[]);
  const fresh=Boolean(view&&!error&&(view.view_age_ms??Infinity)<500);
  const streams=view?.streams??[];
  const active=streams.filter(s=>fresh&&s.active).length;
  return <div className="taps-page">
    <div className="page-heading"><div><span className="eyebrow">WCFI ANALYSIS NODE</span>
      <h1>Follow the signal<span className="heading-dot">.</span></h1>
      <p>See what enters your effects, and what leaves them. Pair nodes by channel name.</p></div>
      <span className={`connection-badge ${active?'connected':''}`}><span/>{active?`${active} nodes receiving`:'Waiting for audio'}</span></div>
    {(error||view?.error)&&<div className="error-banner" role="status">{error||view?.error}</div>}
    <section className="tap-route"><AudioLines size={21}/><div><strong>Before effects → your processing → after effects → track mixer</strong>
      <p>Observation travels directly from each plugin to this app. Your existing audio routing stays in place.</p></div>
      <span className={`tap-loop-state ${state.sending?'active':''}`}>{state.sending?'Inputs looping':'Inputs paused'}</span></section>
    {streams.length>0&&<label className="tap-listen-picker">Choose a node to hear
      <select aria-label="Choose a node to hear" value={monitor.state.node?.id??''}
        onChange={e=>{const node=streams.find(s=>s.id===e.target.value);if(node)void monitor.start(node);else monitor.stop();}}>
        <option value="">Listening off</option>{streams.map(s=><option key={s.id} value={s.id} disabled={!fresh||!s.active}>
          {s.label} · {s.stage}</option>)}
      </select></label>}
    <div className="tap-cards">{streams.map((stream,n)=><article className={`tap-card ${monitor.state.node?.id===stream.id?'node-selected':''}`} key={stream.id}>
      <div className="tap-card-title"><span className="tap-step">{String(n+1).padStart(2,'0')}</span><div><span className="eyebrow">{stream.stage}</span>
        <h2>{stream.label}</h2></div><span className={`tap-signal ${fresh&&stream.signal?'active':''}`}>{!fresh||!stream.active?'Stale':stream.signal?'Signal':'Quiet'}</span></div>
      <div className="tap-wave-label"><span>Last 500 ms · fixed ±1 scale</span><span>{stream.channels===1?'Mono':'Stereo'}</span></div>
      <Wave stream={stream}/><div className="tap-levels">{['L','R'].slice(0,stream.channels).map((side,ch)=><div key={side}>
        <span>{stream.channels===1?'M':side}</span><div className="tap-meter"><i style={{width:`${fresh?Math.max(0,Math.min(100,(stream.peak_dbfs[ch]+60)/60*100)):0}%`}}/></div>
        <strong>{fresh?level(stream.peak_dbfs[ch]):'—'}<small> dBFS peak</small></strong></div>)}</div>
      <footer><span>RMS {fresh?stream.rms_dbfs.slice(0,stream.channels).map(v=>level(v)).join(' / '):'—'} dBFS</span>
        <span>{stream.sample_rate/1000} kHz · {ms(stream.capture_age_ms)} old</span></footer>
      <div className="tap-listen-row"><button className={`button ${monitor.state.node?.id===stream.id?'primary':'secondary'}`}
        disabled={monitor.state.node?.id!==stream.id&&(!fresh||!stream.active)}
        aria-label={`${monitor.state.node?.id===stream.id?'Stop listening to':'Listen to'} ${stream.label} ${stream.stage.toLowerCase()}`}
        aria-pressed={monitor.state.node?.id===stream.id}
        onClick={()=>monitor.state.node?.id===stream.id?monitor.stop():void monitor.start(stream)}>
        {monitor.state.node?.id===stream.id?<Square size={14}/>:<Headphones size={14}/>}
        {monitor.state.node?.id===stream.id?'Listening':'Listen'}</button><span>System default output</span></div>
      {stream.full_scale_samples>0&&stream.active&&fresh&&<p className="tap-clip">{stream.full_scale_samples} samples at or above full scale in the latest 50 ms.</p>}
      {stream.invalid_samples>0&&<p className="tap-clip">Invalid audio samples detected: {stream.invalid_samples}</p>}
      <details><summary>Node details</summary><p>Instance {stream.id.slice(0,8)} · host {stream.pid} · session {stream.epoch}<br/>
        Block {ms(stream.block_ms)} · observation frames skipped {stream.observation_gaps.toLocaleString()}</p></details>
    </article>)}</div>
    {!streams.length&&<div className="tap-wait"><Radio/><h3>Add an Analysis Node to your track</h3>
      <p>Place one before the effects and another after. Give both the same channel name, such as RED, and choose their positions in each plugin’s editor.</p></div>}
    <section className="tap-timing"><div className="section-row"><div><span className="eyebrow">OBSERVED TIMING</span><h3>Audio alignment and app response</h3></div><Clock3 size={22}/></div>
      <div className="tap-delays">{view?.latencies.map(item=><div key={item.id}><span>{item.label}</span>
        <strong>{fresh&&item.available?signedMs(item.lag_ms):'—'}</strong>
        <small>{!fresh?'Waiting for fresh data':item.available?`Waveform match ${((item.correlation??0)*100).toFixed(0)}%`:item.reason}</small></div>)}</div>
      <p className="tap-explanation">Alignment requires two unique nodes and an advancing shared host clock. Positive offsets mean the after signal trails the before signal.
        Delay compensation can align the signals; this does not measure a plugin’s uncompensated latency or physical round trip.</p>
      <div className="tap-health"><div><span>View age at server</span><strong>{ms(view?.view_age_ms)}</strong></div>
        <div><span>API response · p95</span><strong>{ms(percentile(rtt))}</strong></div>
        <div><span>Update interval · p95</span><strong>{ms(percentile(intervals))}</strong></div>
        <div><span>Next animation frame</span><strong>{ms(paint)}</strong></div></div>
      <p className="tap-explanation">Meters use 50 ms; waveforms show 500 ms. View target: 10 updates/s. These timings describe the app, not the stream’s audio delay.</p>
    </section>
    <details className="tap-setup"><summary>Where to place nodes</summary><p>Use the same two positions on audio, group, return and Master tracks.
      Both nodes sit before that track’s fader, pan and mute. On a return, the first node observes the summed send input.
      For the final signal after the Master fader, use Outputs with the emulator selected and observe 23/24.</p></details>
  </div>;
}
