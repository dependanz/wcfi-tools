"""External human-rated SongBench challenge. No DAW, audio-device or upload APIs."""
from __future__ import annotations

import csv
import hashlib
import html
import itertools
import json
import math
import re
import shutil
from collections import defaultdict
from pathlib import Path

from .profiles import file_hash, write_json

AXES = ("Melody", "Arrangement", "Musicality", "Vocal", "Instrumental", "Mixing", "Structure")
POLICY = {
    "kind": "wcfi_songbench_external_policy", "schema_version": 1,
    "primary_axis": "Mixing", "other_axes": "descriptive_only_no_alternative_gate",
    "secondary_analysis": "ODAQ processed conditions alone, excluding reference and LP35/LP70 anchors",
    "audio": "Full published excerpts; mono average, soxr HQ 24kHz float32; no loudness changes",
    "human_pair": "Same song/trial and shared participants; exclude either mix author in MED",
    "minimum_shared_raters": 8, "human_ci": "95% percentile paired-rater bootstrap, 10000 draws",
    "decisive_pair": "Human mean difference CI excludes zero; ties are not decisive",
    "score_tie_tolerance": 1e-6, "predicted_tie_credit": 0.5,
    "aggregation": "Equal weight per song/reference content, not per pair or audio version",
    "model_ci": "95% percentile content-cluster bootstrap, 10000 draws",
    "screening_gate": {"MED_minimum_accuracy": .75, "ODAQ_minimum_accuracy": .80,
                       "minimum_groups_per_dataset": 6, "minimum_decisive_pairs_per_dataset": 12,
                       "lower_95_ci_strictly_above": .5, "all_repeat_checks_required": True},
    "threshold_status": "Prospective engineering screening requirements, not an industry standard",
    "seed": 20261022, "training_or_tuning": False,
    "claims": "External screening only; cannot certify professional church mixing",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _model_wave(path):
    import numpy as np
    import soundfile as sf
    import soxr

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    if not len(audio) or len(audio) > rate * 120 or not np.isfinite(audio).all():
        raise ValueError(f"Invalid or over-budget external audio: {path.name}")
    # Match librosa 0.11.0's soundfile, to_mono and soxr_hq load path.
    mono = np.mean(audio.T, axis=0)
    if rate != 24000:
        length = math.ceil(len(mono) * 24000 / rate)
        mono = soxr.resample(mono, rate, 24000, quality="HQ")
        mono = np.pad(mono, (0, max(0, length-len(mono))))[:length]
    return mono.astype("float32"), {
        "source_rate": rate, "source_channels": audio.shape[1],
        "source_seconds": len(audio)/rate,
        "source_rms_dbfs": 10*math.log10(max(float(np.mean(audio.astype("float64")**2)), 1e-30)),
        "source_peak": float(np.max(np.abs(audio))),
    }


def _med_ratings(mix):
    values = defaultdict(list)
    for item in mix["evaluations"]:
        rating = item.get("rating")
        if isinstance(rating, (float, int)) and not isinstance(rating, bool):
            if not math.isfinite(rating) or not 0 <= rating <= 1:
                raise ValueError("MED rating outside documented normalized range")
            values[item["participant_id"]].append(rating)
    return {key: sum(value)/len(value) for key, value in values.items()}


def prepare(assets_root, output):
    """Prepare opaque inputs and a separate human answer key; never infer here."""
    import numpy as np
    import soundfile as sf

    root, output = Path(assets_root).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Choose a new immutable benchmark directory")
    selection = read_json(root/"med-selection.json")
    med = read_json(root/"assets/med-data.json")
    songs = {s["song_id"]: s for s in med["songs"]}
    rows = list(csv.DictReader((root/"odaq/ODAQ/ODAQ_listening_test/ODAQ_results.csv")
                               .open(encoding="utf-8-sig")))
    records = []
    for item in selection["clips"]:
        song = songs[item["song"]]
        if "Creative Commons" not in str(song["license"]):
            raise ValueError("Only the selected Creative Commons MED songs are eligible")
        mix = next(m for m in song["mixes"] if m["mix_code"] == item["mix"])
        records.append({"dataset": "MED", "group": item["song"], "name": item["mix"],
                        "license": song["license"], "ratings": _med_ratings(mix),
                        "file": root/"med-audio"/(item["song"]+"__"+item["mix"]+".mp3")})
    for trial in read_json(root/"odaq-selection.json")["trials"]:
        by_process = defaultdict(dict)
        for row in rows:
            if row["item"] == trial["item"]:
                if row["subject"] in by_process[row["process"]]:
                    raise ValueError("Duplicate ODAQ subject/condition")
                by_process[row["process"]][row["subject"]] = float(row["score"])
        if len(by_process) != 8:
            raise ValueError("Expected all eight rated ODAQ trial conditions")
        for process, ratings in sorted(by_process.items()):
            if len(ratings) != 26 or not all(0 <= v <= 100 for v in ratings.values()):
                raise ValueError("Unexpected ODAQ ratings")
            records.append({"dataset": "ODAQ", "group": trial["item"], "name": process,
                            "method": trial["method"], "ratings": ratings,
                            "file": root/"odaq/ODAQ/ODAQ_listening_test"/trial["item"]/(process+".wav")})
    if any(not r["file"].is_file() for r in records):
        raise ValueError("One or more selected external audio files are missing")
    output.mkdir(parents=True)
    (output/"audio").mkdir()
    clips, answer = [], []
    for index, record in enumerate(records):
        identifier = hashlib.sha256(f"songbench-{POLICY['seed']}-{index}".encode()).hexdigest()[:20]
        waveform, measures = _model_wave(record["file"])
        destination = output/"audio"/(identifier+".wav")
        sf.write(destination, waveform, 24000, subtype="FLOAT")
        clip = {"id": identifier, "file": "audio/"+destination.name,
                "sha256": file_hash(destination), "seconds": len(waveform)/24000,
                "pcm_sha256": hashlib.sha256(waveform.tobytes()).hexdigest()}
        clips.append(clip)
        answer.append({k: v for k, v in record.items() if k != "file"} | {
            "id": identifier, "source_file": str(record["file"]),
            "source_sha256": file_hash(record["file"]), **measures})
    repeats = []
    for index, dataset in enumerate(("MED", "ODAQ")):
        original = next(c for c, r in zip(clips, answer) if r["dataset"] == dataset)
        identifier = hashlib.sha256(f"songbench-repeat-{index}".encode()).hexdigest()[:20]
        destination = output/"audio"/(identifier+".wav")
        shutil.copyfile(output/original["file"], destination)
        clips.append(original | {"id": identifier, "file": "audio/"+destination.name})
        repeats.append({"original": original["id"], "repeat": identifier})
    rng = np.random.default_rng(POLICY["seed"])
    rng.shuffle(clips)
    assets = {name: file_hash(root/"assets"/name) for name in (
        "songbench-model.py", "songbench-eval.py", "songbench.safetensors",
        "muq-config.json", "muq-model.safetensors", "med-data.json")}
    key = {"kind": "wcfi_songbench_external_answer_key", "rows": answer, "repeats": repeats,
           "selection": selection, "odaq_selection": read_json(root/"odaq-selection.json")}
    write_json(output/"answer-key.json", key)
    write_json(output/"protocol.json", POLICY | {"assets_sha256": assets,
        "implementation_sha256": file_hash(Path(__file__)),
        "cpu_implementation_sha256": file_hash(Path(__file__).with_name("songbench_cpu.py")),
        "planned_inputs": len(clips), "selection_uses_preference_values": False,
        "answer_key_sha256": file_hash(output/"answer-key.json")})
    write_json(output/"blind-manifest.json", {"kind": "wcfi_songbench_external_blind",
        "schema_version": 1, "protocol_sha256": file_hash(output/"protocol.json"), "clips": clips})
    return {"directory": str(output), "inputs": len(clips), "MED_mixes": len(selection["clips"]),
            "MED_songs": len({r["song"] for r in selection["clips"]}), "ODAQ_trials": 6,
            "protocol_sha256": file_hash(output/"protocol.json")}


def paired_ratings(a, b, dataset):
    """Use paired listeners; a mix engineer cannot supply the target for their own pair."""
    import numpy as np

    listeners = sorted(set(a["ratings"]) & set(b["ratings"]))
    if dataset == "MED":
        owners = {a["name"], b["name"]}
        listeners = [r for r in listeners if re.sub(r"_(?:\d{4}|unknown)$", "", r) not in owners]
    return np.array([a["ratings"][r]-b["ratings"][r] for r in listeners], dtype="float64")


def paired_interval(differences, *, seed=20261022):
    import numpy as np

    if len(differences) < POLICY["minimum_shared_raters"]:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(differences, size=(10000, len(differences))).mean(axis=1)
    return [float(v) for v in np.quantile(means, [.025, .975])]


def credit(predicted, human):
    if abs(predicted) <= POLICY["score_tie_tolerance"]:
        return .5
    return float((predicted > 0) == (human > 0))


def cluster_interval(values, *, seed=20261022):
    import numpy as np

    if not values:
        return None
    rng = np.random.default_rng(seed)
    bootstrap = rng.choice(values, size=(10000, len(values))).mean(axis=1)
    return [float(v) for v in np.quantile(bootstrap, [.025, .975])]


def evaluate_rows(rows, scores):
    """Frozen scoring; independent units are content groups, never generated pairs."""
    import numpy as np
    from scipy.stats import kendalltau, spearmanr

    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["dataset"], row["group"])].append(row)
    results, all_pairs = [], []
    for (dataset, group), items in sorted(grouped.items()):
        pairs = []
        for a, b in itertools.combinations(sorted(items, key=lambda r: r["id"]), 2):
            differences = paired_ratings(a, b, dataset)
            interval = paired_interval(differences)
            if interval is None:
                continue
            human = float(np.mean(differences))
            predicted = scores[a["id"]]["Mixing"]-scores[b["id"]]["Mixing"]
            decisive = bool(interval[0] > 0 or interval[1] < 0)
            pair = {"dataset": dataset, "group": group, "a": a["name"], "b": b["name"],
                    "shared_raters_excluding_authors": len(differences),
                    "human_mean_difference": human, "human_difference_95_ci": interval,
                    "human_fraction_prefer_a": float(np.mean(differences > 0)),
                    "human_fraction_tied": float(np.mean(differences == 0)),
                    "model_difference": predicted, "decisive": decisive,
                    "credit": credit(predicted, human),
                    "rms_baseline_credit": credit(a["source_rms_dbfs"]-b["source_rms_dbfs"], human)}
            pairs.append(pair)
        decisive = [p for p in pairs if p["decisive"]]
        processed = [p for p in decisive if dataset == "ODAQ"
                     and p["a"] not in {"reference", "LP35", "LP70"}
                     and p["b"] not in {"reference", "LP35", "LP70"}]
        means = [float(np.mean(list(r["ratings"].values()))) for r in items]
        predictions = [scores[r["id"]]["Mixing"] for r in items]
        selected = int(np.argmax(predictions))
        rho = float(spearmanr(means, predictions).statistic)
        tau = float(kendalltau(means, predictions).statistic)
        results.append({"dataset": dataset, "group": group, "n_versions": len(items),
            "decisive_pairs": len(decisive), "all_pairs": len(pairs),
            "accuracy": float(np.mean([p["credit"] for p in decisive])) if decisive else None,
            "all_pair_accuracy": float(np.mean([p["credit"] for p in pairs])) if pairs else None,
            "processed_only_decisive_pairs": len(processed),
            "processed_only_accuracy": float(np.mean([p["credit"] for p in processed])) if processed else None,
            "rms_baseline_accuracy": float(np.mean([p["rms_baseline_credit"] for p in decisive])) if decisive else None,
            "spearman": rho if math.isfinite(rho) else None,
            "kendall": tau if math.isfinite(tau) else None,
            "selected": items[selected]["name"],
            "top_mean_human_rating": max(means), "selected_mean_human_rating": means[selected],
            "human_rating_regret": max(means)-means[selected],
            "versions": [{"name": r["name"], "id": r["id"], "scores": scores[r["id"]],
                          "human_mean": m, "n_raters": len(r["ratings"]),
                          "source_rms_dbfs": r["source_rms_dbfs"]}
                         for r, m in zip(items, means)]})
        all_pairs.extend(pairs)
    datasets = {}
    for dataset in ("MED", "ODAQ"):
        groups = [g for g in results if g["dataset"] == dataset and g["accuracy"] is not None]
        accuracies = [g["accuracy"] for g in groups]
        interval = cluster_interval(accuracies)
        accuracy = float(np.mean(accuracies)) if accuracies else None
        count = sum(g["decisive_pairs"] for g in groups)
        gate = POLICY["screening_gate"]
        datasets[dataset] = {"groups_with_decisive_pairs": len(groups), "decisive_pairs": count,
            "macro_accuracy": accuracy, "content_cluster_95_ci": interval,
            "macro_rms_baseline_accuracy": float(np.mean([g["rms_baseline_accuracy"] for g in groups])) if groups else None,
            "screening_passed": bool(len(groups) >= gate["minimum_groups_per_dataset"]
                and count >= gate["minimum_decisive_pairs_per_dataset"]
                and accuracy >= gate[dataset+"_minimum_accuracy"] and interval[0] > .5)}
        if dataset == "ODAQ":
            processed_groups = [g for g in groups if g["processed_only_accuracy"] is not None]
            datasets[dataset]["processed_only"] = {
                "groups": len(processed_groups),
                "decisive_pairs": sum(g["processed_only_decisive_pairs"] for g in processed_groups),
                "macro_accuracy": float(np.mean([g["processed_only_accuracy"] for g in processed_groups])) if processed_groups else None,
                "content_cluster_95_ci": cluster_interval([g["processed_only_accuracy"] for g in processed_groups])}
    return {"datasets": datasets, "groups": results, "pairs": all_pairs}


def report(directory, run_directory, *, amendment=None, output=None):
    directory, run_directory = Path(directory).resolve(), Path(run_directory).resolve()
    destination = Path(output).resolve() if output is not None else run_directory
    manifest, policy = read_json(directory/"blind-manifest.json"), read_json(directory/"protocol.json")
    batch = read_json(run_directory/"batch-result.json")
    if (batch["manifest_sha256"] != file_hash(directory/"blind-manifest.json")
            or manifest["protocol_sha256"] != file_hash(directory/"protocol.json")
            or policy["answer_key_sha256"] != file_hash(directory/"answer-key.json")
            or policy["cpu_implementation_sha256"] != file_hash(Path(__file__).with_name("songbench_cpu.py"))):
        raise ValueError("Frozen external benchmark changed")
    amendment_record = None
    if amendment is not None:
        amendment_record = read_json(amendment)
        if (destination == run_directory or destination.exists()
                or amendment_record.get("kind") != "wcfi_songbench_author_id_correction"
                or amendment_record.get("original_protocol_sha256") != file_hash(directory/"protocol.json")
                or amendment_record.get("original_scorer_sha256") != policy["implementation_sha256"]
                or amendment_record.get("corrected_scorer_sha256") != file_hash(Path(__file__))
                or any(policy.get(k) != v for k, v in POLICY.items())):
            raise ValueError("Invalid grading amendment or changed scoring policy")
        destination.mkdir(parents=True)
    elif policy["implementation_sha256"] != file_hash(Path(__file__)):
        raise ValueError("Frozen scorer changed; an explicit recorded correction is required")
    key, scores = read_json(directory/"answer-key.json"), {}
    clips = {c["id"]: c for c in manifest["clips"]}
    for record in batch["predictions"]:
        path = run_directory/record["file"]
        if file_hash(path) != record["sha256"]:
            raise ValueError("Prediction changed")
        prediction = read_json(path)
        identifier = prediction["id"]
        values = prediction["scores"]
        if (identifier not in clips or identifier in scores
                or prediction["input_sha256"] != clips[identifier]["sha256"]
                or set(values) != set(AXES)
                or not all(isinstance(v, (int, float)) and math.isfinite(v) and 1 <= v <= 10
                           for v in values.values())
                or prediction["gpu_initialized"]):
            raise ValueError("Invalid or mismatched CPU prediction")
        scores[prediction["id"]] = prediction["scores"]
    if len(scores) != len(manifest["clips"]):
        raise ValueError("Predictions missing or duplicated")
    repeats = [{**p, "maximum_axis_difference": max(abs(scores[p["original"]][a]-scores[p["repeat"]][a]) for a in AXES)}
               for p in key["repeats"]]
    result = evaluate_rows(key["rows"], scores)
    null_ok = all(r["maximum_axis_difference"] <= POLICY["score_tie_tolerance"] for r in repeats)
    result.update({"kind": "wcfi_songbench_external_results", "primary_axis": "Mixing",
        "screening_passed": null_ok and all(d["screening_passed"] for d in result["datasets"].values()),
        "repeat_checks": repeats, "all_repeats_passed": null_ok,
        "professional_church_mixing_validated": False, "processing_applied": False,
        "runtime": batch["runtime"], "timing": batch["timing"],
        "limitations": ["Nine MED songs and six ODAQ trials; external screening, not entire datasets",
            "MED published browser audio is MP3; human ratings concern original mix evaluation",
            "Published levels retained to avoid silently changing human-rated conditions; RMS baseline reported",
            "Human confidence uses pairwise 95% intervals without multiple-comparison correction",
            "SongBench's released loader removes stereo side information",
            "ODAQ technical fidelity labels are distinct from MED musical preference",
            "Unknown overlap with foundation-model pretraining; no benchmark training or tuning",
            "A pass cannot establish transfer to live worship or small EQ/compressor edits"]})
    if amendment_record is not None:
        result["grading_amendment"] = amendment_record
    write_json(destination/"results.json", result)
    table = "".join(f"<tr><td>{html.escape(g['dataset'])}</td><td>{html.escape(g['group'])}</td>"
                    f"<td>{g['decisive_pairs']}</td><td>{g['accuracy']}</td>"
                    f"<td>{g['spearman']}</td><td>{html.escape(g['selected'])}</td></tr>"
                    for g in result["groups"])
    document = ("<!doctype html><meta charset='utf-8'><title>SongBench external CPU benchmark</title>"
        "<style>body{font:17px system-ui;max-width:1100px;margin:40px auto;padding:20px;background:#111;color:#eee}"
        "table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #555;text-align:left}"
        "pre{white-space:pre-wrap}</style><h1>SongBench against human judgments</h1>"
        "<p>CPU only. No playback, uploads, audio-device access or Ableton operations.</p>"
        f"<p><b>External screening gate: {'PASS' if result['screening_passed'] else 'FAIL'}</b>. "
        "Professional church mixing remains unvalidated.</p><pre>"
        +html.escape(json.dumps(result["datasets"], indent=2))+"</pre>"
        "<table><tr><th>Dataset</th><th>Content</th><th>Decisive pairs</th><th>Accuracy</th>"
        "<th>Rank correlation</th><th>Model selection</th></tr>"+table+"</table><ul>"
        +"".join("<li>"+html.escape(x)+"</li>" for x in result["limitations"])+"</ul>")
    if amendment_record is not None:
        document += "<h2>Recorded grading correction</h2><pre>"+html.escape(json.dumps(amendment_record, indent=2))+"</pre>"
    (destination/"report.html").write_text(document, encoding="utf-8")
    return {k: v for k, v in result.items() if k not in ("groups", "pairs")}
