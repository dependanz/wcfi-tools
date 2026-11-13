import assert from 'node:assert/strict';
import test from 'node:test';
import {setImmediate as turn,setTimeout as sleep} from 'node:timers/promises';
import {build} from 'esbuild';

const built=await build({entryPoints:['src/monitorAudio.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const {NodeAudioMonitor,parseNodeAudio}=await import('data:text/javascript;base64,'+
  Buffer.from(built.outputFiles[0].text).toString('base64'));
const choice=id=>({id,label:id,stage:'Before effects'});
function packet({first=0,frames=480,epoch=1,reset=false,channels=2}={}){
  const samples=new Float32Array(frames*channels);
  for(let i=0;i<frames;i++){samples[i*channels]=.25;if(channels===2)samples[i*channels+1]=-.5;}
  return {rate:48000,channels,epoch,first,next:first+frames,captureAgeMs:5,reset,invalid:false,samples};
}
class Context{
  currentTime=0;sampleRate=44100;state='suspended';destination={};sources=[];buffers=[];
  gain={gain:{value:0,cancelScheduledValues(){},setValueAtTime(value){this.value=value;},
    setTargetAtTime(value){this.value=value;}},connect(){}};
  createGain(){return this.gain;}
  async resume(){this.state='running';}
  async close(){this.state='closed';}
  createBuffer(channels,frames,rate){
    const data=Array.from({length:channels},()=>new Float32Array(frames));
    const buffer={rate,data,getChannelData:ch=>data[ch]};this.buffers.push(buffer);return buffer;
  }
  createBufferSource(){
    const source={buffer:null,stopped:false,connect(){},disconnect(){},stop(){this.stopped=true;},
      start(when){this.when=when;}};
    this.sources.push(source);return source;
  }
}

test('construction is silent; stereo sample rate and float values are preserved, with separate gain',async t=>{
  const ctx=new Context();let created=0;
  const m=new NodeAudioMonitor(async()=>packet(),()=>{},()=>{created++;return ctx;});
  t.after(()=>m.stop());assert.equal(created,0);assert.equal(m.state.gainDb,-18);
  await m.start(choice('RED'));await turn();
  assert.equal(created,1);assert.equal(ctx.buffers[0].rate,48000);
  assert.equal(ctx.buffers[0].data[0][0],.25);assert.equal(ctx.buffers[0].data[1][0],-.5);
  assert.equal(ctx.gain.gain.value,10**(-18/20));
  m.setGain(-6);assert.equal(ctx.gain.gain.value,10**(-6/20));
  assert.equal(ctx.buffers[0].data[0][0],.25);
  assert.throws(()=>m.setGain(1));assert.throws(()=>m.setGain(NaN));
});

test('Stop cancels a pending pull and a late response cannot restart playback',async()=>{
  const ctx=new Context();let finish;
  const m=new NodeAudioMonitor(()=>new Promise(resolve=>{finish=resolve;}),()=>{},()=>ctx);
  await m.start(choice('RED'));m.stop();finish(packet());await turn();
  assert.equal(ctx.sources.length,0);assert.equal(ctx.state,'closed');assert.equal(m.state.node,null);
});

test('switching nodes silences the old context and rejects its late audio',async t=>{
  const contexts=[],pending={};
  const m=new NodeAudioMonitor(id=>new Promise(resolve=>{pending[id]=resolve;}),()=>{},()=>{
    const c=new Context();contexts.push(c);return c;});
  t.after(()=>m.stop());
  await m.start(choice('before'));await m.start(choice('after'));
  pending.before(packet());pending.after(packet());await turn();
  assert.equal(contexts[0].state,'closed');assert.equal(contexts[0].sources.length,0);
  assert.equal(contexts[1].sources.length,1);assert.equal(m.state.node.id,'after');
});

test('a discontinuity clears queued audio and playback backlog stays bounded',async t=>{
  const ctx=new Context();let count=0;
  const m=new NodeAudioMonitor(async()=>packet({first:(count++)*9600,frames:9600,reset:count===3}),()=>{},()=>ctx);
  t.after(()=>m.stop());await m.start(choice('RED'));await sleep(180);
  assert.ok(m.state.resyncs>=1);
  assert.ok(ctx.sources.some(s=>s.stopped));
  assert.ok(m.state.queuedMs<=600);
});

test('a disconnected node stops output rather than replaying the last buffer',async()=>{
  const ctx=new Context();
  const m=new NodeAudioMonitor(async()=>{throw new Error('Node disconnected');},()=>{},()=>ctx);
  await m.start(choice('RED'));await turn();
  assert.equal(m.state.status,'error');assert.equal(m.state.node,null);
  assert.equal(ctx.state,'closed');assert.equal(ctx.sources.length,0);
});

test('binary parser validates framing, metadata and stale packets',()=>{
  const data=new ArrayBuffer(48+4*2*2),v=new DataView(data);
  v.setUint32(0,0x41464357,true);v.setUint32(4,1,true);v.setUint32(8,48000,true);
  v.setUint32(12,2,true);v.setUint32(16,3,true);v.setBigUint64(20,10n,true);
  v.setBigUint64(28,12n,true);v.setFloat64(36,8,true);v.setUint32(44,1,true);
  new Float32Array(data,48).set([.25,-.5,.1,.2]);
  const p=parseNodeAudio(data);assert.equal(p.reset,true);assert.equal(p.epoch,3);
  assert.deepEqual([...p.samples].slice(0,2),[.25,-.5]);
  v.setFloat64(36,600,true);assert.throws(()=>parseNodeAudio(data),/stale/);
  assert.throws(()=>parseNodeAudio(new ArrayBuffer(3)),/Incomplete/);
  v.setFloat64(36,8,true);v.setUint32(12,0,true);assert.throws(()=>parseNodeAudio(data));
});
