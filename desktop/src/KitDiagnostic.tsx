import {useState} from 'react';
import {Activity,Headphones,LoaderCircle,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Id='raw'|'processed'|'foundation'|'bethel'|'maverick';
type Spectrum={band_energy_percent:Record<string,number>;shape:{hz:number;relative_db:number}[];
  local_psd_peaks:{hz:number;prominence_db:number}[];low_mid_over_attack_db:number};
type Measurement={duration_seconds:number;sample_peak_dbfs:number;rms_dbfs:number;crest_db:number;
  full_scale_samples:number;above_full_scale_samples:number;possible_flat_top_samples:number;
  level_spread_p90_p10_db:number|null;side_energy_percent:number;mono_energy_change_db:number;spectrum:Spectrum};
type Events={isolated_windows:number;selected_candidates:number;early_over_tail_median_db:number|null;
  early_band_energy_median_percent:Record<string,number|null>};
export type DiagnosticReport={kind:'wcfi_kit_diagnostic';title:string;audio:Record<string,string>;target_lufs:number;
  measurements:Record<Id,Measurement>;loudness:Record<Id,{integrated_lufs:number;true_peak_dbtp:number}>;
  matched:Record<Id,{integrated_lufs:number;true_peak_dbtp:number}>;
  pair:{best_fit_scalar_gain_db:number;scalar_residual_relative_rms:number;crest_change_db:number;
    frame_gain_p10_p50_p90_db:number[];band_shape_change_db:Record<string,number>;
    raw_events:Record<string,Events>;processed_events:Record<string,Events>;event_method:string};
  context:{recorded_date:string;start_seconds:number;duration_seconds:number;faders:Record<string,string>;
    chain:{name:string;parameters:{name:string;display:string}[]}[];
    references:Record<string,{label:string;url:string;context:string}>};
  findings:{id:string;title:string;evidence:string;scope:string}[];next_tests:{title:string;proposal:string}[];
  literature:{title:string;url:string}[];limitations:string[];method:string};

const labels:Record<Id,string>={raw:'Raw KIT',processed:'Current processed KIT',foundation:'WCFI · RED / KIT / BASS',bethel:'Bethel · full mix',maverick:'Maverick · full mix'};
const colors:Record<Id,string>={raw:'#285e80',processed:'#bd7a26',foundation:'#285e80',bethel:'#38836d',maverick:'#916caa'};
const bands=[['subsonic','Below 30 Hz'],['weight','30–100 Hz'],['body','100–250 Hz'],['low_mid','250–500 Hz'],
  ['mid','500 Hz–1.5 kHz'],['upper_mid','1.5–2 kHz'],['attack','2–5 kHz'],['cymbal','5–12 kHz'],['air','12–20 kHz']];

function SpectrumPlot({report,ids,title}:{report:DiagnosticReport;ids:Id[];title:string}){
  const [hidden,setHidden]=useState<Id[]>([]),left=56,right=762,top=22,bottom=210;
  const x=(hz:number)=>left+(Math.log2(hz)-Math.log2(20))/(Math.log2(20000)-Math.log2(20))*(right-left);
  const y=(db:number)=>bottom-(Math.max(-60,Math.min(0,db))+60)/60*(bottom-top);
  return <figure className="diagnostic-plot"><figcaption>{title}</figcaption>
    <div className="diagnostic-legend">{ids.map(id=><label key={id} style={{color:colors[id]}}><input type="checkbox" checked={!hidden.includes(id)}
      onChange={()=>setHidden(old=>old.includes(id)?old.filter(v=>v!==id):[...old,id])}/>{labels[id]}</label>)}</div>
    <svg viewBox="0 0 800 255" role="img" aria-label={title}>
      {[-60,-40,-20,0].map(db=><g key={db}><line x1={left} y1={y(db)} x2={right} y2={y(db)} stroke="#dce6ed"/><text x={left-9} y={y(db)+4} textAnchor="end">{db}</text></g>)}
      {[30,100,250,1000,4000,16000].map(hz=><g key={hz}><line x1={x(hz)} y1={top} x2={x(hz)} y2={bottom} stroke="#edf1f5"/>
        <text x={x(hz)} y={bottom+22} textAnchor="middle">{hz>=1000?`${hz/1000}k`:hz}</text></g>)}
      {ids.filter(id=>!hidden.includes(id)).map(id=><polyline key={id} fill="none" stroke={colors[id]} strokeWidth="2.5"
        strokeDasharray={id==='processed'?'6 4':undefined} points={report.measurements[id].spectrum.shape.map(p=>`${x(p.hz)},${y(p.relative_db)}`).join(' ')}>
        <title>{labels[id]}</title></polyline>)}
      <text x="410" y="253" textAnchor="middle">Frequency (Hz) · 1/3-octave band power relative to total below 20 kHz (dB)</text>
    </svg><p className="review-small">Each signal is normalized to its own total spectral power. The curves describe tonal distribution, not recommended EQ gain. Values below −60 dB are clipped in the plot.</p>
  </figure>;
}

export function KitDiagnostic({report:r,job,audition}:{report:DiagnosticReport;job:Job;audition:Audition}){
  const [context,setContext]=useState(false),ids:Id[]=context?['foundation','bethel','maverick']:['raw','processed'];
  const compressor=r.context.chain.find(d=>d.name==='Compressor');
  function player(id:Id){const key=`diagnostic_${id}_matched`,identifier=`${job.id}:KIT:${key}:false`;
    const active=audition.playing===identifier,loading=audition.loading===identifier;
    return <article className={`study-option ${active?'selected':''}`} key={id}><span className="eyebrow">{id==='raw'||id==='processed'?'PAIRED KIT CAPTURE':'FULL-MIX CONTEXT'}</span>
      <h3>{labels[id]}</h3><p>{r.measurements[id].duration_seconds.toFixed(0)} seconds · matched to {r.target_lufs.toFixed(1)} LUFS.</p>
      <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading}
        onClick={()=>void audition.listen(job,'KIT',key,false)}>{loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}
        Listen · {labels[id]}</button>
      {(id==='bethel'||id==='maverick')&&<a className="reference-link" href={r.context.references[id].url} target="_blank" rel="noreferrer">Reference source</a>}
    </article>;
  }
  return <section className="review-track kit-diagnostic"><header className="review-track-heading"><span className="review-track-badge"><Activity size={22}/></span>
    <div><span className="eyebrow">KIT · SPECTRUM / ATTACK / DYNAMICS</span><h2>{r.title}</h2></div><span className="review-source">Read-only diagnostic</span></header>
    <div className="study-body"><p className="study-lead">{r.context.recorded_date} · {r.context.start_seconds}–{r.context.start_seconds+r.context.duration_seconds}s. Fresh pre/post node captures; no effects, faders or transport controls changed.</p>
      <section className="study-analysis"><h3>Current processing</h3><p>{compressor?compressor.parameters.filter(p=>['Threshold','Ratio','Attack','Release','Output Gain','Dry/Wet'].includes(p.name)).map(p=>`${p.name}: ${p.display}`).join(' · '):'See the saved chain snapshot below.'}</p>
        <p>Best-fit gain: {r.pair.best_fit_scalar_gain_db.toFixed(3)} dB · remaining waveform difference: {(r.pair.scalar_residual_relative_rms*100).toFixed(4)}% RMS · crest change: {r.pair.crest_change_db.toFixed(3)} dB.</p></section>
      <div className="bank-tabs" aria-label="Diagnostic scope"><button className={!context?'active':''} onClick={()=>{audition.stop();setContext(false);}}>Raw / processed KIT</button>
        <button className={context?'active':''} onClick={()=>{audition.stop();setContext(true);}}>Bethel / Maverick context</button></div>
      {context&&<section className="reference-selection"><strong>These references contain the whole band.</strong><p>They are not isolated drum buses. WCFI here contains RED, KIT and BASS only: {Object.entries(r.context.faders).map(([name,db])=>`${name} ${db}`).join(' · ')}. Differences cannot be assigned to drum EQ or compression.</p>
        {(['bethel','maverick'] as const).map(id=><p key={id}><strong>{r.context.references[id].label}.</strong> {r.context.references[id].context}</p>)}</section>}
      <div className="reference-players">{ids.map(player)}</div>
      <p className="review-small">All listening copies use scalar gain only, verified after writing at {r.target_lufs.toFixed(1)} LUFS and below −3 dBTP. Raw and processed KIT use the same aligned passage. Opening this report starts no audio.</p>
      <SpectrumPlot key={context?'context':'kit'} report={r} ids={ids} title={context?'Full-mix spectral context':'Raw and processed KIT · normalized spectral shape'}/>
      <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><caption>{context?'Whole-mix measurements; no drum attribution':'Signal measurements before listening gain'}</caption><thead><tr><th>Measurement</th>{ids.map(id=><th key={id}>{labels[id]}</th>)}</tr></thead>
        <tbody>{([['Sample peak','sample_peak_dbfs','dBFS'],['RMS','rms_dbfs','dBFS'],['Crest factor','crest_db','dB'],['10 ms level spread, P90–P10','level_spread_p90_p10_db','dB'],['Side energy','side_energy_percent','%'],['Mono energy change','mono_energy_change_db','dB']] as const).map(([label,key,unit])=><tr key={key}><th>{label}</th>{ids.map(id=><td key={id}>{r.measurements[id][key]?.toFixed(2)??'—'}<small>{unit}</small></td>)}</tr>)}
          {bands.map(([key,label])=><tr key={key}><th>{label} power</th>{ids.map(id=><td key={id}>{r.measurements[id].spectrum.band_energy_percent[key].toFixed(2)}<small>%</small></td>)}</tr>)}
        </tbody></table></div>
      {!context&&<section className="diagnostic-events"><h3>Attack and tail · same raw-selected events</h3><p className="review-small">{r.pair.event_method}</p>
        <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><thead><tr><th>Energy band</th><th>Isolated windows</th><th>Raw early / tail</th><th>Processed early / tail</th></tr></thead><tbody>
          {Object.entries(r.pair.raw_events).map(([key,event])=><tr key={key}><th>{key==='low_band'?'40–150 Hz':'150 Hz–5 kHz'}</th><td>{event.isolated_windows}</td><td>{event.early_over_tail_median_db?.toFixed(2)??'—'} dB</td><td>{r.pair.processed_events[key].early_over_tail_median_db?.toFixed(2)??'—'} dB</td></tr>)}
        </tbody></table></div></section>}
      <section className="reference-findings"><h3>Findings</h3>{r.findings.map(f=><article className="reference-finding" key={f.id}><span className="eyebrow">{f.scope==='full_mix_context_only'?'REFERENCE CONTEXT':'KIT MEASUREMENT'}</span><h4>{f.title}</h4><p>{f.evidence}</p></article>)}</section>
      <section className="study-analysis"><h3>Proposed next tests · nothing applied</h3>{r.next_tests.map(t=><div key={t.title}><h4>{t.title}</h4><p>{t.proposal}</p></div>)}</section>
      <details className="study-measurements"><summary>Method, spectral peaks and provenance</summary><p>{r.method}</p>
        <p>Raw local spectral peaks: {r.measurements.raw.spectrum.local_psd_peaks.map(p=>`${Math.round(p.hz)} Hz`).join(', ')}. These are not confirmed unwanted resonances.</p>
        {r.limitations.map(t=><p key={t}>{t}</p>)}<p>Literature: {r.literature.map((s,i)=><span key={s.url}>{i>0?' · ':''}<a href={s.url} target="_blank" rel="noreferrer">{s.title}</a></span>)}</p>
        <pre>{JSON.stringify(r,null,2)}</pre></details>
    </div></section>;
}
