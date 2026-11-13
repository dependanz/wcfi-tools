"""Offline stereo-Master challenge sets; no device, DAW, model or network access."""
from __future__ import annotations

import copy
import html
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

from ..audio.kit_study import scan_loudness
from .local_listening import prepare_model_audio
from .profiles import file_hash, write_json

RATE = 48000
AXES = ("PQ", "CE", "CU", "PC")
EPSILON = 1e-6
PRIMARY_DEGRADATIONS = ("clip_severe", "lowpass", "hum", "dropouts")
CONDITIONS = ("current", "identical", "level_only", "clip_mild", "clip_severe",
              "lowpass", "hum", "dropouts", "candidate", "pre_fx")


def _audio(path):
    samples, rate = sf.read(path, dtype="float32", always_2d=True)
    if (rate != RATE or samples.shape[1] != 2 or not 10*RATE <= len(samples) <= 20*RATE
            or not np.isfinite(samples).all() or np.ptp(samples) < 1e-6):
        raise ValueError("Use finite active stereo Master excerpts, 10–20 seconds at 48 kHz")
    return samples


def interventions(current, pre_fx, candidate):
    """Known interventions, not ground-truth human preferences or repair recipes."""
    current, pre_fx, candidate = [np.asarray(x, dtype=np.float32) for x in
                                  (current, pre_fx, candidate)]
    if (current.ndim != 2 or current.shape[1] != 2 or len(current) < 10*RATE
            or len(current) > 20*RATE or any(x.shape != current.shape or
                not np.isfinite(x).all() for x in (current, pre_fx, candidate))):
        raise ValueError("Use aligned, finite stereo excerpts of equal length")
    variants = {"current": current.copy(), "identical": current.copy(),
        "level_only": (current*.5)*2, "candidate": candidate.copy(), "pre_fx": pre_fx.copy()}
    facts = {"current": {}, "identical": {"decoded_pcm_identical": True},
        "level_only": {"applied_gain_db": 20*math.log10(.5),
            "exact_matching_gain_db": 20*math.log10(2),
            "note": "An exact binary gain roundtrip; tests matched level-only invariance, not raw loudness invariance."},
        "candidate": {"human_preference_known": False},
        "pre_fx": {"clean_reference": False, "expected_preference": None}}
    for name, quantile in (("clip_mild", .99), ("clip_severe", .90)):
        threshold = float(np.quantile(np.abs(current), quantile))
        if threshold <= 1e-8:
            raise ValueError("Too little active audio for clipping controls")
        variants[name] = np.clip(current, -threshold, threshold)
        facts[name] = {"threshold": threshold, "threshold_quantile": quantile,
            "newly_limited_samples": int(np.count_nonzero(np.abs(current) > threshold)),
            "sample_fraction": float(np.mean(np.abs(current) > threshold))}
    variants["lowpass"] = sosfiltfilt(butter(6, 2000, fs=RATE, output="sos"),
                                      current, axis=0).astype(np.float32)
    facts["lowpass"] = {"kind": "Butterworth lowpass", "order": 6, "hz": 2000,
                       "phase": "forward/reverse offline challenge, not native plug-in emulation"}
    t = np.arange(len(current))/RATE
    hum = sum(a*np.sin(2*np.pi*f*t) for a, f in ((1, 60), (.5, 120), (.25, 180)))
    rms = float(np.sqrt(np.mean(current.astype(float)**2)))
    hum *= rms*10**(-12/20)/float(np.sqrt(np.mean(hum**2)))
    variants["hum"] = (current+hum[:, None]).astype(np.float32)
    facts["hum"] = {"frequencies_hz": [60, 120, 180], "hum_to_program_rms_db": -12}
    envelope = np.ones(len(current), dtype=np.float32)
    windows = []
    for fraction in (.2, .4, .6, .8):
        start, width, ramp = round(fraction*len(current)), round(.25*RATE), round(.002*RATE)
        envelope[start:start+width] = 0
        envelope[start:start+ramp] = np.linspace(1, 0, ramp)
        envelope[start+width-ramp:start+width] = np.linspace(0, 1, ramp)
        windows.append([start/RATE, (start+width)/RATE])
    variants["dropouts"] = current*envelope[:, None]
    facts["dropouts"] = {"windows_seconds": windows, "edge_ramp_ms": 2}
    return {c: variants[c] for c in CONDITIONS}, facts


def signal_metrics(audio):
    x = audio.astype(float)
    rms = float(np.sqrt(np.mean(x*x)))
    peak = float(np.max(np.abs(x)))
    mono = x.mean(axis=1)
    denominator = float(np.sqrt(np.sum(x[:, 0]**2)*np.sum(x[:, 1]**2)))
    return {"rms_dbfs": 20*math.log10(max(rms, 1e-20)),
        "sample_peak_dbfs": 20*math.log10(max(peak, 1e-20)),
        "crest_db": 20*math.log10(max(peak, 1e-20)/max(rms, 1e-20)),
        "lr_correlation": float(np.sum(x[:, 0]*x[:, 1])/denominator) if denominator else None,
        "mono_energy_change_db": 10*math.log10(max(float(np.mean(mono*mono)), 1e-30)/max(rms*rms, 1e-30)),
        "zero_stereo_frame_fraction": float(np.mean(np.max(np.abs(x), axis=1) < 1e-12))}


def prepare(profile, output, *, ffmpeg, seed=20261022):
    profile, output = Path(profile).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Existing benchmarks are never overwritten; choose a new directory")
    config = json.loads(profile.read_text(encoding="utf-8"))
    if config.get("kind") != "wcfi_master_benchmark_sources" or config.get("schema_version") != 1:
        raise ValueError("Use a version 1 Master benchmark source profile")
    sessions = config.get("sessions", [])
    if not 1 <= len(sessions) <= 12 or len({s["id"] for s in sessions}) != len(sessions):
        raise ValueError("Choose 1–12 distinct session excerpts")
    resolved = []
    for session in sessions:
        paths = {}
        for stage in ("pre_fx", "post_fx", "candidate"):
            item = session[stage]
            path = (profile.parent/item["file"]).resolve()
            if file_hash(path) != item["sha256"]:
                raise ValueError("A source Master excerpt changed")
            paths[stage] = path
        gain = session.get("output_gain_db", 0)
        if type(gain) not in (int, float) or not math.isfinite(gain) or not -24 <= gain <= 0:
            raise ValueError("Use a verified Master output gain between −24 and 0 dB")
        resolved.append((copy.deepcopy(session), paths))
    output.mkdir(parents=True)
    (output/"clips").mkdir(); (output/"model-inputs").mkdir()
    protocol = {"kind": "wcfi_master_benchmark_protocol", "schema_version": 1,
        "primary_axis": "PQ", "identical_score_epsilon": EPSILON,
        "expected_lower_conditions": list(PRIMARY_DEGRADATIONS),
        "secondary_conditions": ["clip_mild", "candidate", "pre_fx"],
        "null_conditions": ["identical", "level_only"],
        "matching": "One scalar LUFS match per full mix; no per-source normalization. Raw engineering levels retained separately.",
        "diagnostic_margin": .1,
        "margin_note": "0.1 PQ is a reporting bin only, not a calibrated audibility/preference threshold.",
        "pass_rule": "All exact nulls and baseline repeats tie within epsilon; every preregistered coarse degradation receives lower PQ than its session baseline. This is a coarse challenge gate only.",
        "professional_mixing_judgment_validated": False,
        "limitations": ["Expected degradation directions are engineering hypotheses, not human preference labels.",
            "Already poor mixes can respond unexpectedly; coarse-control success does not validate subtle preferences.",
            "Only independent service excerpts count as independent contexts; variants/repeats do not multiply the number of services.",
            "The pre-FX mix is not a clean reference or a mandatory lower-quality version.",
            "Audiobox consumes mono 16 kHz, losing stereo and frequencies above 8 kHz.",
            "No human panel or professional same-performance reference is included in this benchmark."]}
    write_json(output/"protocol.json", protocol)
    rng = np.random.default_rng(seed)
    ids = iter(rng.permutation(np.arange(1, len(sessions)*len(CONDITIONS)+1)).tolist())
    public, truth, rechecks = [], [], []
    source_audit = []
    for session, paths in resolved:
        pre, post, candidate = [_audio(paths[k]) for k in ("pre_fx", "post_fx", "candidate")]
        gain = 10**(session.get("output_gain_db", 0)/20)
        variants, facts = interventions(post*gain, pre, candidate*gain)
        readings, names = {}, {}
        for condition, audio in variants.items():
            identity = f"clip-{next(ids):04d}"; names[condition] = identity
            path = output/"clips"/(identity+".wav")
            sf.write(path, audio, RATE, subtype="FLOAT")
            readings[condition] = scan_loudness(path, ffmpeg)
        target = min(-24, min(-3-r["true_peak_dbtp"]+r["integrated_lufs"] for r in readings.values()))
        baseline_pcm = None
        for condition, audio in variants.items():
            identity = names[condition]; path = output/"clips"/(identity+".wav")
            matching_gain = target-readings[condition]["integrated_lufs"]
            for _ in range(4):
                if condition in ("identical", "level_only"):
                    assert baseline_pcm is not None and np.array_equal(audio, variants["current"])
                    matched = baseline_pcm.copy()
                else:
                    matched = (audio*10**(matching_gain/20)).astype(np.float32)
                sf.write(path, matched, RATE, subtype="FLOAT")
                checked = scan_loudness(path, ffmpeg)
                if abs(checked["integrated_lufs"]-target) <= .11:
                    break
                matching_gain += target-checked["integrated_lufs"]
            if abs(checked["integrated_lufs"]-target) > .15 or checked["true_peak_dbtp"] > -2.85:
                raise ValueError("Could not create a fair, peak-safe loudness match")
            if condition == "current":
                baseline_pcm = matched.copy(); rechecks.append(identity)
            conversion = prepare_model_audio(path, output/"model-inputs"/(identity+".wav"))
            public.append({"id": identity, "stereo_file": "clips/"+identity+".wav",
                "stereo_sha256": file_hash(path), "model_file": "model-inputs/"+identity+".wav",
                "model_sha256": conversion["sha256"], "duration_seconds": len(audio)/RATE})
            truth.append({"id": identity, "session_id": session["id"], "condition": condition,
                "intervention": facts[condition], "original_metrics": readings[condition] | signal_metrics(audio),
                "matched_metrics": checked | signal_metrics(matched),
                "matching_gain_db": matching_gain, "target_lufs": target, "conversion": conversion})
        for stage, path in paths.items():
            if file_hash(path) != session[stage]["sha256"]:
                raise ValueError("A source changed during preparation")
        source_audit.append(session | {"source_files_preserved": True})
    rng.shuffle(public); rng.shuffle(rechecks)
    write_json(output/"private-answer-key.json", {"sessions": source_audit, "clips": truth})
    manifest = {"kind": "wcfi_master_benchmark_blind", "schema_version": 1,
        "seed": seed, "protocol_sha256": file_hash(output/"protocol.json"),
        "answer_key_sha256": file_hash(output/"private-answer-key.json"),
        "clips": public, "repeat_ids": rechecks,
        "audio_uploads": 0, "daw_operations": 0}
    write_json(output/"blind-manifest.json", manifest)
    return {"sessions": len(sessions), "clips": len(public), "inferences_with_repeats": len(public)+len(rechecks),
        "directory": str(output), "protocol_sha256": manifest["protocol_sha256"], "audio_uploads": 0}


def validate_blind(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory/"blind-manifest.json").read_text())
    if manifest.get("kind") != "wcfi_master_benchmark_blind" or manifest.get("schema_version") != 1:
        raise ValueError("Not a Master benchmark")
    if file_hash(directory/"protocol.json") != manifest["protocol_sha256"]:
        raise ValueError("Benchmark protocol changed")
    seen = set()
    for item in manifest["clips"]:
        identifier = item["id"]
        if (not isinstance(identifier, str) or not identifier.startswith("clip-")
                or not identifier[5:].isdigit() or identifier in seen):
            raise ValueError("Invalid or duplicate blind clip identity")
        seen.add(identifier)
        for key, hash_key, folder in (("model_file", "model_sha256", "model-inputs"),
                                      ("stereo_file", "stereo_sha256", "clips")):
            path = (directory/item[key]).resolve()
            if path.parent != directory/folder or file_hash(path) != item[hash_key]:
                raise ValueError("Benchmark input changed or escaped its directory")
        info = sf.info(directory/item["model_file"])
        if (info.samplerate != 16000 or info.channels != 1
                or abs(info.duration-item["duration_seconds"]) > 1/16000):
            raise ValueError("Model input format changed")
    if (len(manifest["repeat_ids"]) != len(set(manifest["repeat_ids"]))
            or not set(manifest["repeat_ids"]) <= seen):
        raise ValueError("Invalid baseline repeat schedule")
    return manifest


def summarize(directory, run_directory):
    """Unblind only a complete immutable run; never turn a tiny score delta into an endorsement."""
    directory, run_directory = Path(directory).resolve(), Path(run_directory).resolve()
    manifest = validate_blind(directory)
    if file_hash(directory/"private-answer-key.json") != manifest["answer_key_sha256"]:
        raise ValueError("Benchmark answer key changed")
    batch = json.loads((run_directory/"batch-result.json").read_text())
    if batch["status"] != "complete" or batch["manifest_sha256"] != file_hash(directory/"blind-manifest.json"):
        raise ValueError("Only completed runs of these exact inputs can be scored")
    if (batch['runtime'].get('device') != 'cpu' or batch.get('gpu_initialized') is not False
            or batch.get('network_connection_attempts') != 0 or batch.get('daw_operations') != 0):
        raise ValueError("A CPU-only offline run with zero DAW operations is required")
    protocol = json.loads((directory/"protocol.json").read_text())
    key = json.loads((directory/"private-answer-key.json").read_text())
    labels = {c["id"]: c for c in key["clips"]}
    rows, first, repeats = [], {}, []
    for entry in batch["predictions"]:
        path = (run_directory/entry["file"]).resolve()
        if path.parent != run_directory or file_hash(path) != entry["sha256"]:
            raise ValueError("Prediction changed or escaped its run directory")
        record = json.loads(path.read_text()); identifier = record["id"]
        if identifier not in labels:
            raise ValueError("Prediction refers to an unknown clip")
        score = record["prediction"]["scores"]
        if set(score) != set(AXES) or any(not math.isfinite(v) for v in score.values()):
            raise ValueError("Prediction axes are invalid")
        if record["input_sha256"] != next(c["model_sha256"] for c in manifest["clips"] if c["id"] == identifier):
            raise ValueError("Prediction was made on different audio")
        row = labels[identifier] | {"scores": score, "elapsed_seconds": record["prediction"]["elapsed_seconds"]}
        if record["repeat"]:
            repeats.append(row)
        elif identifier in first:
            raise ValueError("Duplicate first-pass prediction")
        else:
            first[identifier] = row; rows.append(row)
    if set(first) != set(labels) or sorted(r["id"] for r in repeats) != sorted(manifest["repeat_ids"]):
        raise ValueError("Missing benchmark predictions")
    checks, candidates = [], []
    for session in key["sessions"]:
        cases = {r["condition"]: r for r in rows if r["session_id"] == session["id"]}
        if set(cases) != set(CONDITIONS):
            raise ValueError("Incomplete session challenge set")
        baseline = cases["current"]
        for condition, row in cases.items():
            row["pq_delta_from_current"] = row["scores"]["PQ"]-baseline["scores"]["PQ"]
            row["independent_human_preference"] = None
            if condition in protocol["null_conditions"]:
                same_input = row["conversion"]["sha256"] == baseline["conversion"]["sha256"]
                ties = all(abs(row["scores"][a]-baseline["scores"][a]) <= EPSILON for a in AXES)
                checks.append({"session_id": session["id"], "condition": condition,
                    "type": "matched_null", "same_model_input": same_input, "passed": same_input and ties,
                    "pq_delta": row["pq_delta_from_current"]})
            elif condition in protocol["expected_lower_conditions"]:
                delta = row["pq_delta_from_current"]
                checks.append({"session_id": session["id"], "condition": condition,
                    "type": "coarse_degradation", "passed": delta < -EPSILON, "pq_delta": delta,
                    "score_direction": "lower" if delta < -EPSILON else "higher" if delta > EPSILON else "tie",
                    "reaches_reporting_margin": abs(delta) >= protocol["diagnostic_margin"]})
            elif condition in ("candidate", "clip_mild", "pre_fx"):
                candidates.append({"session_id": session["id"], "condition": condition,
                    "pq_delta": row["pq_delta_from_current"], "preference_established": False})
    for repeated in repeats:
        baseline = first[repeated["id"]]
        checks.append({"session_id": repeated["session_id"], "condition": "repeat_current",
            "type": "repeat", "passed": all(abs(repeated["scores"][a]-baseline["scores"][a]) <= EPSILON for a in AXES),
            "pq_delta": repeated["scores"]["PQ"]-baseline["scores"]["PQ"]})
    summary = {"kind": "wcfi_master_benchmark_result", "schema_version": 1,
        "model": batch["runtime"]["model"], "runtime": batch["runtime"],
        "session_sources": key["sessions"],
        "independent_service_excerpts": len(key["sessions"]), "inferences": len(batch["predictions"]),
        "checks": checks, "coarse_control_gate_passed": all(c["passed"] for c in checks),
        "totals": {kind: {"passed": sum(c["passed"] for c in checks if c["type"] == kind),
                           "total": sum(c["type"] == kind for c in checks)}
                   for kind in ("matched_null", "repeat", "coarse_degradation")},
        "timing": batch["timing"], "worker_memory": batch.get("final_worker_memory", {}),
        "candidates": candidates, "rows": rows,
        "professional_mixing_judgment_validated": False, "automatic_edits_authorized": False,
        "human_preference_data_present": False, "limitations": protocol["limitations"],
        "audio_uploads": 0, "daw_operations": 0}
    write_json(run_directory/"results.json", summary)
    (run_directory/"report.html").write_text(render_report(summary), encoding="utf-8")
    return summary


def render_report(result):
    escape = html.escape
    rows = "".join("<tr>"+"".join(f"<td>{escape(str(v))}</td>" for v in (
        r["session_id"], r["condition"], f"{r['scores']['PQ']:.4f}",
        f"{r['pq_delta_from_current']:+.4f}", f"{r['matched_metrics']['integrated_lufs']:.1f}",
        f"{r['elapsed_seconds']:.2f}s"))+"</tr>" for r in sorted(result["rows"], key=lambda r:(r["session_id"], CONDITIONS.index(r["condition"]))))
    totals = "".join(f"<li>{escape(k)}: {v['passed']}/{v['total']}</li>" for k,v in result["totals"].items())
    limits = "".join(f"<li>{escape(v)}</li>" for v in result["limitations"])
    excerpts = "".join(f"<li>{escape(s['id'])}: {escape(str(s.get('take_id', 'test fixture')))} · "
        f"{s.get('start_seconds', 0):g}–{s.get('start_seconds', 0)+s.get('duration_seconds', 0):g} seconds</li>"
        for s in result.get('session_sources', []))
    failures = "".join(f"<li>{escape(c['session_id'])} · {escape(c['condition'])}: "
        f"Δ PQ {c['pq_delta']:+.4f}</li>" for c in result['checks'] if not c['passed'])
    median = result['timing'].get('median_seconds')
    timing = f"{median:.2f} seconds median per excerpt" if median is not None else "Timing not recorded"
    peak = result.get('worker_memory', {}).get('peak_working_set_mib')
    memory = f" · peak worker memory {peak:.1f} MiB" if peak is not None else ''
    gate = "PASSED" if result["coarse_control_gate_passed"] else "FAILED"
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>WCFI Master evaluator benchmark</title><style>body{font:16px system-ui;background:#f5f7fa;color:#153d58;max-width:1150px;margin:36px auto;padding:0 24px}"
        "h1,h2{color:#123b57}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:9px;text-align:left;border-bottom:1px solid #cdd7e0}"
        ".notice{background:#fff3df;border:1px solid #e4c88b;padding:18px;border-radius:10px}.scroll{overflow-x:auto}li{margin:8px 0}</style>"
        "<h1>Master evaluator · Audiobox CPU benchmark</h1>"
        f"<p>{result['independent_service_excerpts']} service excerpts · {result['inferences']} independent inference calls · CPU only</p>"
        f"<div class='notice'><strong>Coarse challenge gate: {gate}.</strong> "
        "Professional mixing judgment remains unvalidated. PQ is a learned prediction, not a measured quality unit. "
        "A higher score does not authorize an edit. No audio was uploaded or played.</div>"
        f"<ul>{totals}</ul><p>{escape(timing)}{escape(memory)} · one CPU thread · GPU uninitialized</p>"
        f"<h2>Source excerpts</h2><ul>{excerpts}</ul>"
        +(f"<h2>Failed checks</h2><ul>{failures}</ul>" if failures else "")+
        "<h2>Same-performance comparisons</h2>"
        "<p>All full-mix versions are scalar loudness-matched. Master pre-FX is an observation, not a clean reference. "
        "Current means the saved current chain, not a historical service broadcast. Candidate is the existing local RED/KIT processing trial; no human preference is assigned.</p>"
        "<div class='scroll'><table><thead><tr><th>Session</th><th>Condition</th><th>PQ</th><th>Δ PQ vs current</th><th>LUFS</th><th>Inference</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div><h2>Limits</h2><ul>{limits}</ul>"
        "<p>Full evidence: <a href='results.json'>results.json</a> · <a href='runtime.json'>runtime.json</a></p></html>")
