import {useEffect,useState} from 'react';
import {ArrowLeftRight,Headphones,LoaderCircle,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Metrics={integrated_lufs:number;true_peak_dbtp:number;side_energy_percent:number;crest_db:number;
  low_mid_over_presence_db:number;sub_over_presence_db:number;high_over_presence_db:number;
  low_end_over_low_mid_db:number;
  band_energy_percent:Record<string,number>};
type Source={label:string;start_seconds:number;url?:string;context:string;metrics:Metrics;
  matched:{integrated_lufs:number;true_peak_dbtp:number};matching_gain_db:number};
export type ReferenceReport={kind:'wcfi_reference_study';title:string;target_lufs:number;method:string;
  reference_name?:string;reference_key?:'reference';reference_kind?:'service_broadcast'|'released_live_performance';
  pairs:{id:string;label:string;duration_seconds:number;wcfi:Source;bethel?:Source;reference?:Source}[];
  findings:{id:string;title:string;evidence_type:string;evidence:string;next_test:string}[];
  approved_passage_count?:number;
  passage_review?:{id:string;label:string;start_seconds:number;duration_seconds:number;include:boolean;reason:string;origin:string;classification?:string}[];
  limitations:string[];source_audit:unknown[];session_context:{master_fader_display:string};audio:Record<string,string>};

export function ReferenceStudy({report,job,audition,busy,assess}:{report:ReferenceReport;job:Job;audition:Audition;busy:boolean;assess:(id:string,include:boolean)=>void}){
  const [selected,setSelected]=useState(report.pairs.find(p=>p.id==='loud')?.id??report.pairs[0]?.id??'');
  const signature=report.pairs.map(p=>p.id).join('|');
  useEffect(()=>{audition.stop();if(!report.pairs.some(p=>p.id===selected))setSelected(report.pairs[0]?.id??'');},[signature]);
  const pair=report.pairs.find(p=>p.id===selected)??report.pairs[0];
  const referenceKey=report.reference_key??'bethel',referenceName=report.reference_name??'Bethel';
  const sources=['wcfi',referenceKey] as const;
  const released=report.reference_kind==='released_live_performance';
  const review=<>{report.passage_review&&<details className="reference-selection"><summary>Performance selection · {report.pairs.length} approved</summary>
    <p>Only representative band performances support mix-quality comparisons. Jams remain available as technical KIT studies.</p>
    {report.passage_review.map(p=><label key={p.id}><input type="checkbox" checked={p.include} disabled={busy}
      onChange={e=>assess(p.id,e.target.checked)}/><span><strong>{p.start_seconds}–{p.start_seconds+p.duration_seconds}s · {p.include?'Approved performance':p.classification==='jam'?'Jam — excluded':'Excluded / unconfirmed'}</strong><small>{p.reason}</small></span></label>)}</details>}</>;
  if(!pair)return <section className="review-track reference-study"><div className="study-body"><h2>Confirm a performance passage</h2>
    <p>No passage is approved for musical reference comparison.</p>{review}</div></section>;
  return <section className="review-track reference-study"><header className="review-track-heading">
    <span className="review-track-badge"><ArrowLeftRight size={22}/></span><div><span className="eyebrow">FULL MIX · REFERENCE</span>
      <h2>{report.title}</h2></div><span className="review-source">Loudness matched · local listening</span></header>
    <div className="study-body"><p className="study-lead">Compare the complete WCFI mix with {released?`a released live performance by ${referenceName}`:`an archived ${referenceName} service`}.
      Both listening copies are verified at {report.target_lufs.toFixed(1)} LUFS. Different performances and arrangements remain part of what you hear.</p>
      {released&&<p className="study-listen-note">Released live performance: editing, overdubs and mastering are unknown. This provides another sound reference; it does not establish what an everyday service broadcast should achieve.</p>}
      {report.approved_passage_count===1&&<p className="study-listen-note">One operator-approved WCFI performance passage. Jams are excluded from reference findings. These differences have not been repeated across representative WCFI performances.</p>}
      {review}
      <div className="review-saved-links" aria-label="Full-mix passage">{report.pairs.map(p=><button key={p.id}
        className={`button ${p.id===selected?'primary':'secondary'}`} aria-pressed={p.id===selected}
        onClick={()=>{audition.stop();setSelected(p.id);}}>{p.label}</button>)}</div>
      <div className="reference-players">{sources.map(source=>{
        const s=pair[source]!,variant=`reference_${pair.id}_${source}_matched`,id=`${job.id}:MIX:${variant}:false`;
        const active=audition.playing===id,loading=audition.loading===id;
        return <article key={source} className={`study-option ${active?'selected':''}`}><span className="eyebrow">{source==='wcfi'?'YOUR CAPTURED MIX':released?'RELEASED LIVE PERFORMANCE':'PUBLIC SERVICE REFERENCE'}</span>
          <h3>{source==='wcfi'?'WCFI':referenceName}</h3><p>{s.label}<br/>{s.start_seconds}–{s.start_seconds+pair.duration_seconds}s · {pair.duration_seconds} seconds</p>
          <p className="reference-matched">Verified: {s.matched.integrated_lufs.toFixed(1)} LUFS · {s.matched.true_peak_dbtp.toFixed(1)} dBTP</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading}
            onClick={()=>void audition.listen(job,'MIX',variant,false)}>{loading?<LoaderCircle size={15} className="spin"/>:active?<Square size={15}/>:<Headphones size={15}/>}
            {active?'Listening':'Listen'} · {source==='wcfi'?'WCFI':referenceName}</button>
          {s.url&&<a className="reference-link" href={s.url} target="_blank" rel="noopener noreferrer">Original {referenceName} {released?'performance':'service'} ↗</a>}
        </article>;
      })}</div>
      <p className="study-listen-note">Start with lead-word clarity, kick/bass definition, then depth and space. Switch at the same comfortable listening volume.
        These are different songs, so corresponding seconds are not synchronized performances. No EQ, limiter or widening was added to either listening copy.</p>
      <div className="review-metric-scroll"><table className="review-metrics"><caption>Original excerpt measurements, before listening gain</caption>
        <thead><tr><th>Measurement</th><th>WCFI</th><th>{referenceName}</th></tr></thead><tbody>
        {([['Integrated loudness','integrated_lufs','LUFS'],['True peak','true_peak_dbtp','dBTP'],
          ['Stereo side energy','side_energy_percent','%'],['200–500 Hz / 1–3 kHz','low_mid_over_presence_db','dB'],
          ['20–200 Hz / 200–500 Hz','low_end_over_low_mid_db','dB'],
          ['20–80 Hz / 1–3 kHz','sub_over_presence_db','dB'],['6–12 kHz / 1–3 kHz','high_over_presence_db','dB'],
          ['Peak / RMS contrast','crest_db','dB']] as const).map(([label,key,unit])=><tr key={key}><th>{label}</th>
            {sources.map(source=><td key={source}>{pair[source]!.metrics[key].toFixed(1)}<small>{unit}</small></td>)}</tr>)}
        </tbody></table></div>
      <div className="review-metric-scroll"><table className="review-metrics"><caption>Frequency distribution · share of measured 20 Hz–20 kHz power</caption>
        <thead><tr><th>Band</th><th>WCFI</th><th>{referenceName}</th></tr></thead><tbody>
        {([['sub','20–80 Hz'],['bass','80–200 Hz'],['low_mid','200–500 Hz'],['mid','500 Hz–1 kHz'],
          ['presence','1–3 kHz'],['upper_mid','3–6 kHz'],['high','6–12 kHz'],['air','12–20 kHz']] as const).map(([key,label])=>
          <tr key={key}><th>{label}</th>{sources.map(s=><td key={s}>{pair[s]!.metrics.band_energy_percent[key].toFixed(1)}<small>%</small></td>)}</tr>)}
        </tbody></table></div>
      <p className="review-small">WCFI is the captured Master node before its {report.session_context.master_fader_display} fader, not the ATEM or encoded stream.
        The reference’s original level and width are descriptors, not targets to copy.</p>
      <section className="reference-findings"><h3>Biggest checks to make next</h3><p className="review-small">{report.pairs.length===1?'Measured faults and differences in this one approved passage; repeatability is untested.':'Ordered from measured input faults through reference differences.'} Suggested experiments have not been established as audible fixes.</p>
        {report.findings.map((finding,index)=><article className="reference-finding" key={finding.id}>
          <span className="eyebrow">{index+1} · {finding.evidence_type==='source_measurement'?'SOURCE MEASUREMENT':finding.evidence_type==='listening_required'?'LISTENING NEEDED':'MEASURED DIFFERENCE · HYPOTHESIS'}</span>
          <h4>{finding.title}</h4><p>{finding.evidence}</p><p><strong>Next test: </strong>{finding.next_test}</p></article>)}</section>
      <details className="study-measurements"><summary>Sources, methods and limits</summary><p>{report.method}</p>
        <p>{pair.wcfi.context}</p><p>{pair[referenceKey]!.context}</p>
        {report.limitations.map((note,i)=><p key={i}>{note}</p>)}
        <pre>{JSON.stringify({pairs:report.pairs,source_audit:report.source_audit,session_context:report.session_context},null,2)}</pre></details>
    </div></section>;
}
