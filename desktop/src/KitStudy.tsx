import {useState} from 'react';
import {Headphones,LoaderCircle,Square,SlidersHorizontal} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';
import {level} from './api';

type Stats={sample_peak_dbfs:number;rms_dbfs:number;crest_db:number;
  band_energy_fractions:{sub_30:number;low_30_100:number;lowmid_250_500:number};
  loudness:{integrated_lufs:number;true_peak_dbtp:number}};
export type StudyReport={kind:'wcfi_kit_context_study';audio:Record<string,string>;comparison:{
  duration_seconds:number;variants:{id:string;label:string;origin:string}[];
  passage?:{id:string;label:string;start_seconds:number;duration_seconds:number;selection_note:string;musical_label_verified:boolean;
    performance_review?:{include:boolean;reason:string;classification?:string}};
  analysis?:{input:{metrics:{full_scale_samples:number;possible_flat_top_samples:number}};preference_established:boolean};
  eq:{type:string;hz:number;gain_db:number;placement:string;status:string};
  compression_change:{parameter:string;before:string;after:string}[];
  matching:Record<string,{target_lufs:number;original_balance_safety_gain_db:number;method:string;kit_contribution_matched?:boolean;kit_reference_lufs?:number}>;
  measurements:Record<string,Record<string,Stats>>;notes:string[];
  validation:{linear_mix_null_relative_error:number};path:{scope:string;master_fader_display:string}}};

export function KitStudy({report,job,audition,studies=[],selectReview}:{report:StudyReport;job:Job;audition:Audition;studies?:Job[];selectReview?:(id:string)=>void}){
  const [scope,setScope]=useState<'mix'|'kit'>('mix'),[matched,setMatched]=useState(true);
  const c=report.comparison,stats=c.measurements[scope];
  return <section className="review-track kit-study"><header className="review-track-heading"><span className="review-track-badge"><SlidersHorizontal size={22}/></span>
    <div><span className="eyebrow">KIT · EQ & COMPRESSION</span><h2>Judge it in the mix</h2></div><span className="review-source">{c.duration_seconds}-second aligned loop</span></header>
    <div className="study-body">
      {c.passage&&<section className="study-passage"><h3>{c.passage.label} · {c.passage.start_seconds}–{c.passage.start_seconds+c.duration_seconds}s</h3>
        <div className="review-saved-links" aria-label="Comparison passage">{studies.map(study=>{
          const r=study.reports.KIT;if(r?.kind!=='wcfi_kit_context_study'||!r.comparison.passage)return null;
          return <button className={`button ${study.id===job.id?'primary':'secondary'}`} key={study.id} aria-pressed={study.id===job.id}
            onClick={()=>selectReview?.(study.id)}>{r.comparison.passage.label}</button>;
        })}</div><p className="review-small">{c.passage.performance_review?
          `${c.passage.performance_review.reason} ${c.passage.performance_review.include?'Approved for the full-mix reference comparison.':'Technical signal checks only; excluded from musical mix-quality validation.'}`:
          `${c.passage.selection_note} ${!c.passage.musical_label_verified?'Chorus/fill labels need listening confirmation.':''}`}</p></section>}
      <p className="study-lead">{c.passage?'Compare actual compressor bypass with the current chain, a slower attack, a little less low end, or both.':'Compare the current chain with a slower compressor attack, a little less low end, or both.'} The original Ableton settings are restored.</p>
      <div className="bank-tabs" aria-label="Comparison scope">{(['mix','kit'] as const).map(s=><button key={s} className={scope===s?'active':''}
        onClick={()=>{audition.stop();setScope(s);}}>{s==='mix'?'Full mix':'KIT on its own'}</button>)}</div>
      <p className="review-small">{scope==='mix'?`Captured Master node before its fader. ${c.passage?'Bypass, Current and Slower attack':'Current and Slower attack'} use actual Ableton captures; EQ and KIT level matching use validated local mix previews.`:
        'Captured KIT after its existing processing. The EQ options add local reference DSP after the compressor.'} {c.duration_seconds} seconds · stereo 48 kHz.</p>
      <label className="review-match study-level-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>
        {c.passage?'Match KIT loudness for comparison':'Match integrated loudness for comparison'}</label>
      <p className="review-small">{matched?(c.matching[scope].kit_contribution_matched?
        `KIT is matched to Current chain (${c.matching[scope].kit_reference_lufs?.toFixed(1)} LUFS before common audition gain); the rest of the band stays fixed. Total mix loudness may vary slightly.`:
        `EBU R128 matched to ${c.matching[scope].target_lufs.toFixed(1)} LUFS, within scanner precision. This is audition volume, not a broadcast target.`):
        `Original relative levels with ${c.matching[scope].original_balance_safety_gain_db.toFixed(1)} dB of shared audition safety gain.`} Listen volume above controls this computer only.</p>
      <div className="study-options">{c.variants.map(v=>{
        const key=`study_${scope}_${v.id}${matched?'_matched':''}`,id=`${job.id}:KIT:${key}:false`;
        const active=audition.playing===id,loading=audition.loading===id;
        return <article key={v.id} className={`study-option ${active?'selected':''}`}>
          <span className="eyebrow">{v.id==='bypass'?'NATIVE BYPASS':v.id==='current'?'REFERENCE':v.id==='compression'?'NATIVE COMPRESSION':'LOCAL EQ PREVIEW'}</span>
          <h3>{v.label}</h3><p>{v.origin}</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading}
            onClick={()=>void audition.listen(job,'KIT',key,false)}>{loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}
            {active?'Listening':'Listen'} · {v.label}</button></article>;
      })}</div>
      <div className="study-recipes"><div><h3>Compression option</h3><p>Attack: {c.compression_change[0]?.before} → {c.compression_change[0]?.after}. Threshold, ratio, release and Output Gain stay unchanged.</p></div>
        <div><h3>EQ option</h3><p>{c.eq.gain_db} dB low shelf at {c.eq.hz} Hz, {c.eq.placement}. {c.eq.status}.</p></div></div>
      <p className="study-listen-note">Listen for kick/snare definition, bass clarity and whether RED stays easy to follow. A louder or brighter drum sound is not automatically better. Keep the current chain if a candidate does not help.</p>
      {c.analysis&&<div className="study-analysis"><h3>What this passage establishes</h3>
        <p>Raw KIT: {c.analysis.input.metrics.full_scale_samples.toLocaleString()} full-scale samples; {c.analysis.input.metrics.possible_flat_top_samples.toLocaleString()} possible flat-top samples.
          {!!(c.analysis.input.metrics.full_scale_samples||c.analysis.input.metrics.possible_flat_top_samples)&&' These are clipping indicators; the recording does not establish where upstream clipping happened.'}</p>
        <p>Bypass also disables the compressor’s Output Gain. Keep KIT loudness matching on to judge its processing. No version has been established as the musical winner.</p>
        <p>These tests isolate KIT processing. The overall gap to a professional church mix still needs a full-mix reference comparison.</p></div>}
      <details className="study-measurements"><summary>Measurements and comparison checks</summary>
        <p className="review-small">These measurements precede audition loudness matching. The Master node precedes its {c.path.master_fader_display} fader; positive floating-point values here do not establish clipping at the stream output.</p>
        <div className="review-metric-scroll"><table className="review-metrics"><thead><tr><th>Measurement</th>{c.variants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
          {([['Sample peak','sample_peak_dbfs','dBFS'],['Average RMS','rms_dbfs','dBFS'],['Crest factor','crest_db','dB']] as const).map(([label,key,unit])=><tr key={key}><th>{label}</th>{c.variants.map(v=><td key={v.id}>{level(stats[v.id][key])}<small>{unit}</small></td>)}</tr>)}
          <tr><th>Integrated loudness</th>{c.variants.map(v=><td key={v.id}>{stats[v.id].loudness.integrated_lufs.toFixed(1)}<small>LUFS</small></td>)}</tr>
        </tbody></table></div>
        <p className="review-small">The measured full-mix change agrees with the changed KIT contribution: relative null residual {(c.validation.linear_mix_null_relative_error*100).toFixed(3)}%. This check permits the local EQ substitution; it does not establish musical improvement.</p>
        {c.notes.map((note,i)=><p className="review-small" key={i}>{note}</p>)}
        <pre>{JSON.stringify(c,null,2)}</pre></details>
    </div></section>;
}
