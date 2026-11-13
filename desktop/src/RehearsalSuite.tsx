import {useState} from 'react';
import {Headphones,LoaderCircle,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';
import {PassageEvidence,type SelectionEvidence} from './PassageEvidence';

type Metrics={sample_peak_dbfs:number;rms_dbfs:number;crest_db:number;full_scale_samples:number;possible_flat_top_samples:number};
type Observation={metrics:Metrics;findings:{kind:string;note:string}[];analyses:{
  sibilance_candidates?:{candidate_count:number;kit_high_frequency_overlap_fraction:number|null};
  transient_dynamics?:{detected_energy_attacks:number;energy_level_spread_p90_p10_db:number};
  cymbal_balance?:{high_frequency_energy_fraction:number};stereo_health?:{correlation:number}}};
type Track={profile:{role:string;analyses:string[]};frames:number;channels:number;before:Observation;after:Observation;
  paired_change:{crest_change_db:number};decision:{kind:string;label:string;gain_db:number;target_stage:string;reason:string};
  candidate_observation:Observation;candidate_mixer_peak_dbfs:number;current_mixer_peak_dbfs:number;
  validation:{gain_relative_error:number;node_unchanged:boolean};notes:string[]};
type Review={include:boolean;verdict:'better'|'same'|'worse'|'unsure';reason:string;origin?:string};
type Passage={id:string;date:string;take_title:string;start_seconds:number;duration_seconds:number;
  operator_approved:boolean;review:Review;selection_evidence?:SelectionEvidence;tracks:Record<'RED'|'KIT',Track>;
  validation:{full_mix_delta_relative_error:number;candidate_native_capture:boolean;identical_control:boolean;baseline_sum_relative_error:number};
  listening:Record<string,{target_lufs:number;original_safety_gain_db:number}>};
export type SuiteReport={kind:'wcfi_rehearsal_suite';title:string;passages:Passage[];scope:string;limits:string[];audio:Record<string,string>};

const time=(seconds:number)=>`${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;
export function RehearsalSuite({report,job,audition,busy,assess}:{report:SuiteReport;job:Job;audition:Audition;busy:boolean;
  assess:(passage:string,include:boolean,verdict:Review['verdict'])=>void}){
  const [selected,setSelected]=useState((report.passages.find(p=>p.selection_evidence)??report.passages.find(p=>p.review?.include??p.operator_approved)??report.passages[0]).id),[matched,setMatched]=useState(true);
  const passage=report.passages.find(p=>p.id===selected)??report.passages[0];
  const review=passage.review??{include:passage.operator_approved,verdict:'unsure',reason:'Awaiting your listening judgment.'};
  const excluded=review.origin==='operator'&&!review.include;
  function player(scope:string,variant:string,label:string){
    const key=`suite_${passage.id}_${scope}_${variant}${matched?'_matched':''}`,id=`${job.id}:MIX:${key}:false`;
    const active=audition.playing===id,loading=audition.loading===id;
    return <button key={key} className={`button ${active?'primary':'secondary'}`} aria-pressed={active}
      disabled={Boolean(audition.loading)&&!loading} onClick={()=>void audition.listen(job,'MIX',key,false)}>
      {loading?<LoaderCircle size={15} className="spin"/>:active?<Square size={15}/>:<Headphones size={15}/>}
      {active?'Listening':'Listen'} · {label}</button>;
  }
  return <section className="review-track rehearsal-suite"><header className="review-track-heading">
    <span className="review-track-badge"><Headphones size={22}/></span><div><span className="eyebrow">CROSS-SESSION · NATIVE CAPTURES</span>
      <h2>{report.title}</h2></div></header><div className="study-body">
    <p className="study-lead">The same role-specific checks, applied to three recorded sessions. Native test edits are restored; healthy cases remain unchanged controls.</p>
    <div className="bank-tabs" aria-label="Suite session">{report.passages.map(p=><button key={p.id} className={selected===p.id?'active':''}
      aria-pressed={selected===p.id} onClick={()=>{audition.stop();setSelected(p.id);}}>{p.date.slice(5)} · {time(p.start_seconds)}–{time(p.start_seconds+p.duration_seconds)}{p.review?.origin==='operator'&&!p.review.include?' · excluded':''}</button>)}</div>
    <h3>{passage.date} · {passage.take_title}</h3>
    <section className="study-passage"><label className="review-match"><input type="checkbox" checked={review.include} disabled={busy}
      onChange={e=>assess(passage.id,e.target.checked,review.verdict)}/>Representative band performance</label>
      <p className="review-small">{excluded?'Excluded from evaluation.':review.include?'Operator-confirmed performance.':passage.selection_evidence?'Machine-screened source activity; no operator performance confirmation or quality verdict is implied.':'Candidate selected from simultaneous source activity; coherent performance still needs your confirmation.'}</p></section>
    <PassageEvidence evidence={passage.selection_evidence}/>
    {excluded?<section className="study-passage" role="note"><h3>Excluded from evaluation</h3><p>{review.reason}</p>
      <p className="review-small">Processing comparisons and ratings are withheld for this passage. The original recordings and archived measurements are preserved.</p></section>:<>
    <label className="review-match study-level-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>Match comparison loudness</label>
    <p className="review-small">{matched?`Full-mix copies verified at ${passage.listening.mix.target_lufs.toFixed(1)} LUFS. Solo scopes have independently verified matches.`:
      'Original relative levels with one safe listening gain per scope. Listen volume affects this computer only.'}</p>
    <section className="study-analysis suite-full-mix"><h3>Full mix · {passage.validation.identical_control?'unchanged control':'headroom candidate'}</h3>
      <p>{passage.validation.identical_control?'No edit was authorized by the current policy. Current and Candidate contain identical audio; a sonic difference is not expected.':
        'This candidate changes only the eligible RED/KIT controls listed below. Check whether the lead remains easy to follow; numerical headroom does not establish better balance.'}</p>
      <div className="review-saved-links">{player('mix','current','Full mix current')}{player('mix','candidate','Full mix candidate')}</div></section>
    {(['RED','KIT'] as const).map(name=>{const track=passage.tracks[name],scope=name.toLowerCase(),pre=track.before.metrics,post=track.after.metrics;
      const hf=track.after.analyses.sibilance_candidates,drums=track.after.analyses.transient_dynamics;
      const percent=100*pre.full_scale_samples/(track.frames*track.channels);
      return <section key={name} className="study-analysis suite-track"><h3>{name} · {name==='RED'?'lead vocal':'combined stereo drums'}</h3>
        <p><strong>{track.decision.label}</strong>. {track.decision.reason}</p>
        <div className="review-metric-scroll"><table className="review-metrics"><caption>{name} · signal checks</caption>
          <thead><tr><th>Measurement</th><th>Current</th><th>Candidate</th></tr></thead><tbody>
            <tr><th>Before-effects sample peak</th><td>{pre.sample_peak_dbfs.toFixed(2)} dBFS</td><td>Input unchanged</td></tr>
            <tr><th>After-effects sample peak</th><td>{post.sample_peak_dbfs.toFixed(2)} dBFS</td><td>{track.candidate_observation.metrics.sample_peak_dbfs.toFixed(2)} dBFS</td></tr>
            <tr><th>After-mixer sample peak</th><td>{track.current_mixer_peak_dbfs.toFixed(2)} dBFS</td><td>{track.candidate_mixer_peak_dbfs.toFixed(2)} dBFS</td></tr>
            <tr><th>Input full-scale samples</th><td>{pre.full_scale_samples.toLocaleString()} · {percent.toFixed(3)}%</td><td>Input unchanged</td></tr>
            <tr><th>Current chain crest change</th><td>{track.paired_change.crest_change_db.toFixed(2)} dB</td><td>{track.decision.kind==='keep'?'Unchanged':'No new dynamics edit'}</td></tr>
          </tbody></table></div>
        {hf&&<p>{hf.candidate_count} short high-frequency burst candidates; KIT high-frequency overlap {hf.kit_high_frequency_overlap_fraction===null?'unavailable':`${(100*hf.kit_high_frequency_overlap_fraction).toFixed(0)}%`}.
          These are not confirmed sibilance. Automatic de-essing is withheld.</p>}
        {drums&&<p>{drums.detected_energy_attacks} energy attacks; active-level spread {drums.energy_level_spread_p90_p10_db.toFixed(1)} dB.
          This does not establish a need for more compression. Stereo correlation {track.after.analyses.stereo_health?.correlation.toFixed(3)??'unavailable'}.</p>}
        {track.before.findings.map((f,i)=><p key={`input:${i}`} className="review-small"><strong>Input note:</strong> {f.note}</p>)}
        {track.notes.map((note,i)=><p key={i} className="review-small">{note}</p>)}
        <div className="review-saved-links">{player(scope,'raw',`${name} before effects`)}{player(scope,'current',`${name} current`)}{player(scope,'candidate',`${name} candidate`)}</div>
      </section>;
    })}
    <p className="study-listen-note">Gain-only changes largely disappear when solo loudness is matched. Use the full mix to judge balance, or turn matching off to inspect actual relative levels. An unchanged control should sound identical.</p>
    <section className="study-passage"><h3>Your full-mix judgment</h3><div className="review-saved-links">{(['better','same','worse','unsure'] as const).map(v=><button key={v}
      className={`button ${review.verdict===v?'primary':'secondary'}`} disabled={busy} aria-pressed={review.verdict===v}
      onClick={()=>assess(passage.id,review.include,v)}>{({better:'Better',same:'Same',worse:'Worse',unsure:'Unsure'})[v]}</button>)}</div>
      <p className="review-small">{review.reason} Judgments are saved separately from measurements and do not change Ableton.</p></section>
    </>}
    <p className="review-small">{report.scope}</p>{report.limits.map((note,i)=><p key={i} className="review-small">{note}</p>)}
    <details className="study-measurements"><summary>Capture checks and full evidence</summary>
      <p>Baseline sum residual {(100*passage.validation.baseline_sum_relative_error).toFixed(4)}%; candidate mix delta residual {(100*passage.validation.full_mix_delta_relative_error).toFixed(4)}%.</p>
      <pre>{JSON.stringify(report,null,2)}</pre></details>
  </div></section>;
}
