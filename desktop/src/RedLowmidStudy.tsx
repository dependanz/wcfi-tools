import {useState} from 'react';
import {Headphones,LoaderCircle,SlidersHorizontal,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Variant='current'|'eq'|'level';
type Loudness={integrated_lufs:number;true_peak_dbtp:number};
type Tone={lowmid_power_dbfs:number;presence_power_dbfs:number;lowmid_over_presence_db:number};
export type RedLowmidReport={kind:'wcfi_red_lowmid_study';title:string;duration_seconds:number;
  variants:{id:Variant;label:string}[];level_control_gain_db:number;red_loudness:Record<Variant,Loudness>;
  inspection:{whole:{band_power_percent:Record<string,number>;dominant_peaks_hz:number[]}};
  observed:Record<'red'|'mix',Record<Variant,Tone>>;measurements:Record<Variant,{crest_db:number;loudness:Loudness}>;
  matching:{target_lufs:number;original_shared_gain_db:number;variant_gain_db:Record<Variant,number>;measured:Record<Variant,Loudness>};
  context:{start_seconds:number;recorded_date:string;mix_scope:string;working_choice:string;
    input_health:{sample_peak_dbfs:number;full_scale_samples:number;above_full_scale_samples:number;possible_flat_top_samples:number};
    validation:{master_sum_relative_error:number}};notes:string[];sources:{title:string;url:string}[]};

export function RedLowmidStudy({report:r,job,audition}:{report:RedLowmidReport;job:Job;audition:Audition}){
  const [matched,setMatched]=useState(true),health=r.context.input_health;
  return <section className="review-track red-lowmid-study"><header className="review-track-heading"><span className="review-track-badge"><SlidersHorizontal size={22}/></span>
    <div><span className="eyebrow">RED · LOW-MID TONE / LEVEL CONTROL</span><h2>{r.title}</h2></div><span className="review-source">Saved capture + local previews</span></header>
    <div className="study-body"><p className="study-lead">{r.context.recorded_date} · {r.context.start_seconds}–{r.context.start_seconds+r.duration_seconds}s. Compare a small RED tonal cut against unchanged RED and an equally quieter level-only control. {r.context.mix_scope}. No Ableton controls changed for this study.</p>
      <section className="study-analysis"><h3>Working KIT/BASS choice</h3><p>{r.context.working_choice} KIT gentle compression and the −1/+2 EQ stay fixed in every listening file. The choice changes only the saved previews; Ableton’s chain remains untouched.</p></section>
      <section className="study-analysis"><h3>RED inspection</h3><p>RED has {r.inspection.whole.band_power_percent['250-500'].toFixed(1)}% of measured spectral power at 250–500 Hz. Its largest averaged low-mid concentration is near {r.inspection.whole.dominant_peaks_hz[0].toFixed(0)} Hz. Peaks move across two-second windows, consistent with sung fundamentals/harmonics; no fixed room resonance or excessive proximity effect has been confirmed.</p>
        <p>The original pre-effects node peaks at {health.sample_peak_dbfs.toFixed(2)} dBFS, with {health.full_scale_samples} full-scale, {health.above_full_scale_samples} above-full-scale and {health.possible_flat_top_samples} possible flat-top samples. These indicators do not establish an analogue input problem or rule out upstream distortion. The cut tests tone, not clipping repair.</p></section>
      <label className="review-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>Match whole-mix loudness</label>
      <p className="review-small">{matched?`All three mixes are verified at ${r.matching.target_lufs.toFixed(1)} LUFS. One scalar per entire mix preserves its intended balance; KIT/BASS follow that whole-mix gain.`:
        `Original relative levels with ${r.matching.original_shared_gain_db.toFixed(1)} dB shared safety gain. KIT and BASS are identical across versions.`} Listen volume controls only this computer.</p>
      <div className="reference-players balance-three">{r.variants.map(v=>{
        const key=`redlowmid_mix_${v.id}${matched?'_matched':''}`,id=`${job.id}:RED:${key}:false`,active=audition.playing===id,loading=audition.loading===id;
        return <article className={`study-option ${active?'selected':''}`} key={v.id}><span className="eyebrow">FIXED KIT / BASS BED</span><h3>{v.label}</h3>
          <p>{v.id==='current'?'RED’s recorded tone and level, with no added processing.':v.id==='eq'?'RED only: −2 dB bell at 400 Hz, Q 1. Local EQ preview.':`Unfiltered RED at ${r.level_control_gain_db.toFixed(1)} dB gain; its solo loudness matches the EQ-treated RED before mixing.`}</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading} onClick={()=>void audition.listen(job,'RED',key,false)}>
            {loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}Listen · {v.label}</button></article>;
      })}</div>
      <p className="study-listen-note">Compare the EQ version with the level-only control, then Current RED. Does the voice lose congestion while retaining chest and warmth, or does it simply become less prominent? Check that lyrics stay clear and the voice does not become hollow. The control prevents mistaking a quieter vocal for a better EQ. No RED fix has been selected or applied.</p>
      <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><caption>Whole-mix listening levels; RED tone and control metrics measured before audition gain</caption><thead><tr><th>Measurement</th>{r.variants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
        <tr><th>Mix integrated loudness</th>{r.variants.map(v=><td key={v.id}>{(matched?r.matching.measured[v.id]:r.measurements[v.id].loudness).integrated_lufs.toFixed(1)}<small>LUFS</small></td>)}</tr>
        <tr><th>Mix true peak</th>{r.variants.map(v=><td key={v.id}>{(matched?r.matching.measured[v.id]:r.measurements[v.id].loudness).true_peak_dbtp.toFixed(1)}<small>dBTP</small></td>)}</tr>
        <tr><th>RED low-mid / presence change</th>{r.variants.map(v=><td key={v.id}>{(r.observed.red[v.id].lowmid_over_presence_db-r.observed.red.current.lowmid_over_presence_db).toFixed(2)}<small>dB · 250–500 Hz / 1–4 kHz</small></td>)}</tr>
        <tr><th>Mix low-mid / presence change</th>{r.variants.map(v=><td key={v.id}>{(r.observed.mix[v.id].lowmid_over_presence_db-r.observed.mix.current.lowmid_over_presence_db).toFixed(2)}<small>dB · 250–500 Hz / 1–4 kHz</small></td>)}</tr>
        <tr><th>RED solo integrated loudness</th>{r.variants.map(v=><td key={v.id}>{r.red_loudness[v.id].integrated_lufs.toFixed(1)}<small>LUFS · before mixing</small></td>)}</tr>
        <tr><th>Whole-mix listening gain</th>{r.variants.map(v=><td key={v.id}>{(matched?r.matching.variant_gain_db[v.id]:r.matching.original_shared_gain_db).toFixed(2)}<small>dB</small></td>)}</tr>
      </tbody></table></div>
      <section className="study-analysis"><h3>Comparison checks</h3><p>KIT and BASS are identical before whole-mix matching. The saved bed reconstructs within {(r.context.validation.master_sum_relative_error*100).toFixed(4)}% relative RMS error. EQ-treated RED and the level-only control match in solo loudness before mixing; only the EQ changes RED’s normalized spectral balance. No speech-intelligibility or professional-quality score is claimed.</p></section>
      <details className="study-measurements"><summary>Inspection, EQ, control and provenance</summary>{r.notes.map(n=><p className="review-small" key={n}>{n}</p>)}{r.sources.map(s=><a className="reference-link" key={s.url} href={s.url} target="_blank" rel="noreferrer">{s.title}</a>)}<pre>{JSON.stringify(r,null,2)}</pre></details>
    </div></section>;
}
