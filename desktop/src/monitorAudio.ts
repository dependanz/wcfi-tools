/** Local audition only. No DAW, input-source, recording or transport controls. */
export type NodeChoice = {id:string;label:string;stage:string};
export type AudioPacket = {rate:number;channels:number;epoch:number;first:number;next:number;
  captureAgeMs:number;reset:boolean;invalid:boolean;samples:Float32Array};
export type ListenState = {node:NodeChoice|null;status:'off'|'starting'|'playing'|'error';
  gainDb:number;error:string;queuedMs:number;peakDbfs:number;resyncs:number;outputRate:number};
type Pull = (id:string,cursor:number|null,epoch:number|null,signal:AbortSignal)=>Promise<AudioPacket>;

export function parseNodeAudio(data:ArrayBuffer):AudioPacket {
  if(data.byteLength<48)throw new Error('Incomplete node audio packet.');
  const v=new DataView(data);
  if(v.getUint32(0,true)!==0x41464357||v.getUint32(4,true)!==1)throw new Error('Unsupported node audio packet.');
  const rate=v.getUint32(8,true),channels=v.getUint32(12,true),epoch=v.getUint32(16,true);
  const first=Number(v.getBigUint64(20,true)),next=Number(v.getBigUint64(28,true));
  const captureAgeMs=v.getFloat64(36,true),flags=v.getUint32(44,true);
  const frames=(data.byteLength-48)/4/channels;
  if(![1,2].includes(channels)||rate<8000||rate>192000||!Number.isInteger(frames)||frames>32768||
    !Number.isSafeInteger(first)||!Number.isSafeInteger(next)||first<0||next-first!==frames||
    !Number.isFinite(captureAgeMs)||captureAgeMs<0||captureAgeMs>=500)
    throw new Error('Invalid or stale node audio packet.');
  return {rate,channels,epoch,first,next,captureAgeMs,reset:Boolean(flags&1),invalid:Boolean(flags&2),
    samples:new Float32Array(data,48)};
}

export class NodeAudioMonitor {
  private context:AudioContext|null=null;
  private gain:GainNode|null=null;
  private sources=new Set<AudioBufferSourceNode>();
  private abort:AbortController|null=null;
  private wake:MessageChannel|null=null;
  private generation=0;
  private nextStart=0;
  private cursor:number|null=null;
  private epoch:number|null=null;
  private lastPublish=0;
  state:ListenState={node:null,status:'off',gainDb:-18,error:'',queuedMs:0,peakDbfs:-120,resyncs:0,outputRate:0};

  constructor(private pull:Pull,private changed:(state:ListenState)=>void,
    private makeContext:()=>AudioContext=()=>new AudioContext({latencyHint:'playback'})){}

  private publish(update:Partial<ListenState>,throttle=false){
    this.state={...this.state,...update};
    if(throttle&&performance.now()-this.lastPublish<100)return;
    this.lastPublish=performance.now();this.changed({...this.state});
  }
  private clearSources(){
    for(const source of this.sources){try{source.stop();source.disconnect();}catch{/* Already ended. */}}
    this.sources.clear();this.nextStart=0;
  }
  private release(){
    this.wake?.port1.close();this.wake?.port2.close();this.wake=null;
    this.abort?.abort();this.abort=null;
    if(this.gain&&this.context){this.gain.gain.cancelScheduledValues(0);this.gain.gain.setValueAtTime(0,this.context.currentTime);}
    this.clearSources();
    const old=this.context;this.context=null;this.gain=null;
    if(old)void old.close().catch(()=>{});
    this.cursor=null;this.epoch=null;
  }
  stop(){
    this.generation++;this.release();
    this.publish({node:null,status:'off',error:'',queuedMs:0,peakDbfs:-120,outputRate:0,resyncs:0});
  }
  setGain(db:number){
    if(!Number.isFinite(db)||db< -60||db>0)throw new Error('Choose a listening volume from −60 to 0 dB.');
    if(this.gain&&this.context)this.gain.gain.setTargetAtTime(10**(db/20),this.context.currentTime,.015);
    this.publish({gainDb:db});
  }
  async start(node:NodeChoice){
    const serial=++this.generation;this.release();
    this.publish({node,status:'starting',error:'',queuedMs:0,peakDbfs:-120,resyncs:0});
    try{
      // Construct/resume directly from the Listen gesture, never on page load.
      const ctx=this.makeContext();this.context=ctx;
      const gain=ctx.createGain();gain.gain.value=10**(this.state.gainDb/20);
      gain.connect(ctx.destination);this.gain=gain;
      this.abort=new AbortController();
      this.wake=new MessageChannel();
      this.wake.port1.onmessage=event=>{if(event.data===this.generation)void this.pump(event.data);};
      await ctx.resume();
      if(serial!==this.generation)return;
      if(ctx.state!=='running')throw new Error('Audio output did not start. Click Listen again.');
      this.publish({outputRate:ctx.sampleRate});
      void this.pump(serial);
    }catch(error){this.fail(serial,error);}
  }
  private fail(serial:number,error:unknown){
    if(serial!==this.generation)return;
    this.generation++;this.release();
    this.publish({node:null,status:'error',error:error instanceof Error?error.message:'Local listening failed.',
      queuedMs:0,peakDbfs:-120,outputRate:0});
  }
  private async pump(serial:number){
    const ctx=this.context,selection=this.state.node,signal=this.abort?.signal;
    if(serial!==this.generation||!ctx||!selection||!signal)return;
    try{
      if(ctx.state!=='running')throw new Error('Listening paused by the audio device. Click Listen again.');
      const packet=await this.pull(selection.id,this.cursor,this.epoch,signal);
      if(serial!==this.generation||ctx!==this.context)return;
      if(packet.invalid)throw new Error('Invalid samples in this node. Listening stopped; inspect the source.');
      const discontinuity=packet.reset||this.epoch!==packet.epoch||
        (this.cursor!==null&&this.cursor!==packet.first);
      if(discontinuity){
        const hadAudio=this.cursor!==null;this.clearSources();
        if(hadAudio)this.publish({resyncs:this.state.resyncs+1});
      }
      this.cursor=packet.next;this.epoch=packet.epoch;
      const frames=packet.samples.length/packet.channels;
      if(frames){
        const queued=this.nextStart-ctx.currentTime;
        if(queued+frames/packet.rate>.60){
          // Never accumulate an old rehearsal behind real time.
          this.clearSources();this.cursor=null;this.epoch=null;
          this.publish({resyncs:this.state.resyncs+1,queuedMs:0,peakDbfs:-120});
        }else{
          const buffer=ctx.createBuffer(packet.channels,frames,packet.rate);
          let peak=0;
          for(let ch=0;ch<packet.channels;ch++){
            const out=buffer.getChannelData(ch);
            for(let i=0;i<frames;i++){
              const value=packet.samples[i*packet.channels+ch];
              if(!Number.isFinite(value))throw new Error('Invalid node audio samples.');
              out[i]=value;peak=Math.max(peak,Math.abs(value));
            }
          }
          if(this.nextStart<ctx.currentTime+.015){
            if(this.nextStart)this.publish({resyncs:this.state.resyncs+1});
            this.nextStart=ctx.currentTime+.15;
          }
          const source=ctx.createBufferSource();source.buffer=buffer;source.connect(this.gain!);
          this.sources.add(source);
          source.onended=()=>{this.sources.delete(source);source.disconnect();};
          source.start(this.nextStart);this.nextStart+=frames/packet.rate;
          this.publish({status:'playing',queuedMs:Math.max(0,this.nextStart-ctx.currentTime)*1000,
            peakDbfs:Math.max(-120,20*Math.log10(Math.max(peak,1e-12)))+this.state.gainDb},true);
        }
      }
      // The server batches ~40 ms of new audio. Message tasks keep this network
      // pump independent of background-window timer throttling.
      this.wake?.port2.postMessage(serial);
    }catch(error){this.fail(serial,error);}
  }
}
