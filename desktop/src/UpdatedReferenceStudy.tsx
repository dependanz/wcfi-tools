import {ArrowLeftRight,Headphones,LoaderCircle,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Variant='before'|'updated'|'bethel'|'maverick';
type Metrics={side_energy_percent:number;lr_correlation:number;mono_energy_change_db:number;crest_db:number;
  low_mid_over_presence_db:number;sub_over_presence_db:number;high_over_presence_db:number;band_energy_percent:Record<string,number>};
export type UpdatedReferenceReport={kind:'wcfi_updated_reference_study';title:string;duration_seconds:number;
  variants:{id:Variant;label:string;scope:string}[];metrics:Record<Variant,Metrics>;
  sources:Record<Variant,{start_seconds?:number;duration_seconds:number;metadata?:{label:string;start_seconds:number;url:string;context:string}}>;
  context:{start_seconds:number;recorded_date:string;wcf_scope:string;red_choice_reason:string};
  matching:{target_lufs:number;measured:Record<Variant,{integrated_lufs:number;true_peak_dbtp:number}>};
  findings:{id:string;title:string;screened_gap?:boolean;evidence:string;interpretation?:string;next_test:string}[];notes:string[]};

export type FullBandReport=Omit<UpdatedReferenceReport,'kind'>&{kind:'wcfi_full_band_study';
  plan:{track:string;role:string;fader_db:number;pan:number}[];excluded:Record<string,string>;
  source_stats:Record<string,{loudness:{integrated_lufs:number|null};peak_dbfs:number;full_scale_percent:number}>};

export function UpdatedReferenceStudy({report:r,job,audition}:{report:UpdatedReferenceReport|FullBandReport;job:Job;audition:Audition}){
  const full=r.kind==='wcfi_full_band_study';
  return <section className={`review-track updated-reference-study ${full?'full-band-study':''}`}><header className="review-track-heading"><span className="review-track-badge"><ArrowLeftRight size={22}/></span>
    <div><span className="eyebrow">{full?'FULL BAND / WORKING BALANCE':'UPDATED WCFI FOUNDATION / REFERENCE CHECK'}</span><h2>{r.title}</h2></div><span className="review-source">Loudness matched · saved audio</span></header>
    <div className="study-body"><p className="study-lead">WCFI: {r.context.recorded_date}, {r.context.start_seconds}–{r.context.start_seconds+r.duration_seconds}s. All four excerpts are {r.duration_seconds} seconds and verified at {r.matching.target_lufs.toFixed(1)} LUFS. Existing Bethel and Maverick excerpts are reused; no new downloads, model listening or Ableton controls were used.</p>
      <section className="study-analysis"><h3>{full?'RED/KIT fixes held; band restored in previews':'RED choice and what this comparison includes'}</h3><p>Keep the RED −2 dB bell at 400 Hz, Q 1 as a working treatment. {r.context.red_choice_reason} KIT retains gentle compression, the −1/+2 EQ and the 180 Hz body cut. BASS is unchanged.</p>
        <p>{r.context.wcf_scope}. The references are complete mixes with different songs, singers and production. This comparison cannot tell us what further KIT or RED EQ would reproduce their sound.</p></section>
      {r.kind==='wcfi_full_band_study'&&<><section className="study-analysis"><h3>Band balance and placement</h3>
        <p>The full-band file adds electric guitar, acoustic guitar, stereo keys and GREEN to the selected RED/KIT/BASS foundation. Preview fader equivalents below include a fixed Master at −6 dB. They have not been applied to Ableton.</p>
        <div className="review-metric-scroll"><table className="review-metrics"><caption>Engineering starting balance; raw-source LUFS uses the full 20-second recording</caption><thead><tr><th>Source / role</th><th>Raw level</th><th>Preview fader</th><th>Placement</th></tr></thead><tbody>
          {r.plan.map(p=><tr key={p.track}><th>{p.track}<small>{p.role}</small></th><td>{r.source_stats[p.track].loudness.integrated_lufs?.toFixed(1)??'Below measurement gate'}<small>LUFS</small></td><td>{p.fader_db>0?'+':''}{p.fader_db}<small>dB</small></td><td>{p.pan===0?'Recorded stereo':`${Math.round(Math.abs(p.pan)*100)}% ${p.pan<0?'left':'right'}`}</td></tr>)}
        </tbody></table></div><p>Acoustic guitar has {r.source_stats['AC GUITAR'].full_scale_percent.toFixed(2)}% full-scale samples in the raw recording. Its low preview level prevents dominance; recorded clipping is not repaired. GREEN remains conservative because its own performance versus bleed is unconfirmed. KEYS retains its recorded stereo; the other added sources use a local equal-power placement law.</p>
        <details><summary>Excluded and unavailable sources</summary>{Object.entries(r.excluded).map(([name,reason])=><p key={name}><strong>{name}: </strong>{reason}</p>)}</details>
      </section></>}
      <div className="reference-players">{r.variants.map(v=>{
        const key=`updatedref_${v.id}_matched`,id=`${job.id}:MIX:${key}:false`,active=audition.playing===id,loading=audition.loading===id,s=r.sources[v.id],m=r.matching.measured[v.id],ref=v.id==='bethel'||v.id==='maverick';
        return <article className={`study-option ${active?'selected':''}`} key={v.id}><span className="eyebrow">{ref?'COMPLETE REFERENCE MIX':full&&v.id==='updated'?'SEVEN-SOURCE DRY FULL BAND':'RED / KIT / BASS FOUNDATION'}</span><h3>{v.label}</h3>
          <p>{v.id==='before'?(full?'Selected RED/KIT/BASS foundation before the remaining band is added.':'The same selected KIT/BASS bed, before RED’s 400 Hz cut.'):v.id==='updated'?(full?'Full-band preview with RED/KIT fixes held and BASS unchanged. No return effects are added.':'Selected KIT processing plus RED’s 400 Hz cut; BASS unchanged.'):s.metadata?.label}
            {ref&&<><br/>{s.metadata?.start_seconds}–{(s.metadata?.start_seconds??0)+r.duration_seconds}s · {v.id==='bethel'?'Archived service broadcast':'Released live performance; production/mastering unknown'}</>}</p>
          <p className="reference-matched">Verified: {m.integrated_lufs.toFixed(1)} LUFS · {m.true_peak_dbtp.toFixed(1)} dBTP</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading} onClick={()=>void audition.listen(job,'MIX',key,false)}>
            {loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}Listen · {v.label}</button>
          {ref&&s.metadata&&<a className="reference-link" href={s.metadata.url} target="_blank" rel="noreferrer">Original {v.label} source ↗</a>}</article>;
      })}</div>
      <p className="study-listen-note">Compare updated WCFI with each reference at the same comfortable volume. Check the size and placement of the band, then lead clarity and kick/bass definition. These are different performances, not synchronized stems. Stereo width and spectral ratios describe differences; they do not grade professional quality. Listen volume affects only this computer.</p>
      <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><caption>Same-duration descriptors; listening loudness is matched, other ratios are invariant to scalar gain</caption><thead><tr><th>Measurement</th>{r.variants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
        <tr><th>Matched loudness</th>{r.variants.map(v=><td key={v.id}>{r.matching.measured[v.id].integrated_lufs.toFixed(1)}<small>LUFS</small></td>)}</tr>
        <tr><th>Stereo side energy</th>{r.variants.map(v=><td key={v.id}>{r.metrics[v.id].side_energy_percent.toFixed(2)}<small>%</small></td>)}</tr>
        <tr><th>L/R correlation</th>{r.variants.map(v=><td key={v.id}>{r.metrics[v.id].lr_correlation.toFixed(3)}</td>)}</tr>
        <tr><th>Mono fold-down energy change</th>{r.variants.map(v=><td key={v.id}>{r.metrics[v.id].mono_energy_change_db.toFixed(2)}<small>dB</small></td>)}</tr>
        <tr><th>Low mids / presence</th>{r.variants.map(v=><td key={v.id}>{r.metrics[v.id].low_mid_over_presence_db.toFixed(2)}<small>dB · 200–500 Hz / 1–3 kHz</small></td>)}</tr>
        <tr><th>Highs / presence</th>{r.variants.map(v=><td key={v.id}>{r.metrics[v.id].high_over_presence_db.toFixed(2)}<small>dB · 6–12 kHz / 1–3 kHz</small></td>)}</tr>
        <tr><th>Peak / RMS contrast</th>{r.variants.map(v=><td key={v.id}>{r.metrics[v.id].crest_db.toFixed(2)}<small>dB</small></td>)}</tr>
      </tbody></table></div>
      <section className="reference-findings"><h3>Biggest remaining checks</h3>{r.findings.map((f,i)=><article className="reference-finding" key={f.id}><span className="eyebrow">{i+1} · MEASURED DIFFERENCE / WORKING HYPOTHESIS</span><h4>{f.title}</h4><p>{f.evidence}</p>{f.interpretation&&<p>{f.interpretation}</p>}<p><strong>Next test: </strong>{f.next_test}</p></article>)}</section>
      <details className="study-measurements"><summary>Methods, sources and limits</summary>{r.notes.map(n=><p className="review-small" key={n}>{n}</p>)}<pre>{JSON.stringify(r,null,2)}</pre></details>
    </div></section>;
}
