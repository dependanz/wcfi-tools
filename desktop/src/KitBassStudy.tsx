import {useState} from 'react';
import {Headphones,LoaderCircle,SlidersHorizontal,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Variant='current'|'bass_space'|'kit_body'|'both';
type Loudness={integrated_lufs:number;true_peak_dbtp:number};
type Overlap={kit_body_over_attack_db:number;body_source_power_percent:Record<string,number>;lowmid_source_power_percent:Record<string,number>;
  bands:Record<string,{bass_over_kit_db:number;bass_over_kit_strong_p50_db:number;bass_within_6db_of_kit_fraction:number}>};
export type KitBassReport={kind:'wcfi_kit_bass_study';title:string;duration_seconds:number;
  variants:{id:Variant;label:string;kit_cut_db:number;bass_cut_db:number}[];
  measurements:Record<Variant,{crest_db:number;loudness:Loudness}>;
  matching:{target_lufs:number;original_shared_gain_db:number;variant_gain_db:Record<Variant,number>;measured:Record<Variant,Loudness>};
  overlap:Record<Variant,Overlap>;context:{start_seconds:number;recorded_date:string;mix_scope:string;validation:{master_sum_relative_error:number}};notes:string[]};

export function KitBassStudy({report:r,job,audition}:{report:KitBassReport;job:Job;audition:Audition}){
  const [matched,setMatched]=useState(true),base=r.overlap.current;
  return <section className="review-track kit-bass-study"><header className="review-track-heading"><span className="review-track-badge"><SlidersHorizontal size={22}/></span>
    <div><span className="eyebrow">KIT / BASS · BODY VERSUS ATTACK</span><h2>{r.title}</h2></div><span className="review-source">Saved capture + local EQ</span></header>
    <div className="study-body"><p className="study-lead">{r.context.recorded_date} · {r.context.start_seconds}–{r.context.start_seconds+r.duration_seconds}s. Gentle compression stays fixed in every version: threshold −16 dB, ratio 2:1, attack 15 ms, release 100 ms. KIT also retains the −1 dB shelf at 120 Hz and +2 dB bell at 3 kHz, Q 0.7. No Ableton controls changed for this study.</p>
      <p className="review-small">{r.context.mix_scope}. All added EQ is a local preview on the same aligned source captures.</p>
      <section className="study-analysis"><h3>What the overlap measurements show</h3>
        <p>BASS is {Math.abs(base.bands['40-90'].bass_over_kit_strong_p50_db).toFixed(1)} dB below KIT in its strongest 40–90 Hz windows (median). No deep-bass ducking is included. In the 140–250 Hz body band, BASS is {base.bands['140-250'].bass_over_kit_db.toFixed(1)} dB above KIT over the whole passage, but {Math.abs(base.bands['140-250'].bass_over_kit_strong_p50_db).toFixed(1)} dB below KIT during its strongest body windows.</p>
        <p>Before the added cuts, separate source powers in the body band are KIT {base.body_source_power_percent.kit.toFixed(1)}%, BASS {base.body_source_power_percent.bass.toFixed(1)}% and RED {base.body_source_power_percent.red.toFixed(1)}%. RED also supplies {base.lowmid_source_power_percent.red.toFixed(1)}% of 250–500 Hz source power. These are energy shares, not perceived loudness or a proven masking problem. The cuts test sustained body and definition, rather than assume the bass is burying the kick.</p>
      </section>
      <label className="review-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>Match whole-mix loudness</label>
      <p className="review-small">{matched?`All four mixes are verified at ${r.matching.target_lufs.toFixed(1)} LUFS. One scalar gain per mix preserves its intended KIT/BASS balance; sources are not individually normalized. RED follows that whole-mix gain.`:
        `Original relative levels with ${r.matching.original_shared_gain_db.toFixed(1)} dB shared safety gain. RED is identical across versions.`} Listen volume controls only this computer.</p>
      <div className="reference-players">{r.variants.map(v=>{
        const key=`overlap_mix_${v.id}${matched?'_matched':''}`,id=`${job.id}:KIT:${key}:false`,active=audition.playing===id,loading=audition.loading===id;
        return <article className={`study-option ${active?'selected':''}`} key={v.id}><span className="eyebrow">GENTLE COMPRESSION FIXED</span><h3>{v.label}</h3>
          <p>{v.kit_cut_db?`KIT: ${v.kit_cut_db} dB at 180 Hz, Q 1. `:'KIT: no additional body cut. '}{v.bass_cut_db?`BASS: ${v.bass_cut_db} dB at 200 Hz, Q 0.8.`:'BASS: unchanged.'}</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading} onClick={()=>void audition.listen(job,'KIT',key,false)}>
            {loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}Listen · {v.label}</button></article>;
      })}</div>
      <p className="study-listen-note">Compare each cut against Unchanged tone first. With the BASS cut, listen for clearer drum body without hollow bass notes. With the KIT cut, check whether snare/toms lose chest or the kit becomes thin. Both cuts can overdo the change. A lower body/attack ratio is not itself a better mix; no candidate is selected or applied.</p>
      <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><caption>Mix listening levels; source-band ratios measured before audition gain</caption><thead><tr><th>Measurement</th>{r.variants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
        <tr><th>Integrated loudness</th>{r.variants.map(v=><td key={v.id}>{(matched?r.matching.measured[v.id]:r.measurements[v.id].loudness).integrated_lufs.toFixed(1)}<small>LUFS</small></td>)}</tr>
        <tr><th>True peak</th>{r.variants.map(v=><td key={v.id}>{(matched?r.matching.measured[v.id]:r.measurements[v.id].loudness).true_peak_dbtp.toFixed(1)}<small>dBTP</small></td>)}</tr>
        <tr><th>KIT body / attack change</th>{r.variants.map(v=><td key={v.id}>{(r.overlap[v.id].kit_body_over_attack_db-base.kit_body_over_attack_db).toFixed(2)}<small>dB · 140–250 Hz / 2–5 kHz</small></td>)}</tr>
        <tr><th>BASS / KIT body power</th>{r.variants.map(v=><td key={v.id}>{r.overlap[v.id].bands['140-250'].bass_over_kit_db.toFixed(2)}<small>dB · whole passage</small></td>)}</tr>
        <tr><th>BASS / KIT at strong body windows</th>{r.variants.map(v=><td key={v.id}>{r.overlap[v.id].bands['140-250'].bass_over_kit_strong_p50_db.toFixed(2)}<small>dB · median, fixed windows</small></td>)}</tr>
        <tr><th>Whole-mix listening gain</th>{r.variants.map(v=><td key={v.id}>{(matched?r.matching.variant_gain_db[v.id]:r.matching.original_shared_gain_db).toFixed(2)}<small>dB</small></td>)}</tr>
      </tbody></table></div>
      <section className="study-analysis"><h3>Comparison checks</h3><p>The saved native mix reconstructs within {(r.context.validation.master_sum_relative_error*100).toFixed(4)}% relative RMS error. Only the named KIT/BASS contributions change in each preview. Band comparisons use the same baseline-selected 10 ms windows; they do not identify individual drums or calculate perceptual masking.</p></section>
      <details className="study-measurements"><summary>Overlap, filters, matching and provenance</summary>{r.notes.map(n=><p className="review-small" key={n}>{n}</p>)}<pre>{JSON.stringify(r,null,2)}</pre></details>
    </div></section>;
}
