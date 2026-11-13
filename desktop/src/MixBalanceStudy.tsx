import {useState} from 'react';
import {Headphones,LoaderCircle,SlidersHorizontal,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Metrics={integrated_lufs:number;true_peak_dbtp:number;low_mid_over_presence_db:number;
  low_end_over_low_mid_db:number;side_energy_percent:number};
type SourceAnalysis={original_low_mid_over_presence_db:number;eq_low_mid_over_presence_db:number;low_mid_change_db:number;presence_change_db:number};
export type BalanceReport={kind:'wcfi_mix_balance_study';title:string;duration_seconds:number;target_lufs:number;
  passage:{start_seconds:number;operator_approved?:boolean;date?:string;take_title?:string;selection_evidence?:string;
    performance_review?:{include:boolean;reason:string}};scope:string;restored:boolean;shared_original_gain_db:number;
  changes:{track:string;control:string;before:string;after:string}[];
  ranking:{track:string;signed_mix_low_mid_percent:number;low_mid_drop_if_removed_db:number}[];
  variants?:{id:string;label:string;origin:'native_capture'|'local_eq_preview';description:string}[];
  measurements:Record<string,Metrics>;matched:Record<string,Metrics>;
  guitar_analysis?:SourceAnalysis;target_track?:string;target_analysis?:SourceAnalysis;
  comparison_summary?:string;comparison_listen_note?:string;source_findings?:string[];
  foundation?:{anchor_track:string;added_track:string;starting_variant:string;applied_variant:string;explanation:string};
  balance_grid?:{default_variant:string;points:{variant:string;ac_db:number;kit_db:number}[]};
  evaluator?:{model:string;device:'cpu';cpu_threads:number;logical_cpus_allowed:number;gpu_initialized:false;
    median_inference_seconds:number;identical_control_passed:boolean;repeat_controls_passed:boolean;note:string;
    rows:{variant:string;scores:{PQ:number;CE:number;CU:number;PC:number};pq_delta_from_current:number}[]};
  validation:{sum_relative_error:number;measured_change_relative_error:number};notes:string[];audio:Record<string,string>};

export function MixBalanceStudy({report,job,audition,busy=false,assess}:{report:BalanceReport;job:Job;audition:Audition;
  busy?:boolean;assess?:(include:boolean)=>void}){
  const [matched,setMatched]=useState(true);
  const variants=report.variants??[
    {id:'before',label:'Before',origin:'native_capture',description:'Original session settings'},
    {id:'after',label:'After',origin:'native_capture',description:'Actual bridge change through the complete mix path'}];
  const preview=variants.some(v=>v.origin==='local_eq_preview');
  const grid=report.balance_grid;
  const [gridVariant,setGridVariant]=useState(grid?.default_variant??'');
  const [reference,setReference]=useState('ac_down');
  const point=grid?.points.find(p=>p.variant===gridVariant)??grid?.points[0];
  const visibleVariants=grid?variants.filter(v=>v.id===reference||v.id===point?.variant):variants;
  function choosePoint(ac:number,kit:number){const next=grid?.points.find(p=>p.ac_db===ac&&p.kit_db===kit);
    if(next){audition.stop();setGridVariant(next.variant);}}
  const target=report.target_track??'EGUITAR',analysis=report.target_analysis??report.guitar_analysis;
  return <section className="review-track mix-balance-study"><header className="review-track-heading">
    <span className="review-track-badge"><SlidersHorizontal size={22}/></span><div><span className="eyebrow">FULL MIX · {preview?'FADER / EQ COMPARISON':'BRIDGE EXPERIMENT'}</span>
      <h2>{report.title}</h2></div><span className="review-source">{preview?'Captured mix + local EQ preview':'Actual Ableton captures'}</span></header>
    <div className="study-body"><p className="study-lead">{report.foundation?'Starting mix balance':report.passage.operator_approved?'Operator-approved band passage':'Candidate full-band passage'}: {report.passage.start_seconds}–{report.passage.start_seconds+report.duration_seconds}s.
      {report.passage.take_title&&<> {report.passage.take_title}.</>}
      {report.comparison_summary??(preview?' Original and Fader −3 dB are actual Ableton captures. EQ is a local preview on captured EGUITAR, mixed back through the verified linear path. It has not been applied in Ableton.':
        ' The bridge applied the candidate during capture and restored the exact original controls afterward.')}</p>
      {report.source_findings&&<section className="study-analysis"><h3>{target} input and signal path</h3>
        {report.source_findings.map((finding,i)=><p key={i}>{finding}</p>)}</section>}
      {report.passage.selection_evidence&&!report.foundation&&<section className="study-passage"><p className="review-small">{report.passage.selection_evidence}</p>
        <label className="review-match"><input type="checkbox" checked={Boolean(report.passage.operator_approved)} disabled={busy||!assess}
          onChange={e=>assess?.(e.target.checked)}/>Representative band performance</label>
        <p className="review-small">{report.passage.performance_review?.reason??'Awaiting your listening confirmation. Concurrent source activity does not establish coherent playing or a good mix.'}</p></section>}
      {!!report.changes.length&&<div className="study-recipes">{report.changes.map(c=><div key={`${c.track}:${c.control}`}><h3>{c.track} · {c.control}</h3>
        <p>{c.before} → {c.after}. Candidate for listening; no musical preference has been established.</p></div>)}</div>}
      {report.foundation&&<section className="study-analysis" aria-label="Starting balance decision"><h3>{report.foundation.added_track} against {report.foundation.anchor_track}</h3>
        <p>{report.foundation.explanation}</p><p>No audio model or quality score was used. This starting balance is applied in Ableton; the listening buttons below select saved audio.</p></section>}
      {grid&&point&&<section className="study-analysis balance-grid-controls" aria-label="Level sweep controls"><h3>Bring the acoustic back</h3>
        <p>Choose the acoustic and KIT faders for the candidate. These selectors choose saved audio; they do not change Ableton.</p>
        <fieldset><legend>AC GUITAR fader</legend><div className="review-saved-links">{[...new Set(grid.points.map(p=>p.ac_db))].map(db=>
          <button key={db} className={`button ${point.ac_db===db?'primary':'secondary'}`} aria-pressed={point.ac_db===db}
            onClick={()=>choosePoint(db,point.kit_db)}>{db} dB</button>)}</div></fieldset>
        <fieldset><legend>KIT fader</legend><div className="review-saved-links">{[...new Set(grid.points.map(p=>p.kit_db))].map(db=>
          <button key={db} className={`button ${point.kit_db===db?'primary':'secondary'}`} aria-pressed={point.kit_db===db}
            onClick={()=>choosePoint(point.ac_db,db)}>{db>0?'+':''}{db} dB</button>)}</div></fieldset>
        <label>Reference balance <select aria-label="Reference balance" value={reference} onChange={e=>{audition.stop();setReference(e.target.value);}}>
          {variants.slice(0,3).map(v=><option key={v.id} value={v.id}>{v.label}</option>)}</select></label>
        <p className="review-small">The initial candidate has the highest Audiobox PQ among the nine acoustic-present combinations. This is a model-selected listening starting point, not an applied or validated final mix.</p>
      </section>}
      <label className="review-match study-level-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>
        Match full-mix loudness</label>
      <p className="review-small">{matched?`Loudness matching uses scalar gain only; all copies are verified at ${report.target_lufs.toFixed(1)} LUFS.`:
        `Original relative levels with ${report.shared_original_gain_db.toFixed(1)} dB of shared audition gain.`} Listen volume controls this computer only.</p>
      {report.foundation&&<p className="review-small">Turn matching off to hear the added source against a constant {report.foundation.anchor_track} level. With matching on, the entire mix receives one listening gain; the relative balance is preserved.</p>}
      <div className={`reference-players ${visibleVariants.length===3?'balance-three':''}`}>{visibleVariants.map(variant=>{
        const key=`balance_${variant.id}${matched?'_matched':''}`,id=`${job.id}:MIX:${key}:false`;
        const active=audition.playing===id,loading=audition.loading===id;
        return <article key={variant.id} className={`study-option ${active?'selected':''}`}><span className="eyebrow">{report.foundation?.starting_variant===variant.id?'START HERE · NATIVE CAPTURE':variant.origin==='local_eq_preview'?'LOCAL EQ PREVIEW':'NATIVE CAPTURE'}</span>
          <h3>{variant.label}</h3><p>{variant.description}</p>
          <button className={`button ${active?'primary':'secondary'}`} disabled={Boolean(audition.loading)&&!loading} aria-pressed={active}
            onClick={()=>void audition.listen(job,'MIX',key,false)}>{loading?<LoaderCircle size={15} className="spin"/>:active?<Square size={15}/>:<Headphones size={15}/>}
            {active?'Listening':'Listen'} · {variant.label}</button></article>;
      })}</div>
      <p className="study-listen-note">{report.comparison_listen_note??(preview?'The fader lowers the whole guitar; EQ reduces its low-mid body while preserving more of its upper frequencies. Compare vocal clarity, guitar support, warmth and harshness. Neither candidate is established as better.':
        'Does the lead remain easier to follow while guitar chords still support the song? Listen for less low-mid congestion without losing warmth or musical energy. A lower measured ratio is not automatically better.')}</p>
      <p className="review-small">{report.scope}</p>
      {report.evaluator&&<details className="study-measurements"><summary>Audiobox CPU results · experimental</summary>
        <p>{report.evaluator.note}</p>
        <div className="review-metric-scroll"><table className="review-metrics"><caption>Same-passage model predictions · loudness matched</caption>
          <thead><tr><th>Balance</th><th>Production quality</th><th>Change from current</th></tr></thead><tbody>
            {report.evaluator.rows.map(row=><tr key={row.variant}><th>{variants.find(v=>v.id===row.variant)?.label??row.variant}</th>
              <td>{row.scores.PQ.toFixed(3)}</td><td>{row.pq_delta_from_current>0?'+':''}{row.pq_delta_from_current.toFixed(3)}</td></tr>)}
          </tbody></table></div>
        <p className="review-small">CPU only · one thread · median {report.evaluator.median_inference_seconds.toFixed(2)} seconds per inference.
          Identical control: {report.evaluator.identical_control_passed?'passed':'failed'}; repeats: {report.evaluator.repeat_controls_passed?'passed':'failed'}.
          Mono 16 kHz input; stereo and frequencies above 8 kHz are unavailable to this model. Scores are not a professional-quality certification.</p>
      </details>}
      {analysis&&<section className="study-analysis"><h3>{target} measurements</h3>
        <p>200–500 Hz / 1–3 kHz: {analysis.original_low_mid_over_presence_db.toFixed(1)} → {analysis.eq_low_mid_over_presence_db.toFixed(1)} dB with the EQ candidate.</p>
        <p>The cut changes {target} low-mid energy by {analysis.low_mid_change_db.toFixed(2)} dB and presence energy by {analysis.presence_change_db.toFixed(2)} dB.
          These are signal differences, not proof that the guitar needs this cut.</p></section>}
      {!!report.ranking.length&&<><div className="review-metric-scroll"><table className="review-metrics"><caption>Track contributions in this passage · 200–500 Hz</caption>
        <thead><tr><th>Track</th><th>Signed contribution</th></tr></thead><tbody>{report.ranking.map(r=><tr key={r.track}>
          <th>{r.track}</th><td>{r.signed_mix_low_mid_percent.toFixed(1)}<small>%</small></td></tr>)}</tbody></table></div>
      <p className="review-small">Measured source/Master cross-power, including correlated bleed. Values sum to approximately 100% and can be negative with cancellation. This identifies signal contributions; it does not identify how much is unwanted.</p></>}
      <div className="review-metric-scroll"><table className="review-metrics"><caption>Original capture measurements · before listening gain</caption>
        <thead><tr><th>Measurement</th>{visibleVariants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
        {([['Integrated loudness','integrated_lufs','LUFS'],['True peak','true_peak_dbtp','dBTP'],
          ['200–500 Hz / 1–3 kHz','low_mid_over_presence_db','dB'],['20–200 Hz / 200–500 Hz','low_end_over_low_mid_db','dB'],
          ['Stereo side energy','side_energy_percent','%']] as const).map(([label,key,unit])=><tr key={key}><th>{label}</th>
            {visibleVariants.map(v=><td key={v.id}>{report.measurements[v.id][key].toFixed(1)}<small>{unit}</small></td>)}</tr>)}
        </tbody></table></div>
      {report.notes.map((note,i)=><p key={i} className="review-small">{note}</p>)}
      <details className="study-measurements"><summary>Capture checks and provenance</summary>
        <p>Track sum relative residual: {(report.validation.sum_relative_error*100).toFixed(4)}%.
          Fader capture change relative residual: {(report.validation.measured_change_relative_error*100).toFixed(4)}%.</p>
        <pre>{JSON.stringify(report,null,2)}</pre></details>
    </div></section>;
}
