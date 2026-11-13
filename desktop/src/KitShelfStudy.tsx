import {useState} from 'react';
import {Headphones,LoaderCircle,SlidersHorizontal,Square} from 'lucide-react';
import type {Audition,Job} from './RehearsalPage';

type Variant='current'|'shelf1'|'shelf2'|'presence1'|'presence2'|'s1p1'|'s1p2'|'s2p1'|'s2p2';
type Loudness={integrated_lufs:number;true_peak_dbtp:number};
type Stats={sample_peak_dbfs:number;rms_dbfs:number;crest_db:number;loudness:Loudness};
type Matching={target_lufs:number;original_balance_safety_gain_db:number;kit_contribution_matched?:boolean;
  kit_reference_lufs?:number;common_audition_gain_db?:number;measured_matched_loudness:Record<Variant,Loudness>};
type EQReport={title:string;duration_seconds:number;audio:Record<string,string>;
  variants:{id:Variant;label:string;gain_db?:number;shelf_db?:number;presence_db?:number;origin:'native_capture'|'local_eq_preview'}[];
  measurements:Record<'kit'|'mix',Record<Variant,Stats>>;matching:Record<'kit'|'mix',Matching>;
  changes:Record<Variant,{weight_over_attack_change_db:number;attack_over_weight_change_db?:number;crest_change_db:number}>;
  context:{recorded_date:string;start_seconds:number;mix_scope:string;faders:Record<string,string>;
    validation:{master_sum_relative_error:number}};notes:string[]};
export type ShelfReport=(EQReport&{kind:'wcfi_kit_shelf_study'})|(EQReport&{kind:'wcfi_kit_presence_study'})|(EQReport&{kind:'wcfi_kit_combined_study'});

export function KitShelfStudy({report:r,job,audition}:{report:ShelfReport;job:Job;audition:Audition}){
  const [scope,setScope]=useState<'kit'|'mix'>('mix'),[matched,setMatched]=useState(true);
  const matching=r.matching[scope],stats=r.measurements[scope],presence=r.kind==='wcfi_kit_presence_study',combined=r.kind==='wcfi_kit_combined_study';
  return <section className={`review-track kit-shelf-study ${presence?'kit-presence-study':''} ${combined?'kit-combined-study':''}`}><header className="review-track-heading"><span className="review-track-badge"><SlidersHorizontal size={22}/></span>
    <div><span className="eyebrow">{combined?'KIT · SHELF + PRESENCE':presence?'KIT · 3 KHZ PRESENCE BELL':'KIT · 120 HZ LOW SHELF'}</span><h2>{r.title}</h2></div><span className="review-source">Local EQ previews</span></header>
    <div className="study-body"><p className="study-lead">{r.context.recorded_date} · {r.context.start_seconds}–{r.context.start_seconds+r.duration_seconds}s. {combined?
      'Compare the unchanged chain with all four combinations: a −1/−2 dB low shelf at 120 Hz, followed by a +1/+2 dB bell at 3 kHz, Q 0.7. Both follow the existing compressor.':presence?
      'Compare the unchanged chain with separate +1 and +2 dB broad bells at 3 kHz, Q 0.7, after the existing compressor. No shelf cuts are included.':
      'Compare the unchanged chain with −1 and −2 dB shelves at 120 Hz, after the existing compressor.'} No Ableton effects, faders or transport controls changed.</p>
      <div className="bank-tabs" aria-label="EQ comparison scope">{(['mix','kit'] as const).map(s=><button key={s} className={scope===s?'active':''} onClick={()=>{audition.stop();setScope(s);}}>{s==='mix'?'In the mix':'KIT solo'}</button>)}</div>
      <p className="review-small">{scope==='mix'?`${r.context.mix_scope}. Master fader gain is included. ${Object.entries(r.context.faders).map(([name,db])=>`${name} ${db}`).join(' · ')}.`:
        'KIT captured after the current compressor, before its track fader. The EQ versions use the same aligned source waveform.'}</p>
      <label className="review-match"><input type="checkbox" checked={matched} onChange={e=>{audition.stop();setMatched(e.target.checked);}}/>Match KIT loudness</label>
      <p className="review-small">{matched?(scope==='mix'?
        `KIT is matched to the current chain (${matching.kit_reference_lufs?.toFixed(1)} LUFS before the common audition gain). RED and BASS stay fixed. Overall mix LUFS may vary slightly.`:
        `All ${r.variants.length===5?'five':'three'} solo files are verified at ${matching.target_lufs.toFixed(1)} LUFS, using scalar gain only.`):
        `Original relative levels with ${matching.original_balance_safety_gain_db.toFixed(1)} dB shared safety gain. The EQ retains its level change.`} Listen volume controls only this computer.</p>
      <div className={`reference-players ${combined?'balance-combined':'balance-three'}`}>{r.variants.map(v=>{
        const key=`study_${scope}_${v.id}${matched?'_matched':''}`,id=`${job.id}:KIT:${key}:false`,active=audition.playing===id,loading=audition.loading===id;
        return <article className={`study-option ${active?'selected':''}`} key={v.id}><span className="eyebrow">{v.origin==='native_capture'?'ACTUAL CAPTURE':'LOCAL EQ PREVIEW'}</span><h3>{v.label}</h3>
          <p>{v.id==='current'?'Existing compressor; no added EQ.':combined?`120 Hz shelf ${v.shelf_db} dB · 3 kHz bell +${v.presence_db} dB · Q 0.7.`:presence?`3 kHz bell · +${v.gain_db} dB at center · Q 0.7 · no shelf cut.`:`120 Hz low shelf · ${v.gain_db} dB low-frequency gain · slope 1.`}</p>
          <button className={`button ${active?'primary':'secondary'}`} aria-pressed={active} disabled={Boolean(audition.loading)&&!loading}
            onClick={()=>void audition.listen(job,'KIT',key,false)}>{loading?<LoaderCircle className="spin" size={15}/>:active?<Square size={15}/>:<Headphones size={15}/>}
            Listen · {v.label}</button></article>;
      })}</div>
      <p className="study-listen-note">{combined?
        'Start in the mix with matching on. Compare attack definition, kick weight and whether RED stays clear. Use solo to check for thinness or brittle cymbals/snare. Compare each candidate back to Current chain; a stronger contrast is not automatically better. No combination has been selected or applied.':presence?
        'Start in the mix with matching on. Check whether snare and kick attacks become easier to follow while RED remains clear. In solo, check whether cymbals, snare or drum bleed become brittle or tiring. These boosts are separate from the shelf experiment; neither has been selected or applied.':
        'Start in the mix with matching on. Check whether kick attacks become easier to follow while bass pitch and RED stay clear. In solo, check whether the kick loses useful weight or the snare becomes thin. The shelf versions are prepared for comparison; neither has been selected or applied.'}</p>
      <div className="review-metric-scroll diagnostic-table"><table className="review-metrics"><caption>{matched?'Listening-copy loudness and peaks; crest and band ratios are unaffected by scalar listening gain':'Measurements before shared safety listening gain'}</caption>
        <thead><tr><th>Measurement</th>{r.variants.map(v=><th key={v.id}>{v.label}</th>)}</tr></thead><tbody>
          <tr><th>Integrated loudness</th>{r.variants.map(v=><td key={v.id}>{(matched?matching.measured_matched_loudness[v.id]:stats[v.id].loudness).integrated_lufs.toFixed(1)}<small>LUFS</small></td>)}</tr>
          <tr><th>True peak</th>{r.variants.map(v=><td key={v.id}>{(matched?matching.measured_matched_loudness[v.id]:stats[v.id].loudness).true_peak_dbtp.toFixed(1)}<small>dBTP</small></td>)}</tr>
          <tr><th>Crest factor</th>{r.variants.map(v=><td key={v.id}>{stats[v.id].crest_db.toFixed(2)}<small>dB</small></td>)}</tr>
          <tr><th>{presence||combined?'KIT 2–5 kHz / 30–100 Hz change':'KIT 30–100 Hz / 2–5 kHz change'}</th>{r.variants.map(v=><td key={v.id}>{(r.changes[v.id].weight_over_attack_change_db*(presence||combined?-1:1)).toFixed(2)}<small>dB</small></td>)}</tr>
        </tbody></table></div>
      <section className="study-analysis"><h3>Comparison checks</h3><p>Same aligned source passage in all versions. KIT loudness matching is verified on saved audio. Other sources remain fixed across the mix versions.</p>
        <p>The captured mix reconstructs from its sources within {(r.context.validation.master_sum_relative_error*100).toFixed(4)}% relative RMS error. This permits the local KIT substitution; it does not grade musical quality.</p></section>
      <details className="study-measurements"><summary>EQ method, matching and provenance</summary>{r.notes.map(note=><p className="review-small" key={note}>{note}</p>)}<pre>{JSON.stringify(r,null,2)}</pre></details>
    </div></section>;
}
