"""Controlled source comparisons, with human judgments kept separate from model ratings.

This makes an explicitly declared stem test mix. It does not reconstruct downstream
nonlinear group/Master processing, subtract sources from a Master, or apply settings.
"""
from __future__ import annotations

import html
import json
import math
import os
import random
import subprocess
from pathlib import Path

from .mix import parse_measurements
from .profiles import file_hash, number, write_json


def peak_eq(audio, rate: int, frequency: float, gain_db: float, q: float = .8):
    """RBJ peaking biquad, with unchanged stereo routing and no level normalization."""
    import numpy as np
    from scipy.signal import lfilter

    a = 10 ** (gain_db / 40)
    w = 2 * math.pi * frequency / rate
    alpha = math.sin(w) / (2 * q)
    b = np.array([1 + alpha * a, -2 * math.cos(w), 1 - alpha * a])
    denominator = np.array([1 + alpha / a, -2 * math.cos(w), 1 - alpha / a])
    return lfilter(b / denominator[0], denominator / denominator[0], audio, axis=0)


def hf_event_boost(lead, rate: int, gain_db: float = 12):
    """Boost 4.5–7.5 kHz during HF-rich events; these are NOT verified S/SH phonemes."""
    import numpy as np
    from scipy.signal import butter, sosfiltfilt

    high = sosfiltfilt(butter(3, [4500, 7500], fs=rate, btype="band", output="sos"),
                      lead, axis=0)
    middle = sosfiltfilt(butter(3, [300, 3000], fs=rate, btype="band", output="sos"),
                        lead, axis=0)
    frame = round(.02 * rate)
    count = len(lead) // frame
    hp = np.mean(high[:count * frame].reshape(count, frame, -1) ** 2, axis=(1, 2))
    mp = np.mean(middle[:count * frame].reshape(count, frame, -1) ** 2, axis=(1, 2))
    rms = np.mean(lead[:count * frame].reshape(count, frame, -1) ** 2, axis=(1, 2))
    active = rms > max(1e-10, float(np.percentile(rms, 90)) / 100)
    ratio = 10 * np.log10(np.maximum(hp, 1e-20) / np.maximum(mp, 1e-20))
    if not np.any(active) or np.max(hp) < 1e-12:
        raise ValueError("No usable HF-rich lead events; choose a different passage")
    threshold = float(np.percentile(ratio[active], 80))
    selected = active & (ratio >= threshold)
    # Short ramps prevent hard gate clicks; both channels use the same gain envelope.
    centers = (np.arange(count) + .5) * frame
    gate = np.interp(np.arange(len(lead)), centers, selected.astype(float))
    changed = lead + (10 ** (gain_db / 20) - 1) * gate[:, None] * high
    return changed, {"band_hz": [4500, 7500], "added_band_gain_db": gain_db,
                     "frame_seconds": .02, "selected_frame_fraction": float(selected.mean()),
                     "ratio_threshold_db": threshold,
                     "event_starts_seconds": (np.flatnonzero(selected) * .02).tolist(),
                     "phonemes_human_verified": False}


def source_variants(lead, backing, rate: int):
    import numpy as np

    if lead.shape != backing.shape or lead.ndim != 2 or lead.shape[1] != 2:
        raise ValueError("Use aligned stereo lead and backing arrays")
    if not np.isfinite(lead).all() or not np.isfinite(backing).all():
        raise ValueError("Non-finite source samples")
    if np.mean(lead ** 2) < 1e-10 or np.mean(backing ** 2) < 1e-10:
        raise ValueError("Both lead and backing must contain usable audio")
    boosted, events = hf_event_boost(lead, rate)
    return [
        ("control", backing + lead, {"lead_gain_db": 0}),
        ("lead_minus_6", backing + lead * 10 ** (-6 / 20), {"lead_gain_db": -6}),
        ("lead_minus_12", backing + lead * 10 ** (-12 / 20), {"lead_gain_db": -12}),
        ("vocal_300hz_boost", backing + peak_eq(lead, rate, 300, 9),
         {"kind": "lead_peaking_eq", "frequency_hz": 300, "gain_db": 9, "q": .8}),
        ("vocal_hf_events_boost", backing + boosted, events),
        ("sham_control", (backing + lead).copy(), {"lead_gain_db": 0}),
    ]


def _measure(path: Path, log: Path, ffmpeg: Path):
    command = [str(ffmpeg), "-nostdin", "-hide_banner", "-nostats", "-threads", "1",
               "-filter_threads", "1", "-i", str(path), "-map", "0:a:0", "-vn",
               "-af", "ebur128=peak=sample+true", "-f", "null", "-"]
    options = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS} if os.name == "nt" else {}
    run = subprocess.run(command, capture_output=True, text=True, check=False,
                         timeout=120, **options)
    if run.returncode:
        raise RuntimeError("FFmpeg measurement failed")
    log.write_text(run.stderr, encoding="utf-8")
    result = parse_measurements(run.stderr)
    if result["integrated_lufs"] is None or result["true_peak_dbtp"] is None:
        raise ValueError("Unmeasurable programme; choose active excerpts")
    return result


def _read_excerpt(path: Path, start: float, seconds: float):
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(path) as stream:
        offset, frames = round(start * stream.samplerate), round(seconds * stream.samplerate)
        if offset + frames > len(stream):
            raise ValueError("Excerpt extends beyond an aligned stem")
        stream.seek(offset)
        audio = stream.read(frames, dtype="float64", always_2d=True)
    if not np.isfinite(audio).all():
        raise ValueError("Non-finite source samples")
    return audio


def prepare_comparisons(profile: Path, output: Path, *, ffmpeg: Path):
    """Prepare neutral clips and a blind human review; never load a model or audio device."""
    import numpy as np
    import soundfile as sf

    from .local_listening import prepare_model_audio

    profile, output, ffmpeg = profile.resolve(), output.resolve(), ffmpeg.resolve()
    if output.exists():
        raise ValueError("Choose a new output directory; previous evidence is never overwritten")
    if not ffmpeg.is_file():
        raise ValueError("FFmpeg executable missing")
    config = json.loads(profile.read_text(encoding="utf-8"))
    if config.get("kind") != "wcfi_stem_validation" or not config.get("mix_basis"):
        raise ValueError("Declare the stem test mix and its capture/processing basis")
    seconds = number(config.get("seconds", 15), "seconds", 10, 20)
    starts = [number(s, "start", 0, 86400) for s in config["starts"]]
    if len(starts) != 2 or starts[0] == starts[1]:
        raise ValueError("Choose two different source-relative sections")
    declared = [config["lead"], *config["backing"]]
    paths = [(profile.parent / item["file"]).resolve() for item in declared]
    if len(paths) < 2 or len(set(paths)) != len(paths):
        raise ValueError("Use distinct lead and backing stems; never include a source twice")
    info = [sf.info(p) for p in paths]
    if len({(i.samplerate, i.frames, i.channels) for i in info}) != 1:
        raise ValueError("All stems must share rate, frame count and stereo channel count")
    rate = info[0].samplerate
    if info[0].channels != 2 or not 24000 <= rate <= 48000:
        raise ValueError("Use aligned stereo stems at 24–48 kHz")
    stats = [(p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
    inputs = [{"role": d["role"], "file": str(p), "sha256": file_hash(p)}
              for d, p in zip(declared, paths, strict=True)]
    candidates = []
    for section, start in enumerate(starts):
        lead = _read_excerpt(paths[0], start, seconds)
        backing = sum((_read_excerpt(p, start, seconds) for p in paths[1:]),
                      np.zeros_like(lead))
        for condition, audio, facts in source_variants(lead, backing, rate):
            candidates.append({"section": section, "start": start, "condition": condition,
                               "audio": audio, "facts": facts})
    if stats != [(p.stat().st_size, p.stat().st_mtime_ns) for p in paths]:
        raise ValueError("A source changed; wait until all exports finish")
    random.Random(config.get("seed", 20261017)).shuffle(candidates)
    output.mkdir(parents=True)
    (output / "clips").mkdir()
    (output / "model-inputs").mkdir()
    private = output / "private-measurements"
    private.mkdir()
    measured = []
    for index, candidate in enumerate(candidates, 1):
        p = private / f"clip-{index:02d}-before.wav"
        sf.write(p, candidate["audio"], rate, subtype="FLOAT")
        measured.append(_measure(p, p.with_suffix(".log"), ffmpeg))
    target = min(-18., *(m["integrated_lufs"] - m["true_peak_dbtp"] - 3 for m in measured))
    public, truth = [], []
    for index, (candidate, before) in enumerate(zip(candidates, measured, strict=True), 1):
        identifier = f"clip-{index:02d}"
        gain = target - before["integrated_lufs"]
        samples = candidate["audio"] * 10 ** (gain / 20)
        if np.max(np.abs(samples)) >= .999:
            raise ValueError("Comparison lacks PCM16 headroom")
        p = output / "clips" / f"{identifier}.wav"
        sf.write(p, samples, rate, subtype="PCM_16")
        actual = _measure(p, private / f"{identifier}-matched.log", ffmpeg)
        if abs(actual["integrated_lufs"] - target) > .21 or actual["true_peak_dbtp"] > -2.7:
            raise ValueError("Loudness matching or headroom check failed")
        conversion = prepare_model_audio(p, output / "model-inputs" / f"{identifier}.wav")
        public.append({"id": identifier, "file": f"clips/{identifier}.wav",
                       "sha256": file_hash(p), "duration_seconds": seconds,
                       "expected_model_sha256": conversion["sha256"]})
        truth.append({"id": identifier, "section": candidate["section"],
                      "start_seconds": candidate["start"], "condition": candidate["condition"],
                      "transformation": candidate["facts"], "match_gain_db": gain,
                      "before_matching": before, "after_matching": actual,
                      "model_audio": conversion})
    if stats != [(p.stat().st_size, p.stat().st_mtime_ns) for p in paths]:
        raise ValueError("A source changed during preparation; do not use this trial")
    write_json(output / "blind-manifest.json", {"schema_version": 1, "clips": public,
               "status": "prepared_no_upload", "audio_device_access": False,
               "ableton_access": False})
    limits = [
        "Transformations are known; which mix sounds better requires human judgments.",
        "This declared stem test mix omits downstream processing identified in mix_basis; it is not a recreated final Master.",
        "HF-rich events may be consonants, breaths or bleed. Sibilance phonemes are not yet verified.",
        "The local model receives mono 16 kHz. Verify that each intervention remains audible in that exact input.",
        "No automatic EQ, de-essing, gain settings or live changes are authorized by a quality score.",
    ]
    write_json(output / "private-answer-key.json", {"profile_sha256": file_hash(profile),
               "inputs": inputs, "mix_basis": config["mix_basis"],
               "target_integrated_lufs": target, "clips": truth, "limitations": limits,
               "human_validation_status": "pending"})
    _human_review(output, truth, config["mix_basis"])
    return {"clips": len(public), "target_lufs": target, "human_validation": "pending",
            "report": str(output / "human-review.html")}


def _human_review(output: Path, truth: list, basis: str):
    prompts = {
        "lead_minus_12": "Which version makes the lead vocal easier to follow?",
        "vocal_300hz_boost": "Which version has the more natural vocal tone?",
        "vocal_hf_events_boost": "Which version has sharper, harsher S/SH sounds? "
                                 "Choose the harsher one; note separately which you prefer.",
        "sham_control": "Do these versions sound different? Choose Same if they do not.",
    }
    rng = random.Random(919171)
    pairs, hidden = [], []
    for section in (0, 1):
        lookup = {r["condition"]: r for r in truth if r["section"] == section}
        for condition, prompt in prompts.items():
            sides = [lookup["control"]["id"], lookup[condition]["id"]]
            rng.shuffle(sides)
            hidden.append({"id": f"pair-{len(pairs)+1:02d}", "section": section,
                           "condition": condition, "A": sides[0], "B": sides[1]})
            pair = hidden[-1]
            players = "".join(f'<label>{side}<audio controls preload="none" '
                              f'src="model-inputs/{identifier}.wav"></audio></label>'
                              for side, identifier in zip(("A", "B"), sides, strict=True))
            pairs.append(f'<fieldset><legend>{pair["id"]}: {html.escape(prompt)}</legend>'
                         f'{players}<select name="{pair["id"]}" required>'
                         '<option value="">Choose…</option><option>A</option><option>B</option>'
                         '<option>Same</option><option>Cannot assess / no clear vocal or sibilants</option>'
                         '</select><input name="' + pair["id"] + '-note" placeholder="Optional note">'
                         '</fieldset>')
    manifest_hash = file_hash(output / "blind-manifest.json")
    write_json(output / "private-human-pairs.json", {
        "question_version": 2, "trial_manifest_sha256": manifest_hash, "pairs": hidden,
    })
    document = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>WCFI blind source comparisons</title><style>body{font:17px system-ui;"
        "max-width:950px;margin:32px auto;padding:0 20px;background:#101923;color:#e4ecf3}"
        "fieldset{margin:24px 0;padding:18px}label{display:flex;align-items:center;gap:14px;"
        "margin:12px 0}select,input,button{font:inherit;padding:8px;margin:8px}audio{width:85%}"
        "a{color:#8ed7ff}</style><h1>Blind source comparisons</h1>"
        "<p>Listen before reading model ratings or the private answer key. Use the same "
        "comfortable listening volume for every clip. Nothing plays automatically.</p>"
        "<p>Players use the exact mono 16 kHz input prepared for the local model, so human "
        "and model judgments can be compared. Original stereo copies are retained separately. "
        "These are controlled test mixes from processed stems. " + html.escape(basis) + "</p>"
        "<p>Judge only what you hear. ‘Cannot assess’ is useful evidence. In the sibilance pairs, "
        "identify whether you actually hear S/SH sounds; brightness alone is insufficient.</p>"
        "<form id='review'>" + "".join(pairs) +
        "<button>Save my judgments as JSON</button></form><p>Answers stay in this browser "
        "until you save them. There is no upload or server.</p>"
        "<script>document.querySelectorAll('audio').forEach(a=>a.addEventListener('play',()=>{"
        "document.querySelectorAll('audio').forEach(b=>{if(a!==b)b.pause()})}));"
        "document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();"
        "const result={kind:'wcfi_human_source_judgments',audio_domain:'mono_16k_pcm16',trial:" +
        json.dumps(output.name).replace("<", "\\u003c") + ","
        "question_version:2,trial_manifest_sha256:" + json.dumps(manifest_hash) + ","
        "answers:Object.fromEntries(new FormData(e.target)),created_at:new Date().toISOString()};"
        "const a=document.createElement('a');a.href=URL.createObjectURL(new Blob("
        "[JSON.stringify(result,null,2)],{type:'application/json'}));"
        "a.download='wcfi-human-judgments.json';a.click();URL.revokeObjectURL(a.href)});"
        "</script></html>")
    (output / "human-review.html").write_text(document, encoding="utf-8")
