import {useEffect,useRef,useState} from 'react';
import {Headphones,Square,Volume2} from 'lucide-react';
import {nodeAudio,level} from './api';
import {NodeAudioMonitor,type ListenState} from './monitorAudio';

export function useNodeListening(){
  const [state,setState]=useState<ListenState>({node:null,status:'off',gainDb:-18,error:'',queuedMs:0,
    peakDbfs:-120,resyncs:0,outputRate:0});
  const controller=useRef<NodeAudioMonitor|null>(null);
  if(!controller.current)controller.current=new NodeAudioMonitor(nodeAudio,setState);
  useEffect(()=>()=>controller.current?.stop(),[]);
  return {state,start:(node:Parameters<NodeAudioMonitor['start']>[0])=>controller.current!.start(node),
    stop:()=>controller.current!.stop(),setGain:(db:number)=>controller.current!.setGain(db)};
}
export type NodeListening=ReturnType<typeof useNodeListening>;

export function NodeListenBar({monitor}:{monitor:NodeListening}){
  const s=monitor.state,active=Boolean(s.node);
  return <section className={`node-listen-bar ${active?'listening':''}`} aria-label="Local node listening">
    <Headphones size={22}/><div className="node-listen-heading"><strong>{s.node?
      `${s.status==='starting'?'Connecting':'Listening'}: ${s.node.label} · ${s.node.stage}`:'Listen on this computer'}</strong>
      <p>System default output · choose Listen on a node · one node at a time</p>
      {s.error&&<p className="node-listen-error" role="alert">{s.error}</p>}
      {s.node&&<small>{s.outputRate?s.outputRate/1000+' kHz · ':''}buffered audition
        {s.status==='playing'?` · ${Math.round(s.queuedMs)} ms queued · peak ${level(s.peakDbfs)} dBFS`:''}
        {s.resyncs?` · ${s.resyncs} buffer resync${s.resyncs===1?'':'s'}`:''}</small>}
      {s.node&&s.peakDbfs>=0&&<p className="node-listen-error">Monitor level is reaching full scale. Lower listening volume.</p>}
    </div><label className="node-listen-volume"><span><Volume2 size={15}/>Listen volume <strong>{s.gainDb} dB</strong></span>
      <input type="range" min="-60" max="0" step="1" value={s.gainDb} aria-label="Listen volume"
        onChange={e=>monitor.setGain(Number(e.target.value))}/><small>Only your listening copy</small></label>
    <button className="button secondary" disabled={!active&&!s.error} onClick={monitor.stop}>
      <Square size={14}/>Stop listening</button>
  </section>;
}
