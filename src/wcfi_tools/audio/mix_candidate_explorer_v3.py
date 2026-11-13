"""WCFI v3: fail-closed OFFLINE, static-gain, multiobjective candidate explorer.

Never writes audio, accesses Ableton, adjusts a live gain, or selects a 'best mix'.
It operates only on the frozen, exactly verified seven-stem development passage.
Physical energy margins are descriptive proxies, not perceptual assessments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import soundfile as sf

from . import mix_objective_audit as v1
from . import mix_objective_audit_v2 as v2

# Explicit bounded research grid. These are NOT default mixing recommendations.
RED_DELTAS_DB = (0.0, -1.5, -3.0)
KIT_DELTAS_DB = (0.0, 1.5, 3.0)
TARGET_LUFS = -24.0
CEILING_DBTP = -3.0
EXPLORATORY_GUARDBAND_DB = 0.5


def build_variants(baseline):
    if set(baseline) != set(v1.TRACKS):
        raise ValueError("Expected complete seven-stem gain baseline")
    base = np.array([baseline[name] for name in v1.TRACKS], dtype=np.float64)
    if not np.isfinite(base).all():
        raise ValueError("Nonfinite static baseline")
    proposals = {}
    for red in RED_DELTAS_DB:
        for kit in KIT_DELTAS_DB:
            label = ("frozen_static" if red == 0 and kit == 0 else
                     f"red_{red:+.1f}_kit_{kit:+.1f}".replace("+", "p").replace("-", "m").replace(".", "_"))
            g = base.copy()
            g[v1.INDEX["RED"]] += red
            g[v1.INDEX["KIT"]] += kit
            proposals[label] = g
    assert len(proposals) == 9
    return proposals


def _verify_v2_report(v2_result, trial, verified, recomputed_events):
    # Caller independently compares the actual raw file hash; this ensures no
    # normalized serialization is accidentally used as an identity check.
    if v2_result.get("protocol", {}).get("kind") != "seven_stem_offline_physical_proxy_audit_v2":
        raise ValueError("Wrong version of source audit")
    for name in v1.TRACKS:
        earlier = v2_result["verified_sources"][name]["verified_sha256"]
        if earlier != verified[name]["verified_sha256"]:
            raise ValueError(f"v2 source fingerprint mismatch: {name}")
    if v2_result["protocol"].get("host_writes") != 0 or v2_result["protocol"].get("automatic_gain_decision_made") is not False:
        raise ValueError("Prior source audit is not an offline descriptive audit")
    for section, e in recomputed_events.items():
        prior = v2_result["transient_candidates"].get(section)
        if prior is None:
            raise ValueError("v2 onset section missing")
        for band in v2.BANDS:
            old = prior["by_band"][band]
            new = e["by_band"][band]
            if old["selected_hop_indices"] != new["selected_hop_indices"] or old["candidate_count"] != new["candidate_count"]:
                raise ValueError(f"v2 onset evidence mismatch: {section} {band}")
    return True


def only_energy_supported(events, energy):
    """Filter v2's SOURCE-only peaks by its own local, section-relative floor.

    Weak-energy events are not discarded from the v2 report; v3 measures
    supported events independently and abstains if an entire section is empty.
    """
    energy = np.asarray(energy, dtype=float)
    if energy.ndim != 1 or not np.isfinite(energy).all() or (energy < 0).any():
        raise ValueError("Invalid source-only energy vector")
    result = {}
    for label, start, end in v2.SECTIONS:
        a, b = max(0, start * 100), min(len(energy), end * 100)
        if a >= b:
            raise ValueError("Expected four complete sections")
        floor = max(float(np.percentile(energy[a:b], 20)), 1e-14)
        section = dict(events[label])
        section["by_band"] = {}
        for band in v2.BANDS:
            original = events[label]["by_band"][band]
            indices = original["selected_hop_indices"]
            if any(type(i) is not int or i < a or i >= b for i in indices) or len(set(indices)) != len(indices):
                raise ValueError("Invalid event indices")
            supported = [i for i in indices if energy[i] >= 2.0 * floor]
            if len(supported) != original["energy_supported_count"]:
                raise ValueError("v2 event support counts changed")
            section["by_band"][band] = {**original,
                "selected_hop_indices": supported, "candidate_count": len(supported),
                "excluded_weak_energy_candidates": len(indices) - len(supported)}
        result[label] = section
    return result


def safety_and_normalization(lufs, tp, *, target_lufs=TARGET_LUFS,
                             ceiling=CEILING_DBTP, guardband=EXPLORATORY_GUARDBAND_DB):
    if not all(math.isfinite(x) for x in (lufs, tp, target_lufs, ceiling, guardband)):
        raise ValueError("Missing finite loudness and true peak")
    if guardband < 0 or guardband > 3:
        raise ValueError("Invalid peak guardband")
    attenuation = target_lufs - lufs
    if not -36 <= attenuation <= 0:
        raise ValueError("Unexpected loudness normalization request")
    normalized_tp = tp + attenuation
    gate = ceiling - guardband
    return {"raw_true_peak_dbtp": float(tp),
        "raw_meets_peak_ceiling": bool(tp <= ceiling),
        "raw_meets_exploratory_guardband": bool(tp <= gate),
        "scalar_gain_to_target_lufs_db": float(attenuation),
        "predicted_after_scalar_normalization_true_peak_dbtp": float(normalized_tp),
        "predicted_normalized_meets_guardband": bool(normalized_tp <= gate),
        "normalization_is_theoretical_only": True}


def _score_candidate(metrics, attack, r128, name, base):
    item = metrics[name]
    lead = item["global"]["lead_vs_all_stems_speech_band_db"]["p10_db"]
    per_section = {}
    for section, _, _ in v2.SECTIONS:
        low = attack[section]["low_40_180"][name]
        high = attack[section]["high_800_7000"][name]
        per_section[section] = {"low":low,"high":high}
    low_tail=[x["low"]["p10_db"] for x in per_section.values()]
    high_tail=[x["high"]["p10_db"] for x in per_section.values()]
    gain = item["gains_db"]
    quality = "potential_input_saturation_on_KIT" if gain["KIT"] > base["KIT"] else "no_additional_KIT_boost"
    return {"gains_db":gain,"lead_speechband_p10_db":lead,
        "worst_section_energy_supported_low_attack_p10_db": min(low_tail) if all(x is not None for x in low_tail) else None,
        "worst_section_energy_supported_high_attack_p10_db": min(high_tail) if all(x is not None for x in high_tail) else None,
        "per_section":per_section,
        "r128":r128[name],
        "headroom": safety_and_normalization(r128[name]["integrated_lufs"],r128[name]["true_peak_dbtp"]),
        "gain_change_l1_db":float(sum(abs(gain[t]-base[t]) for t in v1.TRACKS)),
        "kit_source_quality_caution": quality,
        "automatically_eligible":False,
        "not_an_accepted_musical_mix":True}


def pareto_among_feasible(profiles):
    """Physical feasibility first; no preference weights and no winner."""
    for name, p in profiles.items():
        p["raw_safe"] = p["headroom"]["raw_meets_exploratory_guardband"]
        p["normalized_safe"] = p["headroom"]["predicted_normalized_meets_guardband"]
        p["proxy_complete"] = all(p[k] is not None for k in (
            "lead_speechband_p10_db", "worst_section_energy_supported_low_attack_p10_db",
            "worst_section_energy_supported_high_attack_p10_db"))
        p["screening_status"] = ("insufficient_proxy_data" if not p["proxy_complete"] else
                                 "requires_scalar_output_attenuation" if not p["raw_safe"] and p["normalized_safe"] else
                                 "exceeds_normalized_peak_guardband" if not p["normalized_safe"] else
                                 "physically_screened_provisional")
    eligible = [name for name,p in profiles.items() if p["raw_safe"] and p["proxy_complete"]]
    objectives = ("lead_speechband_p10_db", "worst_section_energy_supported_low_attack_p10_db",
                  "worst_section_energy_supported_high_attack_p10_db")
    # True peak is a HARD constraint, not an optimization reward: extra headroom
    # alone does not mean that one musical balance is better than another.
    for name,p in profiles.items():
        if name not in eligible:
            p["raw_safe_pareto_status"]="not_compared"
            p["dominated_by"] = []
            continue
        dominated=[]
        for other in eligible:
            if other==name:continue
            q=profiles[other]
            if all(q[k]>=p[k]-1e-9 for k in objectives) and any(q[k]>p[k]+1e-9 for k in objectives):
                dominated.append(other)
        p["raw_safe_pareto_status"]="dominated" if dominated else "nondominated"
        p["dominated_by"]=dominated
    return [name for name in eligible if profiles[name]["raw_safe_pareto_status"]=="nondominated"]


def run(report_path, v2_path, output, *, audit_path, ffmpeg):
    """Read only exact frozen sources; write one bounded summary, never PCM."""
    report_path=Path(report_path).resolve(strict=True)
    v2_path=Path(v2_path).resolve(strict=True)
    audit_path=Path(audit_path).resolve(strict=True)
    output=Path(output)
    if output.exists():raise ValueError("Use a fresh output directory")
    if Path(ffmpeg).resolve(strict=True).is_dir():
        raise ValueError("ffmpeg must be an executable file")
    report_text=report_path.read_bytes()
    trial=json.loads(report_text)
    v2_report=json.loads(v2_path.read_text(encoding="utf-8"))
    audit_raw=audit_path.read_bytes()
    if hashlib.sha256(audit_raw).hexdigest()!=trial["protocol"]["source_audit_sha256"]:
        raise ValueError("Source audit mismatch")
    audit=json.loads(audit_raw)
    if len(audit["sources"])!=7 or {x["track"] for x in audit["sources"]}!=set(v1.TRACKS):
        raise ValueError("Unexpected manifest track set")
    for record in audit["sources"]:
        if Path(record["source_path"]).resolve()!=Path(trial["source_provenance"][record["track"]]["path"]).resolve():
            raise ValueError("Source audit path mismatch")
    if v2_report["protocol"]["source_report_sha256"] != hashlib.sha256(report_text).hexdigest():
        raise ValueError("v2 source report identity mismatch")
    verified=v1._check_source_files(trial)
    rate, _, _, ev_end, sources=v1._source_windows(trial)
    ev_start=trial["protocol"]["evaluation_source_frames"][0]
    length=ev_end-ev_start
    if length!=rate*120:
        raise ValueError("Expected exact 120 second development passage")
    variants=build_variants(trial["protocol"]["coordinator_settings"]["baseline_gains_db"])
    with ExitStack() as stack:
        readers={t:stack.enter_context(sf.SoundFile(sources[t]["path"],"r")) for t in v1.TRACKS}
        readers["KIT"].seek(ev_start)
        flux,energy=v2.kit_flux_10ms(readers["KIT"],length)
        recomputed_events=v2.select_local_events(flux,energy)
        _verify_v2_report(v2_report,trial,verified,recomputed_events)
        supported=only_energy_supported(recomputed_events,energy)
        readers["KIT"].seek(ev_start)
        saturation=v2.source_saturation(readers["KIT"],length)
        if saturation["full_scale_sample_count"] != v2_report["saturation"]["KIT"]["full_scale_sample_count"]:
            raise ValueError("KIT saturation evidence does not match v2")
        for reader in readers.values():reader.seek(ev_start)
        features,powers,_stem_peaks,raw_peaks,raw_rms=v1._capture_features(readers,rate,length,variants)
        objectives,_=v1.evaluate_objectives(features,powers,variants,
            trial["protocol"]["coordinator_settings"]["activity_thresholds_dbfs"],
            mix_peak=raw_peaks,mix_power_sum=raw_rms,frames_per_bin=4800)
        for reader in readers.values():reader.seek(ev_start)
        attacks=v2.capture_section_attack_margins(readers,supported,variants,evaluation_start=ev_start)
        for reader in readers.values():reader.seek(ev_start)
        loudness=v2._scan_ffmpeg_pipe(readers,variants,ffmpeg=ffmpeg,frames=length,evaluation_start=ev_start)
    # Reject source mutation during analysis rather than publishing stale evidence.
    for t,meta in sources.items():
        st=Path(meta["path"]).stat()
        if st.st_size!=meta["size"] or st.st_mtime_ns!=meta["mtime_ns"]:
            raise ValueError(f"Source changed during exploration: {t}")
    base=objectives[next(iter(variants))]["gains_db"]
    profiles={name:_score_candidate(objectives,attacks,loudness,name,base) for name in variants}
    frontier=pareto_among_feasible(profiles)
    result={"protocol":{"kind":"seven_stem_offline_gain_candidate_explorer_v3",
                "source_date":"2026-10-04","analysis_seconds":120,
                "report_sha256":hashlib.sha256(report_text).hexdigest(),
                "v2_summary_sha256":hashlib.sha256(v2_path.read_bytes()).hexdigest(),
                "source_audit_sha256":hashlib.sha256(audit_raw).hexdigest(),
                "candidate_axes_db":{"RED":list(RED_DELTAS_DB),"KIT":list(KIT_DELTAS_DB)},
                "target_lufs":TARGET_LUFS,"peak_ceiling_dbtp":CEILING_DBTP,
                "exploratory_guardband_db":EXPLORATORY_GUARDBAND_DB,
                "no_audio_output":True,"host_writes":0,"live_eligible":False,
                "automatic_gain_decision_made":False,"musical_quality_validated":False},
            "verified_sources":verified,"kit_source_saturation":saturation,
            "backing_role_gate":{"YELLOW":"unverified_do_not_auto_boost",
                                "GREEN":"unverified_do_not_auto_boost"},
            "energy_supported_event_counts":{label:{band: x["candidate_count"] for band,x in section["by_band"].items()}
                for label,section in supported.items()},
            "candidates":profiles,"raw_safe_pareto_frontier":frontier,
            "automatic_selection":None,
            "limitations":["An exploratory grid is not a musical optimization or subjective preference.",
              "Attack events are source-only spectral changes, not verified kicks/snares.",
              "Band energy ratios do not establish speech intelligibility or masking.",
              "Input KIT saturation cannot be repaired by downstream scalar gain.",
              "YELLOW/GREEN independent vocal status remains unverified; unchanged in every candidate.",
              "R128 comparisons are descriptive; scalar loudness matching has not been rendered.",
              "Prior development recording, no independent held-out performance or calibrated artistic target.",
              "No live use, host effects, compressor, limiter or gain automation."]}
    output.mkdir(parents=True,exist_ok=False)
    (output/"summary.json").write_text(json.dumps(result,indent=2,allow_nan=False),encoding="utf-8")
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--v2-summary",type=Path,required=True)
    p.add_argument("--audit",type=Path,required=True)
    p.add_argument("--ffmpeg",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    try:
        result=run(a.report,a.v2_summary,a.output,audit_path=a.audit,ffmpeg=a.ffmpeg)
        print(json.dumps({"output":str(a.output.resolve()),"verified_sources":len(result["verified_sources"]),
             "candidate_count":len(result["candidates"]),
             "raw_safe_candidates":[n for n,x in result["candidates"].items() if x["raw_safe"]],
             "raw_safe_pareto_frontier":result["raw_safe_pareto_frontier"],
             "automatic_selection":result["automatic_selection"],
             "host_writes":result["protocol"]["host_writes"]},indent=2))
    except (ValueError,KeyError,OSError,TypeError,RuntimeError) as exc:
        p.exit(1,f"WCFI offline candidate explorer stopped: {exc}\n")


if __name__=="__main__":main()
