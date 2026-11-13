import {useEffect,useRef,useState} from 'react';
import {AudioLines,Check,FlaskConical,Headphones,LoaderCircle,RefreshCw,Square,Volume2} from 'lucide-react';
import {api,level,rehearsalAudio,type State,type TakeSummary} from './api';
import type {NodeListening} from './NodeListening';
import './rehearsal.css';
import {KitStudy,type StudyReport} from './KitStudy';
import {ReferenceStudy,type ReferenceReport} from './ReferenceStudy';
import {MixBalanceStudy,type BalanceReport} from './MixBalanceStudy';
import {RehearsalSuite,type SuiteReport} from './RehearsalSuite';
import {ProcessingStudy,type ProcessingReport,type ProcessingJudgment} from './ProcessingStudy';
import {KitDiagnostic,type DiagnosticReport} from './KitDiagnostic';
import {KitShelfStudy,type ShelfReport} from './KitShelfStudy';
import {KitCompressionStudy,type CompressionReport} from './KitCompressionStudy';
import {KitBassStudy,type KitBassReport} from './KitBassStudy';
import {RedLowmidStudy,type RedLowmidReport} from './RedLowmidStudy';
import {UpdatedReferenceStudy,type UpdatedReferenceReport,type FullBandReport} from './UpdatedReferenceStudy';
import {FullMixBenchmark,type FullMixBenchmarkReport} from './FullMixBenchmark';

type Metrics={sample_peak_dbfs:number;rms_dbfs:number;crest_db:number;full_scale_samples:number;possible_flat_top_samples:number};
type HF={candidate_count:number;bleed_ambiguous:boolean;kit_high_frequency_overlap_fraction:number|null;treatment_withheld_reason?:string};
type Observation={metrics:Metrics;findings:{kind:string;note:string}[];analyses:{
  sibilance_candidates?:HF;transient_dynamics?:{detected_energy_attacks:number};
  stereo_health?:{correlation?:number|null};cymbal_balance?:{high_frequency_energy_fraction:number}}};
type Proposal={kind:string;gain_db?:number;maximum_reduction_db?:number;reason:string;limited_by_max_attenuation?:boolean};
type Report={kind:'wcfi_role_analysis';profile:{role:string;analyses:string[]};before:Observation;after:Observation;
  proposals:Proposal[];duration_seconds:number;sample_rate:number;channels:number;elapsed_seconds:number;
  paired_alignment_available:boolean;chain_revision:string|null;audio:Record<string,string>;
  candidate?:{observation:Observation};gain_preview?:Observation;
  apply:{supported:boolean;reason?:string;target?:string;from_db?:number;to_db?:number}};
type Applied={status:string;from_db?:number;to_db?:number;note?:string};
export type Job={id:string;study_digest?:string;source:'take'|'nodes'|'study'|'reference'|'balance'|'suite'|'diagnostic';status:string;error?:string;started_at:number;completed_at?:number;
  request:{take_id?:string;start:number;seconds:number};reports:Record<string,Report|StudyReport|ReferenceReport|BalanceReport|SuiteReport|ProcessingReport|DiagnosticReport|ShelfReport|CompressionReport|KitBassReport|RedLowmidReport|UpdatedReferenceReport|FullBandReport|FullMixBenchmarkReport>;applied:Record<string,Applied>};
type Reviews={jobs:Job[];busy:boolean;worker_budget:string};
function diagnosticTitle(job:Job){const r=job.reports.RED??job.reports.KIT??job.reports.MIX;return r&&'title' in r?r.title:'Track diagnostic';}
function studyPassage(job:Job){const report=job.reports.KIT;return report?.kind==='wcfi_kit_context_study'?report.comparison.passage:undefined;}
function referenceTitle(job:Job){const report=job.reports.MIX;return report?.kind==='wcfi_reference_study'?report.title:'Full-mix reference';}
function suiteTitle(job:Job){return job.reports.MIX.kind==='wcfi_processing_study'?'RED & KIT · EQ, compression & de-essing':'RED & KIT · three sessions';}
const modules:Record<string,string>={signal_health:'Signal health',headroom:'Headroom',vocal_dynamics:'Vocal level behaviour',
  sibilance_candidates:'HF burst candidates',transient_dynamics:'Drum attacks & dynamics',cymbal_balance:'Cymbal brightness',stereo_health:'Stereo health'};
const names:Record<string,string>={before:'Before effects',after:'After effects',gain:'Gain-only preview',candidate:'Experimental preview'};

function useAudition(stopNode:()=>void){
  const [playing,setPlaying]=useState(''),[loading,setLoading]=useState(''),[error,setError]=useState('');
  const [gain,setGain]=useState(-18),gainRef=useRef(-18);
  const media=useRef<HTMLAudioElement|null>(null),url=useRef(''),abort=useRef<AbortController|null>(null);
  const generation=useRef(0),position=useRef<{key:string;seconds:number}|null>(null);
  function stop(){generation.current++;abort.current?.abort();abort.current=null;
    media.current?.pause();media.current?.removeAttribute('src');media.current?.load();media.current=null;
    if(url.current)URL.revokeObjectURL(url.current);url.current='';setPlaying('');setLoading('');}
  useEffect(()=>()=>{generation.current++;abort.current?.abort();media.current?.pause();
    if(url.current)URL.revokeObjectURL(url.current);},[]);
  async function listen(job:Job,track:string,variant:string,matched:boolean){
    const key=`${job.id}:${track}`,identifier=`${key}:${variant}:${matched}`;
    if(playing===identifier){stop();return;}
    const previous=playing.startsWith(key+':')&&media.current?media.current.currentTime:0;
    stop();stopNode();setError('');setLoading(identifier);
    position.current={key,seconds:previous};
    const version=generation.current,controller=new AbortController();abort.current=controller;
    try{
      const file=matched&&variant==='after'?'reference_matched':matched&&variant==='candidate'?'candidate_matched':variant;
      const blob=await rehearsalAudio(job.id,track,file,controller.signal);
      if(version!==generation.current)return;
      const object=URL.createObjectURL(blob),audio=new Audio(object);url.current=object;media.current=audio;
      audio.loop=true;audio.volume=10**(gainRef.current/20);
      audio.addEventListener('loadedmetadata',()=>{if(Number.isFinite(audio.duration)&&position.current?.key===key)
        audio.currentTime=Math.min(position.current.seconds,Math.max(0,audio.duration-.01));},{once:true});
      audio.addEventListener('error',()=>{if(version===generation.current){stop();setError('This audio could not be played on the system output.');}});
      await audio.play();
      if(version!==generation.current){audio.pause();return;}
      setPlaying(identifier);setLoading('');
    }catch(e){if(version===generation.current){stop();if((e as Error).name!=='AbortError')setError((e as Error).message);}}
  }
  return{playing,loading,error,gain,stop,listen,setGain:(db:number)=>{gainRef.current=db;setGain(db);if(media.current)media.current.volume=10**(db/20);}};
}
export type Audition=ReturnType<typeof useAudition>;

function TrackReview({name,report,job,audition,busy,apply}: {name:string;report:Report;job:Job;
  audition:ReturnType<typeof useAudition>;busy:boolean;apply:()=>void}){
  const [matched,setMatched]=useState(false);
  const raw=job.source==='take',hf=report.after.analyses.sibilance_candidates;
  const gain=report.proposals.find(p=>p.kind==='software_gain');
  const experimental=report.proposals.some(p=>p.kind==='hf_burst_reduction');
  const applied=job.applied[name];
  const expired=Date.now()/1000-(job.completed_at??0)>600;
  const canApply=Boolean(gain&&report.apply.supported&&!applied&&!expired&&!busy);
  const play=(variant:string)=>void audition.listen(job,name,variant,matched&&['after','candidate'].includes(variant));
  return <article className={`review-track review-${name.toLowerCase()}`}>
    <header className="review-track-heading"><span className="review-track-badge">{name==='RED'?'01':'02'}</span>
      <div><span className="eyebrow">{name==='RED'?'LEAD VOCAL · MICROPHONE':'STEREO DRUM BUS'}</span><h2>{name}</h2></div>
      <span className="review-source">{raw?'Raw source study':'Captured node pair'}</span></header>
    <div className="review-modules">{report.profile.analyses.map(m=><span key={m}>{modules[m]||m}</span>)}</div>
    <div className="review-metric-scroll"><table className="review-metrics"><caption>Same passage · sample measurements</caption><thead><tr><th>Measurement</th><th>{raw?'Raw input':'Pre effects'}</th><th>{raw?'Bypass copy':'Post effects'}</th>{gain&&<th>Gain preview</th>}</tr></thead>
      <tbody>{([['Sample peak','sample_peak_dbfs','dBFS'],['Average RMS','rms_dbfs','dBFS'],['Crest factor','crest_db','dB'],['Full-scale samples','full_scale_samples','']] as const).map(([label,key,unit])=>
        <tr key={key}><th>{label}</th>{[report.before,report.after,...(gain&&report.gain_preview?[report.gain_preview]:[])].map((o,i)=><td key={i}>{key==='full_scale_samples'?o.metrics[key].toLocaleString():level(o.metrics[key])}<small>{unit}</small></td>)}</tr>)}</tbody></table></div>
    <div className="review-findings"><h3>What the measurements suggest</h3>
      {report.before.findings.map((f,i)=><p key={`pre${i}`} className="review-input-note"><strong>Input · </strong>{f.note}</p>)}
      {report.after.findings.map((f,i)=><p key={`post${i}`}><strong>{raw?'Bypass':'After effects'} · </strong>{f.note}</p>)}
      {hf&&<p><strong>{hf.candidate_count} short HF candidates.</strong> {hf.treatment_withheld_reason?
        'Drum context timing is unverified. HF treatment is withheld.':hf.bleed_ambiguous?
        'These overlap KIT brightness; the tool withholds a vocal HF treatment because this may be drum bleed.':
        'These are not confirmed sibilance. A listening preview can help decide whether any treatment is needed.'}</p>}
      {report.after.analyses.transient_dynamics&&<p><strong>{report.after.analyses.transient_dynamics.detected_energy_attacks} energy attacks.</strong> Dynamics and brightness alone do not establish a need for compression or EQ.</p>}
      {!gain&&!experimental&&<p>No supported correction is proposed for this window. Quiet sources are not automatically boosted.</p>}
    </div>
    <section className="review-audition"><div className="section-row"><div><h3>Compare on this computer</h3><p>{raw?'Raw audio and reference processing; not the current Ableton chain.':'Captured before/after audio and local processing previews.'}</p></div><Headphones size={19}/></div>
      <div className="review-listen-buttons">{['before','after',...(gain?['gain']:[]),...(experimental?['candidate']:[])].filter(v=>report.audio[v]).map(variant=>{
        const id=`${job.id}:${name}:${variant}:${matched&&['after','candidate'].includes(variant)}`;
        const active=audition.playing===id,loading=audition.loading===id;
        return <button className={`button ${active?'primary':'secondary'}`} key={variant} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading}
          onClick={()=>play(variant)}>{loading?<LoaderCircle size={14} className="spin"/>:active?<Square size={14}/>:<Headphones size={14}/>}
          {raw&&variant==='before'?'Raw source':raw&&variant==='after'?'Bypass copy':names[variant]}</button>;
      })}</div>
      {experimental&&<label className="review-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>
        RMS-match After / Experimental for a timbre comparison</label>}
      <p className="review-small">{matched?'RMS matching is not LUFS or guaranteed equal perceived loudness. Gain-only preview retains its actual reduction.':
        'Original levels retained. A gain reduction should sound quieter; it does not repair clipped peaks.'} {experimental&&'Experimental preview includes all proposals and uses local reference DSP, not Pro-Q or Pro-DS.'}</p>
    </section>
    <div className="review-proposals"><h3>Proposed fixes</h3>
      {gain&&<div className="review-proposal"><div><span className="review-policy">HEADROOM · NUMERICAL</span><h4>Reduce software gain by {Math.abs(gain.gain_db??0).toFixed(2)} dB</h4>
        <p>{gain.reason}</p>{gain.limited_by_max_attenuation&&<p>The 12 dB reduction limit is reached; this preview may still exceed the configured peak ceiling.</p>}
        {report.apply.supported&&<p>{report.apply.target}: {report.apply.from_db?.toFixed(2)} → {report.apply.to_db?.toFixed(2)} dB</p>}
        {!report.apply.supported&&<p className="review-small">{report.apply.reason}</p>}
        {expired&&!raw&&<p className="review-small">This proposal has expired. Capture current nodes again.</p>}</div>
        <button className="button primary" disabled={!canApply} onClick={apply}><Check size={16}/>
          {applied?.status==='applied'?'Applied':applied?'Inspect required':'Apply gain in Ableton'}</button>
        {applied&&<p className="review-apply-result" role="status">{applied.note||'An earlier apply attempt needs inspection. Capture nodes again before any further action.'}</p>}</div>}
      {experimental&&<div className="review-proposal experimental"><div><span className="review-policy">HF BURSTS · EXPERIMENTAL</span><h4>Audition up to 3 dB of HF reduction</h4>
        <p>Preview only. This follows events in this captured passage; it is not a live de-esser. Listen in context before choosing treatment.</p></div><span className="review-preview-tag">Preview only</span></div>}
      {!report.proposals.length&&<p className="review-small">No change proposed. Try another representative section.</p>}
    </div>
    <details className="review-details"><summary>Measurements, chain snapshot and provenance</summary>
      <p>{report.duration_seconds.toFixed(2)} s · {report.sample_rate/1000} kHz · {report.channels===1?'Mono':'Stereo'} · analysis {report.elapsed_seconds.toFixed(2)} s</p>
      <p>{report.paired_alignment_available?'Samples aligned for paired measurements.':'Paired waveform alignment is unverified; each stage is measured independently.'} Post-effects nodes sit before fader, pan and mute.</p>
      <pre>{JSON.stringify(report,null,2)}</pre></details>
  </article>;
}

export function RehearsalPage({state,monitor}:{state:State;monitor:NodeListening}){
  const [view,setView]=useState<Reviews|null>(null),[takes,setTakes]=useState<TakeSummary[]>([]);
  const [source,setSource]=useState<'take'|'nodes'>('take'),[take,setTake]=useState(state.take?.id??'');
  const [start,setStart]=useState(29),[seconds,setSeconds]=useState(10),[selected,setSelected]=useState('');
  const followLatest=useRef(true);
  const result=useRef<HTMLDivElement|null>(null);
  const [pending,setPending]=useState(false),[error,setError]=useState('');
  const audition=useAudition(monitor.stop);
  useEffect(()=>{audition.stop();},[selected]);
  useEffect(()=>{let stopped=false;let timer:ReturnType<typeof setTimeout>;
    async function poll(){try{const data=await api<Reviews>('/rehearsal?include_references=1&reviewed_passages=1&named_references=1&mix_studies=1&balance_schema=6&candidate_balance=1&role_suites=1&processing_studies=1&kit_diagnostics=1&presence_studies=1&combined_studies=1&compression_studies=1&overlap_studies=1&red_lowmid_studies=1&updated_reference_studies=1&full_band_studies=1&full_mix_benchmarks=1&balance_benchmarks=1&combined_benchmarks=1');if(!stopped){setView(data);setSelected(v=>
      followLatest.current||!data.jobs.some(j=>j.id===v)?data.jobs[0]?.id||'':v);}}
      catch(e){if(!stopped)setError((e as Error).message);}finally{if(!stopped)timer=setTimeout(poll,1000);}}
    void poll();void api<{takes:TakeSummary[]}>('/takes').then(data=>{if(!stopped){setTakes(data.takes);setTake(v=>v||data.takes.find(t=>!t.error)?.id||'');}})
      .catch(e=>{if(!stopped)setError((e as Error).message);});
    return()=>{stopped=true;clearTimeout(timer);};},[]);
  const job=view?.jobs.find(j=>j.id===selected),busy=Boolean(pending||view?.busy);
  useEffect(()=>{audition.stop();},[job?.study_digest]);
  const passageOrder:Record<string,number>={quiet:0,loud:1,fills:2};
  const references=view?.jobs.filter(j=>j.source==='reference'&&j.status==='ready')??[];
  const diagnostics=view?.jobs.filter(j=>j.source==='diagnostic'&&j.status==='ready')??[];
  const balances=view?.jobs.filter(j=>j.source==='balance'&&j.status==='ready')??[];
  const suites=view?.jobs.filter(j=>j.source==='suite'&&j.status==='ready')??[];
  const studies=(view?.jobs.filter(j=>j.source==='study'&&j.status==='ready')??[]).sort((a,b)=>
    (passageOrder[studyPassage(a)?.id??'']??3)-(passageOrder[studyPassage(b)?.id??'']??3));
  function selectReview(id:string,reveal=false){audition.stop();followLatest.current=false;setSelected(id);
    if(reveal)requestAnimationFrame(()=>result.current?.scrollIntoView({block:'start',behavior:'smooth'}));}
  async function analyze(mode=source){audition.stop();followLatest.current=true;setError('');setPending(true);
    try{const data=await api<Reviews>('/rehearsal/analyze',{source:mode,take_id:take,start,seconds});setView(data);setSelected(data.jobs[0]?.id??'');}
    catch(e){setError((e as Error).message);}finally{setPending(false);}}
  async function apply(name:string){if(!job)return;audition.stop();setError('');setPending(true);
    try{await api<Applied>('/rehearsal/apply',{job_id:job.id,track:name,kind:'software_gain'});}
    catch(e){setError((e as Error).message);}finally{try{setView(await api<Reviews>('/rehearsal?include_references=1&reviewed_passages=1&named_references=1&mix_studies=1&balance_schema=6&candidate_balance=1&role_suites=1&processing_studies=1&kit_diagnostics=1&presence_studies=1&combined_studies=1&compression_studies=1&overlap_studies=1&red_lowmid_studies=1&updated_reference_studies=1&full_band_studies=1&full_mix_benchmarks=1&balance_benchmarks=1&combined_benchmarks=1'));}catch{/* Poll surfaces service failures. */}setPending(false);}}
  async function assessPassage(id:string,include:boolean){if(!job)return;audition.stop();setError('');setPending(true);
    try{setView(await api<Reviews>('/rehearsal/assess-reference',{job_id:job.id,assessments:{[id]:{include,
      reason:include?'Operator confirmed a representative band performance.':'Operator excluded this passage from musical reference comparison.'}}}));}
    catch(e){setError((e as Error).message);}finally{setPending(false);}}
  async function assessBalance(include:boolean){if(!job)return;audition.stop();setError('');setPending(true);
    try{setView(await api<Reviews>('/rehearsal/assess-balance',{job_id:job.id,include,
      reason:include?'Operator confirmed coherent band playing after listening.':'Operator has not confirmed a representative band performance.'}));}
    catch(e){setError((e as Error).message);}finally{setPending(false);}}
  async function assessSuite(id:string,include:boolean,verdict:'better'|'same'|'worse'|'unsure'){if(!job)return;audition.stop();setError('');setPending(true);
    try{setView(await api<Reviews>('/rehearsal/assess-suite',{job_id:job.id,passage_id:id,include,verdict,
      reason:`Operator ${include?'confirmed':'has not confirmed'} the performance; full-mix judgment: ${verdict}.`}));}
    catch(e){setError((e as Error).message);}finally{setPending(false);}}
  async function assessProcessing(value:ProcessingJudgment){if(!job)return;audition.stop();setError('');setPending(true);
    try{setView(await api<Reviews>('/rehearsal/assess-processing',{job_id:job.id,...value,
      reason:`Operator judgment for ${value.role} ${value.variant} in ${value.context}: ${value.verdict}.`}));}
    catch(e){setError((e as Error).message);}finally{setPending(false);}}
  return <div className="rehearsal-page"><div className="page-heading"><div><span className="eyebrow">REHEARSAL LAB</span><h1>Measure. Listen. Decide<span className="heading-dot">.</span></h1>
    <p>Role-aware analysis for RED and KIT, with local previews and guarded gain changes.</p></div><FlaskConical size={30}/></div>
    <section className="review-intro"><AudioLines size={23}/><div><strong>RED: lead vocal. KIT: a mixed stereo drum kit.</strong>
      <p>Different sources use different analyses. Proposed treatments are starting points for listening, not a professional-quality score.</p></div></section>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    {!!diagnostics.length&&<section className="review-saved" aria-label="Track diagnostics"><div className="section-row"><div><span className="eyebrow">RED / KIT · DIAGNOSTICS / PROCESSING TESTS</span><h2>Track diagnostics and comparisons</h2>
      <p>Measured before/after signals, reference context and local EQ comparisons. Preview processing is not applied in Ableton.</p></div><AudioLines size={24}/></div>
      <div className="review-saved-links">{diagnostics.map(d=><button key={d.id} className={`button ${selected===d.id?'primary':'secondary'}`} aria-pressed={selected===d.id} onClick={()=>selectReview(d.id,true)}>Open · {diagnosticTitle(d)}</button>)}</div></section>}
    {!!suites.length&&<section className="review-saved" aria-label="Cross-session evaluations"><div className="section-row"><div>
      <span className="eyebrow">THREE SESSIONS · RED / KIT</span><h2>Cross-session evaluation</h2>
      <p>Compare role-specific findings, local processing hypotheses and reversible native headroom tests across the three bands.</p></div><Headphones size={24}/></div>
      <div className="review-saved-links">{suites.map(s=><button key={s.id} className={`button ${selected===s.id?'primary':'secondary'}`}
        aria-pressed={selected===s.id} onClick={()=>selectReview(s.id,true)}>Open · {suiteTitle(s)}</button>)}</div></section>}
    {!!balances.length&&<section className="review-saved" aria-label="Full-mix experiments"><div className="section-row"><div>
      <span className="eyebrow">FULL MIX · BEFORE / AFTER</span><h2>Source balance experiments</h2>
      <p>Captured bridge experiments and clearly labelled local processing previews. Original Ableton settings are preserved.</p></div><Headphones size={24}/></div>
      <div className="review-saved-links">{balances.map(b=><button key={b.id} className={`button ${selected===b.id?'primary':'secondary'}`}
        aria-pressed={selected===b.id} onClick={()=>selectReview(b.id,true)}>Open · {b.reports.MIX.kind==='wcfi_mix_balance_study'?b.reports.MIX.title:'Full-mix experiment'}</button>)}</div></section>}
    {!!references.length&&<section className="review-saved reference-saved" aria-label="Full-mix references"><div className="section-row">
      <div><span className="eyebrow">FULL MIX · SAVED REFERENCES</span><h2>Compare the full mix</h2>
        <p>Separate level-matched references, with measurements and source-specific checks to investigate next.</p></div><Headphones size={24}/></div>
      <div className="review-saved-links">{references.map(reference=><button key={reference.id}
        className={`button ${selected===reference.id?'primary':'secondary'}`} aria-pressed={selected===reference.id}
        onClick={()=>selectReview(reference.id,true)}>Open · {referenceTitle(reference)}</button>)}</div>
      <p className="review-small">Opening the comparison does not start audio. KIT studies remain available below.</p></section>}
    {!!studies.length&&<section className="review-saved" aria-label="Saved comparisons"><div className="section-row">
      <div><span className="eyebrow">SAVED COMPARISONS</span><h2>KIT EQ & compression</h2>
        <p>{studies.some(studyPassage)?'Compare compressor bypass and processing across Sunday’s playing, with KIT on its own or in the full mix.':'Four versions of Sunday’s drums, on their own and in the full mix. Start here to compare the processing.'}</p></div><Headphones size={24}/></div>
      <div className="review-saved-links">{studies.map(study=><button key={study.id} className={`button ${selected===study.id?'primary':'secondary'}`}
        aria-pressed={selected===study.id} onClick={()=>selectReview(study.id,true)}><Headphones size={16}/>
        {studyPassage(study)?`Open · ${studyPassage(study)?.label}`:studies.length===1?'Open KIT comparison':`Open KIT comparison · ${new Date(study.started_at*1000).toLocaleString()}`}</button>)}</div>
      <p className="review-small">Opening a comparison does not start audio. New captures keep these comparisons available; the latest three are saved.</p>
    </section>}
    <section className="review-setup"><div className="bank-tabs" aria-label="Analysis source"><button className={source==='take'?'active':''} disabled={busy} onClick={()=>setSource('take')}>Sunday’s raw stems</button>
      <button className={source==='nodes'?'active':''} disabled={busy} onClick={()=>setSource('nodes')}>Current Ableton nodes</button></div>
      <form onSubmit={e=>{e.preventDefault();void analyze();}}><div className="review-fields">
        {source==='take'?<><label>Recorded take<select value={take} disabled={busy} onChange={e=>setTake(e.target.value)}><option value="">Choose a take</option>{takes.map(t=><option key={t.id} value={t.id} disabled={Boolean(t.error)}>{t.title}</option>)}</select></label>
          <label>Start (seconds)<input type="number" min="0" max="86400" step=".5" required value={start} disabled={busy} onChange={e=>setStart(Number(e.target.value))}/></label></>:
          <p className="review-capture-note">Capture a finite window from the existing RED/KIT nodes. Inputs must already be flowing. No transport, source, routing or monitoring setting is changed.</p>}
        <label>Window (seconds)<input type="number" min=".5" max="20" step=".5" value={seconds} required disabled={busy} onChange={e=>setSeconds(Number(e.target.value))}/></label>
        <button type="submit" className="button primary" disabled={busy||source==='take'&&!take}>{busy?<LoaderCircle className="spin" size={16}/>:<FlaskConical size={16}/>}{busy?'Analysing…':source==='take'?'Analyse stems':'Capture & analyse'}</button>
      </div></form><p className="review-small">{source==='take'?'Reads the original files without sending them into Ableton. The post-effects comparison is a bypass reference.':
        'Captures stay local. A chain change or observation gap invalidates the window. Without a shared host clock, node timing is unverified and drum bleed treatment is withheld.'} One CPU at idle priority · no GPU · no cloud uploads.</p>
      {view?.busy&&<button className="button secondary" onClick={()=>void api<Reviews>('/rehearsal/cancel',{}).then(setView).catch(e=>setError((e as Error).message))}>Cancel analysis</button>}
    </section>
    <section className="review-listening-bar"><Headphones size={21}/><div><strong>{audition.playing?'Audition looping on this computer':audition.loading?'Loading audition…':'Local comparison listening'}</strong>
      <p>Only starts when you choose a comparison. This does not control Ableton’s transport.</p>{audition.error&&<p role="alert">{audition.error}</p>}</div>
      <label><span><Volume2 size={14}/> Listen volume · {audition.gain} dB</span><input type="range" min="-60" max="0" value={audition.gain} onChange={e=>audition.setGain(Number(e.target.value))} aria-label="Comparison listen volume"/></label>
      <button className="button secondary" disabled={!audition.playing&&!audition.loading} onClick={audition.stop}><Square size={14}/>Stop listening</button></section>
    {!!view?.jobs.length&&<div className="review-history"><label>Review window<select value={selected} onChange={e=>selectReview(e.target.value)}>{view.jobs.map(j=><option key={j.id} value={j.id}>
      {new Date(j.started_at*1000).toLocaleTimeString()} · {j.source==='diagnostic'?diagnosticTitle(j):j.source==='suite'?suiteTitle(j):j.source==='balance'?'Full-mix balance experiment':j.source==='reference'?referenceTitle(j):j.source==='study'?studyPassage(j)?.label??'KIT EQ/compression study':j.source==='take'?`Stems ${j.request.start}–${j.request.start+j.request.seconds}s`:'Ableton nodes'} · {j.status}</option>)}</select></label>
      <button className="button secondary" disabled={busy} onClick={()=>void analyze('nodes')}><RefreshCw size={15}/>Capture nodes to verify</button></div>}
    {job?.status==='working'&&<div className="review-empty" role="status"><LoaderCircle className="spin"/><h3>{job.source==='nodes'?'Collecting a continuous node window':'Analysing the selected stem passage'}</h3><p>You can continue using your laptop. Node capture waits up to 45 seconds for input.</p></div>}
    {job?.status==='error'&&<div className="error-banner" role="alert">{job.error}</div>}
    {job?.status==='ready'&&<div className="review-tracks" ref={result} style={{scrollMarginTop:100}}>{Object.entries(job.reports).map(([name,report])=>report.kind==='wcfi_kit_shelf_study'||report.kind==='wcfi_kit_presence_study'||report.kind==='wcfi_kit_combined_study'?
      <KitShelfStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition}/>:
      report.kind==='wcfi_full_mix_benchmark'||report.kind==='wcfi_source_balance_benchmark'||report.kind==='wcfi_full_mix_combined_benchmark'?
      <FullMixBenchmark key={`${job.id}:${name}`} report={report} job={job} audition={audition}/>:
      report.kind==='wcfi_updated_reference_study'||report.kind==='wcfi_full_band_study'?
      <UpdatedReferenceStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition}/>:report.kind==='wcfi_red_lowmid_study'?
      <RedLowmidStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition}/>:report.kind==='wcfi_kit_bass_study'?
      <KitBassStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition}/>:report.kind==='wcfi_kit_compression_study'?
      <KitCompressionStudy key={name} report={report} job={job} audition={audition}/>:report.kind==='wcfi_kit_diagnostic'?
      <KitDiagnostic key={`${job.id}:${name}`} report={report} job={job} audition={audition}/>:
      report.kind==='wcfi_processing_study'?
      <ProcessingStudy key={`${job.id}:${name}:${job.study_digest??''}`} report={report} job={job} audition={audition} busy={busy} assess={value=>void assessProcessing(value)} confirm={(id,include)=>void assessSuite(id,include,'unsure')}/>:
      report.kind==='wcfi_rehearsal_suite'?
      <RehearsalSuite key={`${job.id}:${name}:${job.study_digest??''}`} report={report} job={job} audition={audition} busy={busy} assess={(id,include,verdict)=>void assessSuite(id,include,verdict)}/>:
      report.kind==='wcfi_mix_balance_study'?
      <MixBalanceStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition} busy={busy} assess={include=>void assessBalance(include)}/>:
      report.kind==='wcfi_reference_study'?
      <ReferenceStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition} busy={busy} assess={(id,include)=>void assessPassage(id,include)}/>:
      report.kind==='wcfi_kit_context_study'?
      <KitStudy key={`${job.id}:${name}`} report={report} job={job} audition={audition} studies={studies} selectReview={id=>selectReview(id)}/>:
      <TrackReview key={`${job.id}:${name}`} name={name} report={report} job={job} audition={audition} busy={busy} apply={()=>void apply(name)}/>)}</div>}
    {!job&&<div className="review-empty"><FlaskConical/><h3>Start with a representative passage</h3><p>Try 29–49 seconds for sustained drums, then a second passage to check whether the same problem persists.</p></div>}
  </div>;
}
