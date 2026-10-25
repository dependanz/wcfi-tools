import {useState} from 'react';
import {Headphones,LoaderCircle,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';
import {PassageEvidence,type SelectionEvidence} from './PassageEvidence';

type Role='red'|'kit'|'both';
type Treatment='eq'|'compression'|'deess'|'chain';
type Verdict='better'|'same'|'worse'|'unsure';
type Metrics={sample_peak_dbfs:number;rms_dbfs:number;crest_db:number};
type Reduction={enabled:boolean;maximum_reduction_db:number;mean_reduction_db?:number;reduced_fraction?:number};
type Recipe={role:string;eq:{enabled:boolean;bands:{kind:string;hz:number;gain_db?:number;q?:number}[];reason:string};
  compression:{enabled:boolean;ratio:number;threshold_dbfs:number;attack_ms:number;release_ms:number;maximum_reduction_db:number;reason:string};
  deess:{enabled:boolean;eligible_events:number;rejected_kit_overlap_events:number;maximum_reduction_db:number;reason:string;
    events:{start_seconds:number;end_seconds:number}[]};
  metrics:{low_mid_contrast_db:number;low_mid_fraction:number;upper_level_tail_db:number}};
type Track={recipe:Recipe;processing_stats:{compression:Reduction;deess:Reduction;chain:{compression:Reduction;deess:Reduction}};
  input_metrics:{full_scale_samples:number};input_sample_count:number;notes:string[];
  identical_controls:Record<string,boolean>;signal_metrics:Record<string,Metrics>};
type Passage={id:string;date:string;take_title:string;start_seconds:number;duration_seconds:number;operator_approved:boolean;
  review?:{include:boolean;origin?:string;reason?:string};selection_evidence?:SelectionEvidence;tracks:Record<'red'|'kit',Track>;baseline_sum_relative_error:number;
  source_loudness_compensation_db:Record<string,Record<string,number>>;
  listening:Record<string,{target_lufs:number;matched:Record<string,{integrated_lufs:number;true_peak_dbtp:number}>}>};
export type ProcessingReport={kind:'wcfi_processing_study';title:string;passages:Passage[];scope:string;matching_note:string;limits:string[];
  processing_reviews?:Record<string,{verdict:Verdict;reason:string}>;audio:Record<string,string>};
export type ProcessingJudgment={passage_id:string;role:Role;variant:Treatment;context:'solo'|'mix';verdict:Verdict};
const labels:Record<Treatment,string>={eq:'EQ',compression:'Compression',deess:'De-essing',chain:'Full chain'};
const time=(seconds:number)=>`${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;

export function ProcessingStudy({report,job,audition,busy,assess,confirm}:{report:ProcessingReport;job:Job;audition:Audition;busy:boolean;
  assess:(value:ProcessingJudgment)=>void;confirm:(id:string,include:boolean)=>void}){
  const [selected,setSelected]=useState((report.passages.find(p=>p.selection_evidence)??report.passages.find(p=>p.review?.include??p.operator_approved)??report.passages[0]).id),[role,setRole]=useState<Role>('red');
  const [treatment,setTreatment]=useState<Treatment>('chain'),[context,setContext]=useState<'mix'|'solo'>('mix'),[matched,setMatched]=useState(true);
  const p=report.passages.find(v=>v.id===selected)??report.passages[0];
  const excluded=p.review?.origin==='operator'&&!p.review.include;
  const scope=context==='mix'?'mix':role;
  const variant=context==='mix'?(role==='both'?'both_chain':`${role}_${treatment}`):treatment;
  const caseKey=`${p.id}:${role}:${treatment}:${context}`;
  const review=report.processing_reviews?.[caseKey]??{verdict:'unsure',reason:'No listening judgment saved for this treatment and context.'};
  const options:Treatment[]=role==='both'?['chain']:role==='red'?['eq','compression','deess','chain']:['eq','compression','chain'];
  const activeRoles:('red'|'kit')[]=role==='both'?['red','kit']:[role];
  const unchanged=activeRoles.every(r=>p.tracks[r].identical_controls[treatment]);
  function changeRole(value:Role){audition.stop();setRole(value);if(value==='both'){setTreatment('chain');setContext('mix');}
    else if(value==='kit'&&treatment==='deess')setTreatment('chain');}
  function player(value:string,label:string){const key=`process_${p.id}_${scope}_${value}${matched?'_matched':''}`,id=`${job.id}:MIX:${key}:false`;
    const active=audition.playing===id,loading=audition.loading===id;
    return <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading}
      onClick={()=>void audition.listen(job,'MIX',key,false)}>{loading?<LoaderCircle size={15} className="spin"/>:active?<Square size={15}/>:<Headphones size={15}/>}
      {active?'Listening':'Listen'} · {label}</button>;}
  return <section className="review-track processing-study"><header className="review-track-heading"><span className="review-track-badge"><Headphones size={22}/></span>
    <div><span className="eyebrow">CROSS-SESSION · LOCAL DSP PREVIEWS</span><h2>{report.title}</h2></div></header><div className="study-body">
    <p className="study-lead">Compare individual treatments and their combined chain against the same current capture. These are local reference processors; no Ableton plug-ins or faders have been edited.</p>
    <div className="bank-tabs" aria-label="Processing session">{report.passages.map(v=><button key={v.id} className={p.id===v.id?'active':''}
      aria-pressed={p.id===v.id} onClick={()=>{audition.stop();setSelected(v.id);}}>{v.date.slice(5)} · {time(v.start_seconds)}–{time(v.start_seconds+v.duration_seconds)}{v.review?.origin==='operator'&&!v.review.include?' · excluded':''}</button>)}</div>
    <h3>{p.take_title}</h3><label className="review-match study-level-match"><input type="checkbox" checked={p.review?.include??p.operator_approved} disabled={busy}
      onChange={e=>confirm(p.id,e.target.checked)}/>Representative band performance</label>
    <PassageEvidence evidence={p.selection_evidence}/>
    {excluded?<section className="study-passage" role="note"><h3>Excluded from evaluation</h3><p>{p.review?.reason}</p>
      <p className="review-small">Processing comparisons and ratings are withheld for this passage. The original recordings and archived measurements are preserved.</p></section>:<>
    <p className="review-small">{(p.review?.include??p.operator_approved)?'Operator-confirmed passage.':p.selection_evidence?'Machine-screened source activity; no operator performance confirmation or quality verdict is implied.':'Activity-selected candidate; coherent performance still needs your confirmation.'}</p>
    <div className="study-options"><div><h3>Source to process</h3><div className="bank-tabs" aria-label="Processing role">{(['red','kit','both'] as const).map(r=><button key={r}
      className={role===r?'active':''} aria-pressed={role===r} onClick={()=>changeRole(r)}>{r==='both'?'Both':r.toUpperCase()}</button>)}</div></div>
      <div><h3>Listen in context</h3><div className="bank-tabs" aria-label="Processing context">{(['mix','solo'] as const).map(c=><button key={c} disabled={role==='both'&&c==='solo'}
        className={context===c?'active':''} aria-pressed={context===c} onClick={()=>{audition.stop();setContext(c);}}>{c==='mix'?'Full mix':'Solo'}</button>)}</div></div></div>
    <div className="bank-tabs" aria-label="Processing treatment">{options.map(v=><button key={v} className={treatment===v?'active':''} aria-pressed={treatment===v}
      onClick={()=>{audition.stop();setTreatment(v);}}>{labels[v]}</button>)}</div>
    <label className="review-match study-level-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>Match treatment and mix loudness</label>
    <p className="review-small">{matched?`${context==='mix'?'Full-mix':'Solo'} copies verified at ${p.listening[scope].target_lufs.toFixed(1)} LUFS. ${context==='mix'?'Treated sources are loudness-matched before reinsertion; full mixes then receive a scalar listening match.':''}`:
      'Original processing level changes with one safe gain per scope. No source loudness compensation.'}</p>
    <div className="study-options"><article className="study-option"><span className="eyebrow">CAPTURED BASELINE</span><h3>Current</h3><p>{context==='mix'?'Full mix with the existing chain and balance.':`${role.toUpperCase()} after its existing chain and fader.`}</p>
      {player('current','Current capture')}</article><article className="study-option"><span className="eyebrow">{unchanged?'IDENTICAL CONTROL':'LOCAL DSP CANDIDATE'}</span>
      <h3>{role==='both'?'Both chains':`${role.toUpperCase()} · ${labels[treatment]}`}</h3><p>{unchanged?'The rule kept this treatment bypassed; no sonic difference is expected.':
        'Only the selected source treatment changes before the global listening gain. This is a hypothesis to judge, not an established improvement.'}</p>{player(variant,'Selected treatment')}</article></div>
    <p className="study-listen-note">{treatment==='eq'?'Listen for reduced congestion while preserving useful warmth, vocal body and drum weight. A lower spectral ratio alone is not better.':
      treatment==='compression'?'Listen for steadier phrases or drum body while preserving attacks, cymbal tails and space. Reject pumping, flattened hits or more distracting bleed.':
      treatment==='deess'?'Focus on the listed HF bursts. Check whether consonants become less piercing without lisping, dullness or obvious whole-vocal ducking.':
      'Judge whether the lead remains natural and easy to follow, and whether the drums keep their weight and impact. Compare individual treatments before blaming the entire chain.'}</p>
    {activeRoles.map(r=>{const track=p.tracks[r],recipe=track.recipe;
      const comp=treatment==='chain'?track.processing_stats.chain.compression:track.processing_stats.compression;
      const dess=treatment==='chain'?track.processing_stats.chain.deess:track.processing_stats.deess;
      const current=track.signal_metrics.current,candidate=track.signal_metrics[treatment];
      return <section key={r} className="study-analysis processing-role-details"><h3>{r.toUpperCase()} · decision evidence and recipe</h3>
        <p><strong>EQ: {recipe.eq.enabled?'audition candidate':'keep current'}</strong>. {recipe.eq.reason}</p>
        {recipe.eq.bands.map((b,i)=><p key={i}>{b.kind==='highpass'?`High-pass ${b.hz} Hz, 12 dB/octave`:`Bell ${b.hz} Hz · ${b.gain_db?.toFixed(2)} dB · Q ${b.q}`}</p>)}
        <p className="review-small">Low-mid density contrast: {recipe.metrics.low_mid_contrast_db.toFixed(1)} dB. Low-mid energy fraction: {(100*recipe.metrics.low_mid_fraction).toFixed(1)}%. These can include wanted fundamentals.</p>
        <p><strong>Compression: {recipe.compression.enabled?'audition candidate':'keep current'}</strong>. {recipe.compression.reason}</p>
        {recipe.compression.enabled&&<p>{recipe.compression.ratio}:1 · threshold {recipe.compression.threshold_dbfs.toFixed(1)} dBFS · attack {recipe.compression.attack_ms} ms · release {recipe.compression.release_ms} ms · no makeup.
          Measured maximum reduction {comp.maximum_reduction_db.toFixed(2)} dB{comp.mean_reduction_db!==undefined?`, mean ${comp.mean_reduction_db.toFixed(2)} dB`:''}.</p>}
        {r==='red'&&<><p><strong>De-essing: {recipe.deess.enabled?'audition candidate':'keep current'}</strong>. {recipe.deess.reason}</p>
          <p>{recipe.deess.eligible_events} eligible bursts; {recipe.deess.rejected_kit_overlap_events} vetoed for KIT overlap. Maximum measured reduction {dess.maximum_reduction_db.toFixed(2)} dB. These are not confirmed sibilants.</p>
          {!!recipe.deess.events.length&&<p className="review-small">Within this excerpt: {recipe.deess.events.slice(0,8).map(e=>`${e.start_seconds.toFixed(2)}–${e.end_seconds.toFixed(2)}s`).join(', ')}.</p>}</>}
        {track.input_metrics.full_scale_samples>0&&<p><strong>Input note:</strong> {(100*track.input_metrics.full_scale_samples/track.input_sample_count).toFixed(3)}% of pre-effects samples reach full scale. These treatments do not reconstruct missing input peaks.</p>}
        <div className="review-metric-scroll"><table className="review-metrics"><caption>{r.toUpperCase()} · before audition gain</caption><thead><tr><th>Measurement</th><th>Current</th><th>{labels[treatment]}</th></tr></thead>
          <tbody>{([['Sample peak','sample_peak_dbfs'],['RMS','rms_dbfs'],['Crest','crest_db']] as const).map(([label,key])=><tr key={key}><th>{label}</th><td>{current[key].toFixed(2)} dB</td><td>{candidate[key].toFixed(2)} dB</td></tr>)}</tbody></table></div>
        {track.notes.map((note,i)=><p key={i} className="review-small">{note}</p>)}</section>;
    })}
    <section className="study-passage"><h3>Your judgment · {role.toUpperCase()} · {labels[treatment]} · {context==='mix'?'full mix':'solo'}</h3>
      <div className="review-saved-links">{(['better','same','worse','unsure'] as const).map(v=><button key={v} className={`button ${review.verdict===v?'primary':'secondary'}`}
        aria-pressed={review.verdict===v} disabled={busy} onClick={()=>assess({passage_id:p.id,role,variant:treatment,context,verdict:v})}>{v[0].toUpperCase()+v.slice(1)}</button>)}</div>
      <p className="review-small">{review.reason} Saved separately for each source, treatment and context; listening and rating do not apply settings to Ableton.</p></section>
    </>}
    <p className="review-small">{report.scope}</p><p className="review-small">{report.matching_note}</p>
    {report.limits.map((note,i)=><p key={i} className="review-small">{note}</p>)}
    <details className="study-measurements"><summary>Recipes, measurements and comparison checks</summary><p>Baseline linear sum residual: {(100*p.baseline_sum_relative_error).toFixed(4)}%.</p><pre>{JSON.stringify(report,null,2)}</pre></details>
  </div></section>;
}
