import {useState} from 'react';
import {Headphones,LoaderCircle,SlidersHorizontal,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Variant='current'|'gentle'|'light';
type Loudness={integrated_lufs:number;true_peak_dbtp:number};
type Stats={crest_db:number;loudness:Loudness};
type Matching={target_lufs:number;original_balance_safety_gain_db:number;kit_reference_lufs?:number;
  measured_matched_loudness:Record<Variant,Loudness>};
export type CompressionReport={kind:'wcfi_kit_compression_study';title:string;duration_seconds:number;
  variants:{id:Variant;label:string;threshold_db:number}[];measurements:Record<'kit'|'mix',Record<Variant,Stats>>;
  matching:Record<'kit'|'mix',Matching>;gain_reduction:Record<Variant,{strong_p50_p90_db:number[];active_1ms_max_db:number;gain_model_relative_rms_error:number}>;
  context:{recorded_date:string;start_seconds:number;mix_scope:string;compressor:{ratio:number;attack_ms:number;release_ms:number;output_gain_db:number};
    validation:{master_sum_relative_errors:Record<Variant,number>}};notes:string[]};

export function KitCompressionStudy({report:r,job,audition}:{report:CompressionReport;job:Job;audition:Audition}){
  const [scope,setScope]=useState<'kit'|'mix'>('mix'),[matched,setMatched]=useState(true);
  const matching=r.matching[scope],stats=r.measurements[scope],c=r.context.compressor;
  return <section className="review-track kit-compression-study"><header className="review-track-heading"><span className="review-track-badge"><SlidersHorizontal size={22}/></span>
    <div><span className="eyebrow">KIT · LIGHT COMPRESSION / FIXED EQ</span><h2>{r.title}</h2></div><span className="review-source">Native compression + local EQ</span></header>
    <div className="study-body"><p className="study-lead">{r.context.recorded_date} · {r.context.start_seconds}–{r.context.start_seconds+r.duration_seconds}s.
      Compare current, gentle and light native compression. Every version has the same −1 dB shelf at 120 Hz and +2 dB bell at 3 kHz, Q 0.7, after compression.
      The original Ableton threshold was restored after both captures. Play and Record stayed stopped.</p>
      <p className="review-small">Ratio {c.ratio}:1 · attack {c.attack_ms} ms · release {c.release_ms} ms · Output Gain {c.output_gain_db.toFixed(2)} dB, fixed in every trial. The EQ is a local preview.</p>
      <div className="bank-tabs" aria-label="Compression comparison scope">{(['mix','kit'] as const).map(s=><button key={s} className={scope===s?'active':''} onClick={()=>{audition.stop();setScope(s);}}>{s==='mix'?'In the mix':'KIT solo'}</button>)}</div>
      <p className="review-small">{scope==='mix'?`${r.context.mix_scope}. Master fader gain is included.`:'KIT after the native compressor and identical local EQ, before its track fader.'}</p>
      <label className="review-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>Match KIT loudness</label>
      <p className="review-small">{matched?(scope==='mix'?`KIT is matched to the current version (${matching.kit_reference_lufs?.toFixed(1)} LUFS before the common audition gain). RED and BASS stay fixed. Overall mix LUFS may vary slightly.`:
        `All three solo files are verified at ${matching.target_lufs.toFixed(1)} LUFS, using scalar gain only.`):
        `Original relative levels with ${matching.original_balance_safety_gain_db.toFixed(1)} dB shared safety gain. Compression retains its level change.`} Listen volume controls only this computer.</p>
      <div className="reference-players balance-three">{r.variants.map(v=>{
        const key=`study_${scope}_${v.id}${matched?'_matched':''}`,id=`${job.id}:KIT:${key}:false`,active=audition.playing===id,loading=audition.loading===id,g=r.gain_reduction[v.id];
        return <article className={`study-option ${active?'selected':''}`} key={v.id}><span className="eyebrow">NATIVE CAPTURE + FIXED EQ</span><h3>{v.label}</h3>
          <p>Threshold {v.threshold_db} dB · estimated stronger-window reduction {g.strong_p50_p90_db[1].toFixed(1)} dB (P90); brief maximum {g.active_1ms_max_db.toFixed(1)} dB.</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading} onClick={()=>void audition.listen(job,'KIT',key,false)}>
            {loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}Listen · {v.label}</button></article>;
      })}</div>
      <p className="study-listen-note">Start in the mix with matching on. Check whether stronger hits sit more consistently without losing kick/snare punch. In solo, check for softened attacks, cymbal swells and pumping. Light compression reaches the requested 1–3 dB range; that alone does not establish a better sound. Neither trial is left applied.</p>
      <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><caption>Listening levels and peaks; gain reduction measured before EQ</caption><thead><tr><th>Measurement</th>{r.variants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
        <tr><th>Integrated loudness</th>{r.variants.map(v=><td key={v.id}>{(matched?matching.measured_matched_loudness[v.id]:stats[v.id].loudness).integrated_lufs.toFixed(1)}<small>LUFS</small></td>)}</tr>
        <tr><th>True peak</th>{r.variants.map(v=><td key={v.id}>{(matched?matching.measured_matched_loudness[v.id]:stats[v.id].loudness).true_peak_dbtp.toFixed(1)}<small>dBTP</small></td>)}</tr>
        <tr><th>Crest factor after fixed EQ</th>{r.variants.map(v=><td key={v.id}>{stats[v.id].crest_db.toFixed(2)}<small>dB</small></td>)}</tr>
        <tr><th>Stronger-window reduction · P50 / P90</th>{r.variants.map(v=><td key={v.id}>{r.gain_reduction[v.id].strong_p50_p90_db.map(n=>n.toFixed(2)).join(' / ')}<small>dB, estimated</small></td>)}</tr>
        <tr><th>Maximum 1 ms reduction</th>{r.variants.map(v=><td key={v.id}>{r.gain_reduction[v.id].active_1ms_max_db.toFixed(2)}<small>dB, estimated</small></td>)}</tr>
      </tbody></table></div>
      <section className="study-analysis"><h3>Comparison checks</h3><p>Same raw loop in every trial; only the native compressor Threshold changed. Captured RED and BASS waveforms stayed unchanged. Each native Master capture reconstructs within {(Math.max(...Object.values(r.context.validation.master_sum_relative_errors))*100).toFixed(4)}% relative RMS error, permitting the local EQ substitution.</p>
        <p>Gain reduction comes from aligned input/output samples with known Output Gain removed, not the plug-in meter. Strong windows are the loudest 10% of active raw 10 ms windows; these are not individually identified drum hits. The brief maximum uses 1 ms windows.</p></section>
      <details className="study-measurements"><summary>Compression, EQ, matching and provenance</summary>{r.notes.map(n=><p className="review-small" key={n}>{n}</p>)}<pre>{JSON.stringify(r,null,2)}</pre></details>
    </div></section>;
}
