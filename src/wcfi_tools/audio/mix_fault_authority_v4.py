"""WCFI offline v4: stress-test static candidates against bounded KIT fault authority.

Frozen seven-stem development recording only. No DAW, host writes, PCM file
exports, audio playback, live gain decisions, or claims of musical improvement.
Injected KIT faults are deliberately artificial. The clairvoyant controller
knows fault boundaries, unlike a real online system, and tests only a specified
uniform-cut policy, not a universal impossibility certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import soundfile as sf

from . import mix_candidate_explorer_v3 as v3
from . import mix_objective_audit as v1

RATE = 48000
BIN = RATE // 10
FAULT_DB = 10.0
CEILING = -3.0
GUARDBAND = 0.5
MAX_CUT = 3.0
SLEW_DB_PER_SECOND = 0.5
FAULT_START = 20.0
FAULT_END = 50.0
SCENARIOS = (
    "uncontrolled_10db_step",
    "clairvoyant_slew_limited_maxcut",
    "instant_maxcut_noncausal_upper_authority",
    "isolated_100ms_hit_uncontrolled",
)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda:f.read(1<<20),b""):
            h.update(block)
    return h.hexdigest()


def controller_correction(t, scenario):
    """Static hypothetical controls, never a real or trainable controller."""
    if scenario == "clairvoyant_slew_limited_maxcut":
        if t <= FAULT_START:
            return 0.
        if t < FAULT_END:
            return -min(MAX_CUT, SLEW_DB_PER_SECOND*(t-FAULT_START))
        return -max(0., MAX_CUT-SLEW_DB_PER_SECOND*(t-FAULT_END))
    if scenario == "instant_maxcut_noncausal_upper_authority":
        return -MAX_CUT if FAULT_START <= t < FAULT_END else 0.
    if scenario in ("uncontrolled_10db_step", "isolated_100ms_hit_uncontrolled"):
        return 0.
    raise ValueError("Unsupported experimental control scenario")


def injection_gain(t, scenario):
    if scenario == "isolated_100ms_hit_uncontrolled":
        return FAULT_DB if FAULT_START <= t < FAULT_START+0.1 else 0.
    if scenario in SCENARIOS:
        return FAULT_DB if FAULT_START <= t < FAULT_END else 0.
    raise ValueError("Unknown KIT fault scenario")


def validate_controller_contract():
    grid = np.arange(0,1200*BIN+1, BIN, dtype=np.float64)/RATE
    cuts = np.array([controller_correction(float(t),"clairvoyant_slew_limited_maxcut") for t in grid])
    if (cuts.max() > 1e-9 or cuts.min() < -MAX_CUT-1e-9
        or np.max(np.abs(np.diff(cuts))) > SLEW_DB_PER_SECOND*.1+1e-9):
        raise AssertionError("Hypothetical uniform correction exceeds its authority/slew")
    return True


def verify_prior(v3_report, trial, audit_bytes, verified):
    proto = v3_report.get("protocol",{})
    if proto.get("kind") != "seven_stem_offline_gain_candidate_explorer_v3":
        raise ValueError("Expected a v3 candidate explorer report")
    if (proto.get("host_writes") != 0 or proto.get("no_audio_output") is not True or
        proto.get("automatic_gain_decision_made") is not False):
        raise ValueError("v3 report must be descriptive and offline")
    if proto.get("source_audit_sha256") != hashlib.sha256(audit_bytes).hexdigest():
        raise ValueError("v3 source audit mismatch")
    if set(v3_report.get("verified_sources",{})) != set(v1.TRACKS):
        raise ValueError("v3 verified source set mismatch")
    for t in v1.TRACKS:
        if v3_report["verified_sources"][t]["verified_sha256"] != verified[t]["verified_sha256"]:
            raise ValueError("v3 decoded source SHA mismatch: "+t)
    base=trial["protocol"]["coordinator_settings"]["baseline_gains_db"]
    expected=v3.build_variants(base)
    actual=v3_report.get("candidates",{})
    if set(actual)!=set(expected):
        raise ValueError("v3 candidate grid differs")
    for name, g in expected.items():
        prior=actual[name]
        if not np.allclose(g,np.asarray([prior["gains_db"][t] for t in v1.TRACKS]),atol=1e-9,rtol=0):
            raise ValueError("v3 candidate gain mismatch")
        if prior.get("automatically_eligible") is not False:
            raise ValueError("v3 cannot authorize live gain")
    frontier=v3_report.get("raw_safe_pareto_frontier")
    if (not isinstance(frontier,list) or not frontier or len(frontier)!=len(set(frontier))
        or any(n not in actual or actual[n].get("raw_safe") is not True or
               actual[n].get("raw_safe_pareto_status") != "nondominated" for n in frontier)):
        raise ValueError("Invalid v3 Pareto frontier")
    return frontier, expected


def mix_block(blocks, gains, scenario, bin_index):
    """Recompose seven stems; sample-accurate hypothetical KIT correction."""
    if scenario not in SCENARIOS:raise ValueError("Unknown scenario")
    if len(blocks)!=len(v1.TRACKS):
        raise ValueError("Need exactly seven stems")
    n=len(blocks[0]);
    if n!=BIN:raise ValueError("Exactly 100ms blocks required")
    gain=np.asarray(gains,dtype=np.float64)
    if gain.shape!=(len(v1.TRACKS),) or not np.isfinite(gain).all():
        raise ValueError("Invalid candidate gain shape")
    t=(np.arange(n,dtype=np.float64)+bin_index*BIN)/RATE
    r=np.zeros((n,2),dtype=np.float64)
    kit_idx=v1.INDEX["KIT"]
    faulty = (t>=FAULT_START)&(t<(FAULT_START+0.1 if scenario=="isolated_100ms_hit_uncontrolled" else FAULT_END))
    injected = np.where(faulty,FAULT_DB,0.)
    if scenario=="clairvoyant_slew_limited_maxcut":
        control=np.where(t<=FAULT_START,0., np.where(t<FAULT_END,
            -np.minimum(MAX_CUT,SLEW_DB_PER_SECOND*(t-FAULT_START)),
            -np.maximum(0.,MAX_CUT-SLEW_DB_PER_SECOND*(t-FAULT_END))))
    elif scenario=="instant_maxcut_noncausal_upper_authority":
        control=np.where((t>=FAULT_START)&(t<FAULT_END),-MAX_CUT,0.)
    else:
        control=np.zeros_like(t)
    for i,raw in enumerate(blocks):
        x=np.asarray(raw,dtype=np.float64)
        if x.shape not in ((n,1),(n,2)) or not np.isfinite(x).all():
            raise ValueError("Invalid source PCM")
        if x.shape[1]==1:x=np.repeat(x,2,axis=1)
        if i==kit_idx:
            mult=np.power(10.0,(gain[i]+injected+control)/20)
            r+=x*mult[:,None]
        else:
            r+=x*(10**(gain[i]/20))
    if not np.isfinite(r).all():raise ValueError("Nonfinite dry sum")
    return r


def parse_r128_summary(log):
    parts=log.split("Summary:")
    if len(parts)<2:raise ValueError("FFmpeg EBU R128 final summary missing")
    final=parts[-1]
    l=re.search(r"Integrated loudness:\s*I:\s*([-+\d.]+)\s*LUFS",final)
    p=re.search(r"True peak:\s*Peak:\s*([-+\d.]+)\s*dBFS",final)
    if not l or not p:raise ValueError("FFmpeg loudness/true peak missing")
    values=(float(l[1]),float(p[1]))
    if not all(math.isfinite(x) for x in values):raise ValueError("Nonfinite R128 result")
    return {"integrated_lufs": values[0], "true_peak_dbtp": values[1]}


def scan_stress(readers,gains,scenario,*,evaluation_start,ffmpeg,rate=RATE):
    if rate!=RATE:raise ValueError("Unsupported sample rate")
    if scenario not in SCENARIOS:raise ValueError("Unsupported scenario")
    for reader in readers.values():reader.seek(evaluation_start)
    cmd=[str(ffmpeg),"-nostdin","-hide_banner","-nostats","-threads","1","-filter_threads","1",
         "-f","f32le","-ar",str(RATE),"-ac","2","-i","pipe:0",
         "-af","ebur128=peak=true","-f","null","-"]
    unsafe_bins=0;first_unsafe=None;sample_max=0.;max_at=None
    fault_max=0.; post_max=0.
    with tempfile.TemporaryFile(mode="w+t",encoding="utf-8") as logfile:
        p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=logfile)
        try:
            for i in range(1200):
                blocks=[]
                for t in v1.TRACKS:
                    x=readers[t].read(BIN,dtype="float32",always_2d=True)
                    if len(x)!=BIN:raise ValueError(f"Short source during fault test: {t}")
                    blocks.append(x)
                y=mix_block(blocks,gains,scenario,i)
                peak=float(np.max(np.abs(y)))
                if peak>sample_max:
                    sample_max,max_at=peak,(i+0.5)/10
                if 200<=i<500:fault_max=max(fault_max,peak)
                if 500<=i<820:post_max=max(post_max,peak)
                if peak>10**((CEILING-GUARDBAND)/20):
                    unsafe_bins+=1
                    if first_unsafe is None:first_unsafe=i/10
                p.stdin.write(y.astype("<f4").tobytes())
        except Exception:
            p.kill();p.wait()
            raise
        finally:
            if p.stdin and not p.stdin.closed:p.stdin.close()
        try:code=p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill();p.wait();raise ValueError("FFmpeg timeout in offline fault scan")
        logfile.seek(0)
        if code:raise ValueError(f"FFmpeg fault scan exited {code}")
        r128=parse_r128_summary(logfile.read())
    db=lambda x:20*math.log10(max(x,1e-30))
    return {"status":"measured",**r128,"sample_peak_dbfs":db(sample_max),
            "sample_peak_20_50_dbfs":db(fault_max),"sample_peak_50_82_dbfs":db(post_max),
            "worst_sample_peak_approx_time_seconds":max_at,
            "sample_peak_guardband_exceeding_100ms_bins":unsafe_bins,
            "first_sample_peak_guardband_exceedance_seconds":first_unsafe,
            "raw_true_peak_meets_guardband":r128["true_peak_dbtp"]<=CEILING-GUARDBAND,
            "no_audio_file_written":True}


def run(report_path,v3_path,audit_path,ffmpeg,output):
    report_path=Path(report_path).resolve(strict=True)
    v3_path=Path(v3_path).resolve(strict=True)
    audit_path=Path(audit_path).resolve(strict=True)
    ffmpeg=Path(ffmpeg).resolve(strict=True)
    output=Path(output)
    if output.exists():raise ValueError("Use a fresh output directory")
    if ffmpeg.is_dir():raise ValueError("FFmpeg path is a folder")
    report_bytes=report_path.read_bytes()
    trial=json.loads(report_bytes)
    v3_result=json.loads(v3_path.read_bytes())
    audit_bytes=audit_path.read_bytes()
    if hashlib.sha256(audit_bytes).hexdigest()!=trial["protocol"]["source_audit_sha256"]:
        raise ValueError("Original audit file changed")
    if v3_result["protocol"]["report_sha256"]!=hashlib.sha256(report_bytes).hexdigest():
        raise ValueError("v3 provenance report digest mismatch")
    audit=json.loads(audit_bytes)
    if len(audit["sources"])!=7 or {x["track"] for x in audit["sources"]}!=set(v1.TRACKS):
        raise ValueError("Unexpected source audit manifest")
    for a in audit["sources"]:
        if Path(a["source_path"]).resolve()!=Path(trial["source_provenance"][a["track"]]["path"]).resolve():
            raise ValueError("Source provenance path mismatch")
    verified=v1._check_source_files(trial)
    frontier,grid=verify_prior(v3_result,trial,audit_bytes,verified)
    _rate,_,_,end,sources=v1._source_windows(trial)
    start=trial["protocol"]["evaluation_source_frames"][0]
    if end-start!=RATE*120:raise ValueError("Wrong evaluation duration")
    validate_controller_contract()
    # Four frozen v3 frontier candidates, four disturbance scenarios each.
    results={}
    with ExitStack() as stack:
        readers={t:stack.enter_context(sf.SoundFile(sources[t]["path"],"r")) for t in v1.TRACKS}
        for name in frontier:
            results[name]={"gains_db":{t:float(grid[name][v1.INDEX[t]]) for t in v1.TRACKS},
                           "baseline_v3_true_peak_dbtp":v3_result["candidates"][name]["r128"]["true_peak_dbtp"],
                           "fault_scenarios":{}}
            for scenario in SCENARIOS:
                results[name]["fault_scenarios"][scenario]=scan_stress(
                    readers,grid[name],scenario,evaluation_start=start,ffmpeg=ffmpeg)
    for t,meta in sources.items():
        st=Path(meta["path"]).stat()
        if st.st_size!=meta["size"] or st.st_mtime_ns!=meta["mtime_ns"]:
            raise ValueError("Source modified during stress study: "+t)
    output.mkdir(parents=True,exist_ok=False)
    out={"protocol":{"kind":"seven_stem_fault_authority_audit_v4", "source":"frozen_october_4_development_passage",
          "injected_fault_db":FAULT_DB,"sustained_fault_seconds":[20,50],"isolated_hit_seconds":[20,20.1],
          "max_kit_cut_db":MAX_CUT,"hypothetical_slew_db_per_second":SLEW_DB_PER_SECOND,
          "hard_true_peak_ceiling_dbtp":CEILING,"exploratory_guardband_db":GUARDBAND,
          "v3_sha256":sha256(v3_path),"trial_sha256":sha256(report_path),"audit_sha256":sha256(audit_path),
          "source_verified":True,"host_writes":0,"audio_files_written":0,"live_eligible":False,
          "automatic_gain_decision_made":False,"musical_quality_validated":False},
         "scenarios":list(SCENARIOS),"frontier":frontier,"candidate_results":results,
         "automatic_selection":None,
         "limitations":["Synthetic gain disturbances are not measured real-world future input variability.",
          "Clairvoyant ramp knows the beginning and end of disturbance, not achievable online.",
          "Instantaneous -3 dB step violates the slew bound: an unrealistic authority comparison only.",
          "Failure of tested maxcut policies is not a mathematical certificate against every adaptive policy.",
          "Source KIT saturation may preexist downstream mixing; attenuation cannot repair it.",
          "No live controller, host effects, spatial room mix, listener evaluation or musical optimum."]}
    (output/"summary.json").write_text(json.dumps(out,indent=2,allow_nan=False),encoding="utf-8")
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--v3-summary",type=Path,required=True)
    p.add_argument("--audit",type=Path,required=True)
    p.add_argument("--ffmpeg",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    try:
        d=run(a.report,a.v3_summary,a.audit,a.ffmpeg,a.output)
        print(json.dumps({"output":str(a.output.resolve()),"verified_sources":7,
          "candidate_count":len(d["frontier"]),"scenario_count":len(d["scenarios"]),
          "sustained_fault_maxcut_pass":{
              n:d["candidate_results"][n]["fault_scenarios"]["clairvoyant_slew_limited_maxcut"]["raw_true_peak_meets_guardband"]
              for n in d["frontier"]},"automatic_selection":None,"host_writes":0},indent=2))
    except (ValueError,KeyError,OSError,TypeError,RuntimeError,AssertionError) as exc:
        p.exit(1,f"WCFI offline fault authority audit stopped: {exc}\n")

if __name__=="__main__":main()
