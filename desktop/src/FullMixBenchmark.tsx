import {useState} from 'react';
import {FlaskConical,Headphones,LoaderCircle,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Evaluation={cost:number;style_distance:number;eligible:boolean;guardrail_failures:string[];
  source_balance?:{violations:string[];indeterminate_rules:string[];rules:{id:string;status:string;band:string;ceiling_db:number;median_db?:number;p90_db?:number;longest_excess_seconds?:number}[]};
  projection:{lead_over_band_db:number;kit_over_band_db:number;residual_fraction:number}};
type Row={id:string;variant:string;label:string;evaluation:Evaluation};
type BalanceCounts={new_faults:number;new_faults_caught:number;old_faults_rejected:number;benign_controls:number;false_balance_alarms:number;indeterminate_controls:number};
type CombinedCounts={faults:number;faults_rejected:number;hum_faults:number;hum_detected:number;benign_null:number;false_alarms:number};
type FullMixReportData={title:string;duration_seconds:number;
  control_gate_passed:boolean;professional_quality_judgment_validated:false;
  sessions:{id:string;source:{date:string;start_seconds:number};ranked_candidates:Row[];controls:Row[];
    best_candidate:string;automatic_recommendation:string;improvement_over_current:number;
    single_reference_winners:Record<string,string>;plan:Record<string,unknown>;
    counts?:BalanceCounts|CombinedCounts;selection_audit?:{selection_checks_passed:boolean;gap_to_next_eligible:number;
      halves:{half:number;candidate_eligible:boolean;candidate_failures:string[];cost_improvement:number;passed:boolean}[]}}[];
  checks:{session:string;test:string;passed:boolean;delta?:number}[];
  runtime:{wall_seconds:number;gpu_used:false;audio_models_run:number;audio_uploads:number};limits:string[]};
export type FullMixBenchmarkReport=FullMixReportData&({kind:'wcfi_full_mix_benchmark'}|{kind:'wcfi_source_balance_benchmark'}|{kind:'wcfi_full_mix_combined_benchmark'});

export function FullMixBenchmark({report:r,job,audition}:{report:FullMixBenchmarkReport;job:Job;audition:Audition}){
  const [session,setSession]=useState(r.sessions[0].id);
  const balance=r.kind==='wcfi_source_balance_benchmark';
  const combined=r.kind==='wcfi_full_mix_combined_benchmark';
  const s=r.sessions.find(v=>v.id===session)??r.sessions[0];
  const balanceCounts=s.counts&&'new_faults' in s.counts?s.counts:undefined;
  const combinedCounts=s.counts&&'faults' in s.counts?s.counts:undefined;
  const failed=r.checks.filter(c=>!c.passed);
  function listen(key:string,label:string){
    const identifier=`${job.id}:MIX:${key}:false`,active=audition.playing===identifier,loading=audition.loading===identifier;
    return <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active}
      disabled={Boolean(audition.loading)&&!loading} onClick={()=>void audition.listen(job,'MIX',key,false)}>
      {loading?<LoaderCircle className="spin" size={14}/>:active?<Square size={14}/>:<Headphones size={14}/>}{label}</button>;
  }
  return <section className="review-track full-mix-benchmark"><header className="review-track-heading">
    <span className="review-track-badge"><FlaskConical size={22}/></span><div><span className="eyebrow">AUTOMATIC SELECTION / BLIND BENCHMARK</span><h2>{r.title}</h2></div>
    <span className="review-source">CPU only · saved audio</span></header><div className="study-body">
    <section className="study-analysis"><h3>{r.control_gate_passed?(combined?'Combined control gate passed; choices remain experimental':balance?'Source-balance gate passed; automatic mix selection withheld':'Control gate passed; musical preference remains unvalidated'):'Automatic recommendation withheld'}</h3>
      <p>{r.checks.filter(c=>c.passed).length}/{r.checks.length} checks passed. {failed.length>0?'The selector failed challenges, so its feature ranking is not trusted to choose or apply a better mix.':'Passing these controls establishes basic behavior, not professional mixing judgment.'}</p>
      <p>{combined?'All original full-mix and source-balance controls are retained, with eight new hum/bass-note controls per date. Eight retained candidates cover balance, EQ/dynamics, placement, ambience and bus compression. Control names and answers are hidden during scoring.':balance?'Eight original candidates per date are unchanged. Sixteen fresh controls per date test different balance faults, a two-second acoustic surge, low-mid acoustic excess, and benign gain/pan changes. The old and new evaluators score the same files; candidate names and test labels are hidden.':'Eight coordinated candidates per date cover source balance, EQ/dynamics, stereo placement, room/hall returns and gentle bus compression. Ten controls per date test unchanged audio, gain invariance, buried sources, a dominant acoustic guitar, filtering, hum, clipping, excess ambience and phase faults. Names were hidden during scoring.'}</p>
      {balance&&<p>Source-balance violations now veto selection. A closer Bethel/Maverick spectral match cannot override the veto. This test does not revalidate the previous overall mix-quality gate or its hum failure.</p>}
      <p>No listening decision is required from you. These results test the tool itself. Nothing has been applied to Ableton.</p>
    </section>
    <div className="study-variants" role="group" aria-label="Benchmark session">{r.sessions.map(v=><button className={`button ${v.id===s.id?'primary':'secondary'}`} key={v.id} aria-pressed={v.id===s.id} onClick={()=>{audition.stop();setSession(v.id);}}>{v.source.date}</button>)}</div>
    <h3>{s.source.date} · {s.source.start_seconds}–{s.source.start_seconds+r.duration_seconds}s</h3>
    {balanceCounts&&<section className="study-analysis"><h3>Fresh-control results</h3><p>New balance faults detected: <strong>{balanceCounts.new_faults_caught}/{balanceCounts.new_faults}</strong>. Old evaluator rejected {balanceCounts.old_faults_rejected}/{balanceCounts.new_faults} under its original margin rule. False balance alarms: <strong>{balanceCounts.false_balance_alarms}/{balanceCounts.benign_controls}</strong> null/benign controls. Indeterminate controls: {balanceCounts.indeterminate_controls}.</p><p>These are known synthetic alterations to the existing recordings. They do not establish preference for natural or subtle mix changes.</p></section>}
    {combinedCounts&&<section className="study-analysis"><h3>Combined full-mix results</h3><p>Fault controls vetoed or ranked worse: <strong>{combinedCounts.faults_rejected}/{combinedCounts.faults}</strong>. Hum detected: <strong>{combinedCounts.hum_detected}/{combinedCounts.hum_faults}</strong>. False alarms on benign/null controls: <strong>{combinedCounts.false_alarms}/{combinedCounts.benign_null}</strong>.</p><p>Hum is checked against raw-source context. A source-explained tone may be an instrument or upstream hum; this check cannot establish which.</p></section>}
    <p>Best feature-ranked candidate: <strong>{s.ranked_candidates[0].label}</strong>. Recommendation: <strong>{s.automatic_recommendation.replaceAll('_',' ')}</strong>. Cost decrease from saved Master baseline: {s.improvement_over_current.toFixed(3)}. A lower cost measures our experimental rules; it does not establish better sound.</p>
    <p>The saved Master baseline is an earlier captured chain, not the historical broadcast or the latest October 11 seven-source local preview. Candidates here are new local renders from synchronized raw stems. No current Ableton state is inferred from these files.</p>
    <p>Single-reference choices: Bethel → {s.single_reference_winners.bethel}; Maverick → {s.single_reference_winners.maverick}. Different songs and arrangements remain confounds.</p>
    {s.selection_audit&&<section className="study-analysis"><h3>{s.automatic_recommendation==='experimental_engineering_candidate'?'Experimental engineering recommendation':'Selection withheld'}</h3><p>The choice must pass the full control gate, balance/hum checks, both references, a ranking-gap margin, and a separate comparison against the saved baseline in each six-second half. This is not a professional preference judgment. Nothing is applied.</p><p>Gap to next eligible candidate: {s.selection_audit.gap_to_next_eligible.toFixed(3)}.</p>{s.selection_audit.halves.map(h=><p key={h.half}>Half {h.half}: {h.passed?'PASS':'FAIL'} · cost improvement {h.cost_improvement.toFixed(3)} · {h.candidate_failures.join(', ')||'candidate passes configured checks'}</p>)}</section>}
    <div className="review-metric-scroll"><table className="review-metrics benchmark-ranking"><caption>Automatic ranking · all playback verified at {balance||combined?'−30':'−24'} LUFS · listen is optional</caption><thead><tr><th>Candidate</th><th>Cost ↓</th><th>Technical checks</th><th>Audition</th></tr></thead><tbody>
      {s.ranked_candidates.map(v=><tr key={v.id}><th>{v.label}</th><td>{v.evaluation.cost.toFixed(3)}</td><td>{v.evaluation.guardrail_failures.map(f=>f.replaceAll('_',' ')).join(', ')||'Passed configured checks'}</td><td>{listen(`fmb_${v.id}`,'Listen')}</td></tr>)}
    </tbody></table></div>
    {(balance||combined)&&<details className="study-measurements"><summary>Measured source-balance checks for the leading candidate</summary><div className="review-metric-scroll"><table className="review-metrics"><thead><tr><th>Check</th><th>Band</th><th>Median</th><th>Ceiling</th><th>Longest excess</th><th>Result</th></tr></thead><tbody>{s.ranked_candidates[0].evaluation.source_balance?.rules.map(v=><tr key={v.id}><th>{v.id.replaceAll('_',' ')}</th><td>{v.band}</td><td>{v.median_db?.toFixed(1)??'—'} dB</td><td>{v.ceiling_db} dB</td><td>{v.longest_excess_seconds?.toFixed(2)??'—'} s</td><td>{v.status.replaceAll('_',' ')}</td></tr>)}</tbody></table></div></details>}
    <section className="study-analysis"><h3>Reference excerpts</h3><p>Existing Bethel service and Maverick “Promises” excerpts; same twelve-second duration and listening loudness. No new downloads or uploads.</p>
      <div className="study-variants">{listen('fmb_bethel','Bethel')}{listen('fmb_maverick','Maverick City')}</div></section>
    <div className="review-metric-scroll"><table className="review-metrics"><caption>Frozen evaluation gate · failed checks are retained</caption><thead><tr><th>Session</th><th>Challenge</th><th>Result</th><th>Cost difference</th></tr></thead><tbody>
      {r.checks.filter(c=>c.session===s.id||c.session==='all').map(c=><tr key={`${c.session}:${c.test}`}><th>{c.session}</th><td>{c.test.replaceAll('_',' ')}</td><td>{c.passed?'PASS':'FAIL'}</td><td>{c.delta?.toFixed(3)??'—'}</td></tr>)}
    </tbody></table></div>
    <details className="study-measurements"><summary>Optional degraded controls and source processing</summary>
      {s.controls.map(v=><p key={v.id}>{v.label} · cost {v.evaluation.cost.toFixed(3)} · {v.evaluation.guardrail_failures.join(', ')||'no technical rejection'} {listen(`fmb_${v.id}`,'Listen')}</p>)}
      <pre>{JSON.stringify(s.plan,null,2)}</pre></details>
    <details className="study-measurements"><summary>Method, resource use and limitations</summary>
      <p>{r.runtime.wall_seconds.toFixed(1)} seconds scoring · GPU: no · learned model calls: {r.runtime.audio_models_run} · uploads: {r.runtime.audio_uploads}.</p>
      {r.limits.map(v=><p key={v}>{v}</p>)}
    </details></div></section>;
}
