"""WCFI offline Mix Objective Audit v2: descriptive, bounded source evidence.

No DAW, playback, host operation, gain automation, neural inference, or audio
export. The physical proxies are not validated listening-quality estimates.
Only the seven frozen 2026-10-04 recording sources are accepted. Failed source
fingerprints prevent all output. Never infer that a channel is a singer.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from collections import Counter, deque
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

import numpy as np
import soundfile as sf

from . import mix_objective_audit as v1

RATE = 48000
HOP = 480
SECTIONS = (
    ("before_0_20s", 0, 20),
    ("main_20_50s", 20, 50),
    ("bridge_approx_50_82s", 50, 82),
    ("later_82_120s", 82, 120),
)
BANDS = {"low_40_180": (40, 180, 80), "high_800_7000": (800, 7000, 20)}


def _stats(values):
    a = np.asarray(values, dtype=float)
    if not len(a):
        return {"status": "unavailable", "count": 0, "p10_db": None, "median_db": None, "p90_db": None}
    if not np.isfinite(a).all():
        raise ValueError("Nonfinite feature")
    return {"status": "measured", "count": int(len(a)), "p10_db": float(np.percentile(a,10)),
            "median_db": float(np.median(a)), "p90_db": float(np.percentile(a,90))}


def _segment_for_hop(hop):
    t = hop / 100.0
    return next((name for name, a, b in SECTIONS if a <= t < b), None)


def _spectrum(frame, rate, low, high):
    """RFFT band magnitudes of causal frame ending at a 10 ms hop."""
    if frame.ndim != 2 or frame.shape[1] not in (1, 2):
        raise ValueError("Bad frame shape")
    n = len(frame)
    window = np.hanning(n)
    freq = np.fft.rfftfreq(n, 1.0 / rate)
    mask = (freq >= low) & (freq < high)
    if not np.any(mask):
        raise ValueError("Frequency resolution insufficient")
    return np.sqrt(np.mean(np.abs(np.fft.rfft(frame * window[:,None], axis=0))**2,axis=1))[mask]


def kit_flux_10ms(reader, frames, *, rate=RATE):
    """Compute two independent causal spectral-flux measurements, 10 ms hops.

    Each channel uses the trailing 80 ms or 20 ms (zero future lookahead).
    Result has one entry per hop, with exactly four/one warmup windows inactive.
    Flux is normalized by the preceding spectral magnitude and remains descriptive.
    """
    if rate != RATE or frames % HOP:
        raise ValueError("KIT study requires 48kHz and complete 10ms hops")
    ring = deque(maxlen=8)
    prev = {name: None for name in BANDS}
    flux = {name: np.zeros(frames//HOP) for name in BANDS}
    energy = np.zeros(frames//HOP)
    for i in range(frames // HOP):
        block = reader.read(HOP,dtype="float32",always_2d=True)
        if len(block) != HOP or not np.isfinite(block).all():
            raise ValueError("Invalid/short KIT source")
        ring.append(block)
        energy[i] = float(np.mean(block.astype(float)**2))
        for name, (lo, hi, duration) in BANDS.items():
            k = duration // 10
            if len(ring) < k:
                continue
            window = np.concatenate(list(ring)[-k:])
            mag = _spectrum(window,rate,lo,hi)
            old = prev[name]
            if old is not None:
                flux[name][i] = float(np.sum(np.maximum(mag-old,0))) / max(float(np.sum(old)),1e-6)
            prev[name] = mag
    return flux, energy


def select_local_events(flux, energy, *, minimum_hops=8):
    """Section-specific, band-specific peak selection; never invent attacks.

    A positive local maximum must exceed the section's 75th percentile of its
    positive local maxima. Suppression is at least 80 ms, not a beat tracker.
    Low-energy events are RETAINED but flagged relative to the section floor.
    Empty/silent sections remain empty. No fixed number of attacks is imposed.
    """
    if set(flux) != set(BANDS):
        raise ValueError("Expected low and high onset features")
    n = len(energy)
    if any(len(v)!=n or not np.all(np.isfinite(v)) or np.any(v<0) for v in [energy,*flux.values()]):
        raise ValueError("Expected nonnegative finite features")
    result = {}
    for label, begin, end in SECTIONS:
        a,b = max(0,begin*100),min(n,end*100)
        section = {"time_seconds": [begin,end],"by_band":{},"union_hop_indices":[],"union_count":0}
        combined=[]
        if b <= a:
            for name in BANDS:
                section["by_band"][name]={"candidate_count":0,"energy_supported_count":0,"weak_energy_count":0,
                    "selected_hop_indices":[],"local_positive_peak_p75":None,
                    "interpretation":"candidate_transients_not_verified_drum_hits"}
            result[label] = section
            continue
        floor = max(float(np.percentile(energy[a:b],20)),1e-14)
        for name, values in flux.items():
            peak_candidates = [i for i in range(max(a+1,1),min(b-1,n-1))
                               if values[i]>1e-8 and values[i]>=values[i-1]
                               and values[i]>values[i+1]]
            if peak_candidates:
                threshold = float(np.percentile(values[peak_candidates],75)) if len(peak_candidates)>8 else float(min(values[i] for i in peak_candidates))
                selected=[]
                for idx in sorted((i for i in peak_candidates if values[i]>=threshold),
                                  key=lambda i:(-values[i],i)):
                    if all(abs(idx-j)>=minimum_hops for j in selected):
                        selected.append(idx)
                selected.sort()
            else:
                threshold = None
                selected=[]
            strong = [i for i in selected if energy[i]>=floor*2.0]
            section["by_band"][name]={"candidate_count":len(selected),
                "energy_supported_count":len(strong),"weak_energy_count":len(selected)-len(strong),
                "selected_hop_indices":selected,"local_positive_peak_p75":threshold,
                "interpretation":"candidate_transients_not_verified_drum_hits"}
            combined.extend(selected)
        section["union_hop_indices"] = sorted(set(combined))
        section["union_count"] = len(section["union_hop_indices"])
        result[label] = section
    return result


def source_saturation(reader, frames, *, rate=RATE):
    """Count observed exactly/full-scale PCM samples and contiguous runs per channel."""
    count=0
    maximum=0
    ongoing=None
    channels=reader.channels
    per_channel=[0]*channels
    chunk=rate
    remaining=frames
    near=float(1-2**-23)
    while remaining:
        x=reader.read(min(chunk,remaining),dtype="float32",always_2d=True)
        if not len(x) or not np.isfinite(x).all():
            raise ValueError("Invalid/short PCM")
        mask = np.abs(x)>=near
        count+=int(mask.sum())
        for c in range(channels):per_channel[c]+=int(mask[:,c].sum())
        # Per-frame run (at least one channel saturated), across chunk boundaries.
        frame_flags=mask.any(axis=1)
        if ongoing is None: ongoing=0
        for flag in frame_flags:
            ongoing=ongoing+1 if flag else 0
            if ongoing>maximum:maximum=ongoing
        remaining-=len(x)
    return {"full_scale_sample_count":count,"full_scale_sample_count_by_channel":per_channel,
            "max_consecutive_full_scale_frames":int(maximum),
            "full_scale_fraction_of_channel_samples":count/(frames*channels),
            "potential_input_saturation":count>0,"clipping_or_source_origin_proven":False}


def _speech_fft(block, rate=RATE):
    mono=np.mean(block,axis=1) if block.ndim==2 else block
    taper=np.hanning(len(mono))
    x=np.fft.rfft(mono*taper)
    freqs=np.fft.rfftfreq(len(mono),1/rate)
    return x[(freqs>=300)&(freqs<4500)]


def vocal_channel_evidence(readers, n_bins, *, threshold_red, calibration_noise=None):
    """Unverified correlation/energy evidence for YELLOW/GREEN; no role claims.

    Interchannel spectral similarity can arise from bleed OR aligned singers,
    and lack of similarity does not prove independent singing.
    """
    raw={t:[] for t in ("RED","YELLOW","GREEN")}
    spectra={t:[] for t in raw}
    for i in range(n_bins):
        for t in raw:
            x=readers[t].read(4800,dtype="float32",always_2d=True)
            if len(x)!=4800 or not np.isfinite(x).all():
                raise ValueError("Invalid vocal evidence input")
            raw[t].append(float(np.mean(x.astype(float)**2)))
            spectra[t].append(_speech_fft(x))
    red=np.asarray(raw["RED"])
    red_db=v1._db_power(red)
    red_active=red_db>=threshold_red
    out={}
    for t in ("YELLOW","GREEN"):
        p=np.asarray(raw[t]);db=v1._db_power(p)
        # A within-recording relative gate; does not infer noise or speech.
        floor=float(np.percentile(db,20))
        spread=float(np.percentile(db,80))-floor
        local_gate=max(-80.0,floor+(6.0 if spread>=6.0 else -3.0))
        active=db>=local_gate
        coherence=[]
        for k in range(n_bins):
            if not active[k] or not red_active[k]:continue
            a,b=spectra[t][k],spectra["RED"][k]
            denom=float(np.vdot(a,a).real*np.vdot(b,b).real)
            if denom>1e-16:
                coherence.append(float(np.clip(abs(np.vdot(a,b))**2/denom,0,1)))
        overlap=int(np.count_nonzero(active&red_active))
        solo=int(np.count_nonzero(active&~red_active))
        out[t]={"status":"unverified_source_role", "activity_threshold_raw_dbfs":local_gate,
                "energy_active_bins":int(active.sum()),"active_while_red_inactive_bins":solo,
                "active_together_with_red_bins":overlap,
                "speechband_spectral_similarity_median":float(np.median(coherence)) if coherence else None,
                "speechband_spectral_similarity_count":len(coherence),
                "possible_bleed_or_shared_performance":True,
                "independent_singer_verified":False,
                "safe_to_auto_boost":False,
                "interpretation":"energy and spectral similarity cannot identify singing, bleed, or speaker identity"}
    return out


def capture_section_attack_margins(readers, events, variants, *, evaluation_start, rate=RATE):
    """Report onset-local power ratios; separately for low and upper bands.

    Only verified exact sources are used. No audio is saved. Measurements use
    end-aligned 80ms or 20ms source windows, never future samples.
    """
    result={}
    for section,entry in events.items():
        result[section]={}
        for band, (lo,hi,duration) in BANDS.items():
            hops=entry["by_band"][band]["selected_hop_indices"]
            ratios={name:[] for name in variants}
            frames=duration*rate//1000
            taper=np.hanning(frames)
            freqs=np.fft.rfftfreq(frames,1/rate)
            mask=(freqs>=lo)&(freqs<hi)
            for hop in hops:
                end=hop*HOP+HOP
                powers=[]
                for t in v1.TRACKS:
                    r=readers[t]
                    r.seek(evaluation_start + max(0,end-frames))
                    z=r.read(frames,dtype="float32",always_2d=True)
                    if len(z)!=frames or not np.isfinite(z).all():
                        raise ValueError("Short onset-aligned source")
                    X=np.fft.rfft(z*taper[:,None],axis=0)
                    p=float(np.mean(np.sum(abs(X[mask])**2,axis=0)))
                    powers.append(p)
                power=np.asarray(powers)
                for name,gain in variants.items():
                    weighted=power*10**(gain/10)
                    num=weighted[v1.INDEX["KIT"]]
                    den=np.sum(weighted)-num
                    ratios[name].append(float(v1._dB_ratio(num,den)))
            result[section][band]={k:_stats(v) for k,v in ratios.items()}
    return result


def pareto_descriptive(metrics,attack,peaks):
    """Nondominance of heterogeneous descriptive objectives; never auto select.

    Maximize lead lower-tail, onset lower-tail and full-mix headroom, minimize
    absolute gain change from frozen static. A value with missing evidence is
    NOT comparable and is classified as unresolved, not optimistically filled.
    """
    base=metrics["frozen_static"]["gains_db"]
    scores={}
    for name,x in metrics.items():
        lead=x["global"]["lead_vs_all_stems_speech_band_db"]["p10_db"]
        attacks=[attack[s]["high_800_7000"][name]["p10_db"] for s,_,_ in SECTIONS]
        # If any section lacks an attack measurement, abstain from global ranking.
        attack_min=min(attacks) if all(v is not None for v in attacks) else None
        tp=peaks.get(name)
        drift=sum(abs(x["gains_db"][t]-base[t]) for t in v1.TRACKS)
        scores[name]={"lead_lower_tail_margin_db":lead,"worst_section_high_attack_p10_db":attack_min,
            "true_peak_dbtp":tp,"gain_change_l1_db":drift,
            "comparable":all(v is not None for v in (lead,attack_min,tp))}
    available=[k for k,v in scores.items() if v["comparable"]]
    vectors={k:np.array([v["lead_lower_tail_margin_db"],v["worst_section_high_attack_p10_db"],
                         -v["true_peak_dbtp"],-v["gain_change_l1_db"]])
             for k,v in scores.items() if k in available}
    for k,x in scores.items():
        dominators=[j for j in available if j!=k and np.all(vectors[j]>=vectors[k]-1e-8)
                    and np.any(vectors[j]>vectors[k]+1e-8)] if k in vectors else []
        x["pareto_status"]=("insufficient_evidence" if k not in vectors else
                            "nondominated" if not dominators else "dominated")
        x["dominated_by"]=dominators
        x["automatic_gain_decision_allowed"]=False
    return scores


def _scan_ffmpeg_pipe(readers, variants, *, ffmpeg, frames, evaluation_start):
    """Compute EBU R128 LUFS/true peak by streaming PCM through local FFmpeg.

    No WAV files are created; audio goes only to ffmpeg's stdin and null sink.
    Stderr is a temporary log for parsing and is discarded immediately.
    """
    if ffmpeg is None:
        return {k:{"status":"unavailable", "reason":"ffmpeg_not_supplied",
                   "integrated_lufs":None,"true_peak_dbtp":None} for k in variants}
    exe=Path(ffmpeg).resolve(strict=True)
    results={}
    for name,gain in variants.items():
        for t in v1.TRACKS:readers[t].seek(evaluation_start)
        cmd=[str(exe),"-nostdin","-hide_banner","-nostats","-threads","1",
             "-filter_threads","1","-f","f32le","-ar",str(RATE),"-ac","2", "-i","pipe:0",
             "-af","ebur128=peak=true", "-f","null","-"]
        with tempfile.TemporaryFile(mode="w+t",encoding="utf-8") as log:
            p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log)
            try:
                remaining=frames
                mult=(10**(np.asarray(gain)/20))[None,:,None]
                while remaining:
                    n=min(RATE,remaining)
                    blocks=[]
                    for t in v1.TRACKS:
                        x=readers[t].read(n,dtype="float32",always_2d=True)
                        if len(x)!=n or not np.isfinite(x).all():
                            raise ValueError("Short PCM during true-peak scan")
                        blocks.append(np.repeat(x,2,axis=1) if x.shape[1]==1 else x)
                    mix=np.sum(np.stack(blocks,axis=1).astype(np.float64)*mult,axis=1)
                    if not np.isfinite(mix).all():raise ValueError("Nonfinite reconstructed mix")
                    p.stdin.write(np.asarray(mix,dtype="<f4").tobytes())
                    remaining-=n
            except Exception:
                p.kill();p.wait()
                raise
            finally:
                if p.stdin and not p.stdin.closed:p.stdin.close()
            code=p.wait(timeout=120)
            log.seek(0)
            message=log.read()
            if code:
                raise ValueError(f"ffmpeg R128 scan failed for {name} (exit={code})")
            # Extract the terminal EBU R128 summary, not per-frame stats.
            chunks=message.split("Summary:")
            if len(chunks)<2:raise ValueError("ffmpeg ebur128 summary missing")
            final=chunks[-1]
            loud=re.search(r"Integrated loudness:\s*I:\s*([-+\d.]+)\s*LUFS",final)
            true=re.search(r"True peak:\s*Peak:\s*([-+\d.]+)\s*dBFS",final)
            if not loud or not true:raise ValueError("ffmpeg integrated loudness/true peak missing")
            l,tp=float(loud[1]),float(true[1])
            if not math.isfinite(l) or not math.isfinite(tp):
                raise ValueError("ffmpeg returned nonfinite loudness/true peak")
            results[name]={"status":"measured", "integrated_lufs":l,
                           "true_peak_dbtp":tp, "provider":"ffmpeg_ebur128_peak_true_no_audio_file"}
    return results


def run(report_path, output, *, audit_path=None, ffmpeg=None):
    """Fail-closed reproducible audit, no mix recommendation or host writes."""
    report_path=Path(report_path).resolve(strict=True)
    output=Path(output)
    if output.exists():raise ValueError("Output already exists")
    report=json.loads(report_path.read_text(encoding="utf-8"))
    if audit_path is not None:
        audit_path=Path(audit_path).resolve(strict=True)
        if hashlib.sha256(audit_path.read_bytes()).hexdigest()!=report["protocol"]["source_audit_sha256"]:
            raise ValueError("Source audit hash mismatch")
        audit=json.loads(audit_path.read_text(encoding="utf-8"))
        if {x["track"] for x in audit["sources"]}!=set(v1.TRACKS) or len(audit["sources"])!=7:
            raise ValueError("Unexpected source audit tracks")
        for item in audit["sources"]:
            if Path(item["source_path"]).resolve()!=Path(report["source_provenance"][item["track"]]["path"]).resolve():
                raise ValueError("Audit source path does not match exact saved source")
    verified=v1._check_source_files(report)
    rate,_,_,end,sources=v1._source_windows(report)
    eval_start=report["protocol"]["evaluation_source_frames"][0]
    length=end-eval_start
    settings=report["protocol"]["coordinator_settings"]
    variants=v1._variants(settings["baseline_gains_db"])
    with ExitStack() as stack:
        readers={t:stack.enter_context(sf.SoundFile(sources[t]["path"],"r")) for t in v1.TRACKS}
        for reader in readers.values():reader.seek(eval_start)
        features,powers,source_peak,mix_peak,mix_rms = v1._capture_features(readers,rate,length,variants)
        results,selection=v1.evaluate_objectives(features,powers,variants,settings["activity_thresholds_dbfs"],
             mix_peak=mix_peak,mix_power_sum=mix_rms,frames_per_bin=4800)
        readers["KIT"].seek(eval_start)
        flux,energy=kit_flux_10ms(readers["KIT"],length)
        events=select_local_events(flux,energy)
        for reader in readers.values():reader.seek(eval_start)
        vocal=vocal_channel_evidence(readers,len(features),
             threshold_red=settings["activity_thresholds_dbfs"]["RED"]-settings["baseline_gains_db"]["RED"])
        for reader in readers.values():reader.seek(eval_start)
        attacks=capture_section_attack_margins(readers,events,variants,evaluation_start=eval_start)
        for reader in readers.values():reader.seek(eval_start)
        clipping={t:source_saturation(readers[t],length) for t in v1.TRACKS}
        for reader in readers.values():reader.seek(eval_start)
        ffmpeg_results=_scan_ffmpeg_pipe(readers,variants,ffmpeg=ffmpeg,frames=length,evaluation_start=eval_start)
    # Source identity and metadata checks before committing output.
    for t,meta in sources.items():
        st=Path(meta["path"]).stat()
        if st.st_size!=meta["size"] or st.st_mtime_ns!=meta["mtime_ns"]:
            raise ValueError("A source changed during audit")
    peaks={k:item["true_peak_dbtp"] for k,item in ffmpeg_results.items()}
    frontier=pareto_descriptive(results,attacks,peaks)
    summary={"protocol":{"kind":"seven_stem_offline_physical_proxy_audit_v2",
              "previous_version":"v1_separate_unchanged","source_date":"2026-10-04",
              "source_report_sha256":hashlib.sha256(report_path.read_bytes()).hexdigest(),
              "analysis_seconds":length/rate,"onset_hop_ms":10,"onset_band_window_ms":{k:v[2] for k,v in BANDS.items()},
              "sections":SECTIONS,"no_fault_injection":True,"audio_output_files":0,
              "host_writes":0,"live_eligible":False,"human_listening_quality_established":False,
              "automatic_gain_decision_made":False},
             "verified_sources":verified,"saturation":clipping,
             "vocal_source_evidence":vocal,"transient_candidates":events,
             "section_attack_band_margins":attacks,
             "static_probe_metrics":results,
             "r128":ffmpeg_results,"descriptive_pareto":frontier,
             "limitations":["Not auditory masking or speech intelligibility, nor verified musical attacks.",
                "Spectral similarity alone cannot identify singer identity or microphone bleed.",
                "Only five preselected static probes; Pareto nondominance is not an optimal mix.",
                "Section labels are approximate; synthetic thresholds cannot infer composition intent.",
                "Previously examined development recording, not an independent holdout.",
                "No host processing, Ableton effects, live eligibility or audible preference is established."]}
    output.mkdir(parents=True,exist_ok=False)
    (output/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False),encoding="utf-8")
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report",required=True,type=Path)
    p.add_argument("--audit",type=Path)
    p.add_argument("--ffmpeg",type=Path)
    p.add_argument("--output",required=True,type=Path)
    args=p.parse_args()
    try:
        x=run(args.report,args.output,audit_path=args.audit,ffmpeg=args.ffmpeg)
        print(json.dumps({"output":str(args.output.resolve()),"protocol":x["protocol"]["kind"],
            "verified_sources":len(x["verified_sources"]),
            "transients":{k:{"low":v["by_band"]["low_40_180"]["candidate_count"],
                              "high":v["by_band"]["high_800_7000"]["candidate_count"]}
                          for k,v in x["transient_candidates"].items()},
            "saturation_flags":{k:v["potential_input_saturation"] for k,v in x["saturation"].items()},
            "backing_roles":{k:v["status"] for k,v in x["vocal_source_evidence"].items()},
            "true_peak":{"status":{k:v["status"] for k,v in x["r128"].items()}},
            "nondominated_probes":[k for k,v in x["descriptive_pareto"].items() if v["pareto_status"]=="nondominated"],
            "auto_gain_changes":False},indent=2,allow_nan=False))
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError) as e:
        p.exit(1,f"Offline v2 audit stopped: {e}\n")

if __name__ == "__main__":main()
