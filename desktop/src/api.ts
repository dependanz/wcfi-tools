import {parseNodeAudio} from './monitorAudio';

export type Source = { kind: string; level_dbfs?: number; frequency_hz?: number; path?: string;
  file_channel?: number; original_name?: string; file_channels_count?: number; start_seconds?: number;
  bpm?: number; midi_notes?: number[]; kick_db?: number; snare_db?: number; hat_db?: number;
  take_id?: string; track_name?: string };
export type Fault = {kind: string; [key: string]: unknown};
export type Channel = {id: number; name: string; role: string; enabled: boolean; muted: boolean;
  solo: boolean; gain_db: number; source: Source; faults: Fault[]; seconds: number};
export type Meter = {peak_dbfs: number; rms_dbfs: number; clips: number};
export type TakeSummary = {id:string;title:string;frames:number;sample_rate:number;duration_seconds:number;
  source_count:number;input_channel_count:number;error?:string|null};
export type RecordedTakeState = TakeSummary & {start_frame:number;end_frame:number;fade_ms:number;
  position_frame:number;mapped_inputs:number[];checksums_verified:boolean};
export type State = {running: boolean; sending: boolean; error: string | null; warning?:string|null;
  take:RecordedTakeState|null; revision: number;
  sample_rate: number; input_count: number; output_count: number; channels: Channel[];
  input_meters: Meter[]; output_meters: Meter[]; output_connected: boolean;
  driver: null | {running: number; host_pid: number; blocksize: number; underruns: number;
    output_drops: number; late_callbacks: number; service_active: number; version: number}};
export type AnalysisResult = {channel: number; peak_dbfs: number; rms_dbfs: number; crest_db: number;
  dominant_hz: number | null; clipped_samples: number; dc_offset: number; notes: string[];
  spectrum: {hz: number; dbfs: number}[]; waveform: {min: number; max: number}[]};
export type Analysis = {available: boolean; results: AnalysisResult[]; correlation: number | null};

const fragment = new URLSearchParams(location.hash.slice(1));
const key = 'wcfi-emulator-session';
const supplied = fragment.get('token');
if (supplied) { sessionStorage.setItem(key, supplied); history.replaceState(null, '', location.pathname); }
const token = supplied || sessionStorage.getItem(key) || '';

export async function api<T = State>(path: string, data?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {method: data === undefined ? 'GET' : 'POST',
    headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The emulator could not complete that request.');
  return result as T;
}

export async function rehearsalAudio(job:string,track:string,variant:string,signal:AbortSignal):Promise<Blob>{
  const query=new URLSearchParams({job,track,variant});
  const response=await fetch(`/api/rehearsal/audio?${query}`,{
    headers:{Authorization:`Bearer ${token}`},signal,cache:'no-store'});
  if(!response.ok){const error=await response.json();throw new Error(error.error||'Could not load this audition.');}
  return response.blob();
}

export async function importAudio(file: File): Promise<{path: string; name: string; seconds: number; channels: number}> {
  const response = await fetch(`/api/import?name=${encodeURIComponent(file.name)}`, {
    method: 'POST', headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/octet-stream'}, body: file});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Could not import this audio file.');
  return result;
}

export async function nodeAudio(id:string,cursor:number|null,epoch:number|null,signal:AbortSignal){
  const query=new URLSearchParams({id});
  if(cursor!==null&&epoch!==null){query.set('cursor',String(cursor));query.set('epoch',String(epoch));}
  const abort=new AbortController(),cancel=()=>abort.abort();
  signal.addEventListener('abort',cancel,{once:true});
  if(signal.aborted)abort.abort();
  const timeout=setTimeout(cancel,2000);
  try{
    const response=await fetch(`/api/nodes/audio?${query}`,{
      headers:{Authorization:`Bearer ${token}`},signal:abort.signal,cache:'no-store'});
    if(!response.ok){const result=await response.json();throw new Error(result.error||'Node listening is unavailable.');}
    return parseNodeAudio(await response.arrayBuffer());
  }finally{clearTimeout(timeout);signal.removeEventListener('abort',cancel);}
}

export const level = (n?: number | null) => n == null ? '—' : n <= -119 ? '−∞' : n.toFixed(1);
export const sourceNames: Record<string,string> = {silence: 'No source', file: 'Audio file', tone: 'Sine wave',
  noise: 'Noise', notes: 'Note pattern', 'drum-kit': 'Mixed drum kit', take:'Recorded take'};
