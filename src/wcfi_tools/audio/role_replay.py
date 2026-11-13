"""Read-only recorded replay, reference previews and inspectable local reports."""
from __future__ import annotations

import hashlib
import html
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from ..soundcheck.profiles import file_hash, number, write_json
from .role_analysis import analyze_pair, observe, render_proposals
from .roles import get_profile
from .takes import _manifest


def read_excerpt(path, *, start=0, seconds=20):
    number(start, "start", 0, 86400)
    number(seconds, "seconds", .5, 30)
    path = Path(path).resolve(strict=True)
    before = path.stat()
    with sf.SoundFile(path) as stream:
        rate = stream.samplerate
        if not 32000 <= rate <= 96000 or stream.channels not in (1, 2):
            raise ValueError("Use mono/stereo 32–96 kHz recordings")
        first = round(start * rate)
        count = round(seconds * rate)
        if first + count > len(stream):
            raise ValueError(f"Requested window extends beyond {path.name}")
        stream.seek(first)
        audio = stream.read(count, dtype="float32", always_2d=True)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source is changing; use a finished recording")
    return audio, rate, {"path": str(path), "start_frame": first, "frames": count,
                        "sample_rate": rate, "window_float32_sha256": hashlib.sha256(audio.tobytes()).hexdigest()}


def take_windows(take, *, start=29, seconds=20):
    take = Path(take).resolve(strict=True)
    folder, manifest = _manifest(take.parent, take.name)
    result = {}
    for name in ("RED", "KIT"):
        matches = [s for s in manifest["sources"] if s["track_name"].casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Take needs one unambiguous {name} source")
        source = matches[0]
        path = folder / source["file"]
        stamp = path.stat()
        if file_hash(path).lower() != source["sha256"].lower():
            raise ValueError(f"{name} source checksum does not match its manifest")
        audio, rate, provenance = read_excerpt(path, start=start, seconds=seconds)
        final = path.stat()
        if (stamp.st_size, stamp.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise ValueError("Source changed between checksum verification and reading")
        if rate != manifest["sample_rate"] or audio.shape[1] != source["channels"]:
            raise ValueError("Source header does not match take manifest")
        result[name] = (audio, rate, provenance | {"source_sha256": source["sha256"],
                       "track_id": source["track_id"], "take_id": manifest["take_id"]})
    return result


def save_observation(output, name, before, after, rate, profile, *, provenance,
                     chain_state=None, aligned=False, context=None, previews=False,
                     withhold_hf_reason=None):
    report = analyze_pair(before, after, rate, profile, chain_state=chain_state,
                          aligned=aligned, context=context)
    report["provenance"] = provenance
    if withhold_hf_reason:
        report["proposals"] = [p for p in report["proposals"] if p["kind"] != "hf_burst_reduction"]
        candidates = report["after"]["analyses"].get("sibilance_candidates")
        if candidates is not None:
            candidates["treatment_withheld_reason"] = withhold_hf_reason
            report["after"]["findings"].append({"kind": "bleed_context_unavailable",
                "stage": "post_fx", "note": withhold_hf_reason})
    report["audio"] = {}
    if previews:
        for stage, audio in (("before", before), ("after", after)):
            filename = name + "-" + stage + ".wav"
            sf.write(output / filename, audio, rate, subtype="FLOAT")
            report["audio"][stage] = filename
        candidate, applied = render_proposals(after, rate, report["proposals"],
                                               include_experimental=True)
        filename = name + "-candidate.wav"
        sf.write(output / filename, candidate, rate, subtype="FLOAT")
        report["audio"]["candidate"] = filename
        report["candidate"] = {"processor": "local_reference_dsp_not_ableton",
            "applied": applied, "observation": observe(candidate, rate, profile, stage="preview"),
            "length_preserved": len(candidate) == len(after),
            "channels_preserved": candidate.shape[1] == after.shape[1],
            "audible_improvement_validated": False}
        # Separate RMS-matched copies for timbre listening. Actual levels above remain intact.
        levels = [float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) for a in (after, candidate)]
        matched = []
        for audio, rms in zip((after, candidate), levels, strict=True):
            matched.append(audio * (10 ** (-24 / 20) / max(rms, 1e-12)))
        shared_gain = min(1.0, 10 ** (-6 / 20) / max(1e-12, *(float(np.max(np.abs(a))) for a in matched)))
        for side, audio in zip(("reference_matched", "candidate_matched"), matched, strict=True):
            filename = name + "-" + side + ".wav"
            sf.write(output / filename, audio * shared_gain, rate, subtype="FLOAT")
            report["audio"][side] = filename
        report["matching"] = {"method": "global unweighted RMS, common peak safety gain",
                              "lufs_or_perceptual_loudness_matching": False,
                              "purpose": "Timbre audition only; actual gain-staging files are separate"}
    write_json(output / (name + ".json"), report)
    return report


def render_report(output, reports, *, title="Role-aware rehearsal analysis"):
    esc = html.escape
    panels = []
    for name, report in reports.items():
        before, after = report["before"]["metrics"], report["after"]["metrics"]
        text = {"before": report["before"], "after": report["after"],
                "paired_change": report["paired_change"], "proposals": report["proposals"]}
        panel = '<section><h2>' + esc(name) + ' · ' + esc(report["profile"]["role"]) + '</h2>'
        panel += (f'<p>Before sample peak: {before["sample_peak_dbfs"]:.2f} dBFS · '
                  f'After sample peak: {after["sample_peak_dbfs"]:.2f} dBFS</p>')
        panel += '<p>Enabled analyses: ' + esc(', '.join(report["profile"]["analyses"])) + '</p>'
        panel += '<pre>' + esc(json.dumps(text, indent=2)) + '</pre>'
        for label, path in report["audio"].items():
            panel += '<p>' + esc(label.replace('_', ' ')) + '</p><audio controls preload="none" src="' + esc(path, quote=True) + '"></audio>'
        panel += '<p><a href="' + esc(name + '.json', quote=True) + '">Full measurements and provenance</a></p></section>'
        panels.append(panel)
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(title) + '</title><style>body{font:16px/1.6 system-ui;max-width:1050px;margin:40px auto;padding:0 24px;background:#14232b;color:#edf4f4}a{color:#9ce2d2}section{border-top:1px solid #62757d;margin:28px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#20343e;padding:16px;font:13px/1.5 monospace}audio{max-width:100%}</style><h1>' + esc(title) + '</h1><p>Experimental local analysis and processing proposals. No audio-language model, GPU, cloud uploads, audio-device access or Ableton writes. No playback starts automatically.</p><p>Headroom is an engineering constraint; HF candidates are not confirmed sibilance. Preview DSP is not Pro-Q/Pro-DS/Ableton emulation. No automatic EQ, vocal boosting or drum compression is selected. Musical improvement requires listening in context.</p><p>Matched files use unweighted RMS, not LUFS or guaranteed equal perceived loudness. Original-level copies are retained for gain checks.</p>' + ''.join(panels) + '</html>'
    (output / "report.html").write_text(page, encoding="utf-8")


def analyze_take(take, output, *, start=29, seconds=20, previews=False):
    if output.exists():
        raise ValueError("Choose a new output directory; previous results are immutable")
    windows = take_windows(take, start=start, seconds=seconds)
    output.mkdir(parents=True, exist_ok=False)
    reports = {}
    started = time.perf_counter()
    for name, (audio, rate, provenance) in windows.items():
        context = windows["KIT"][0] if name == "RED" else None
        reports[name] = save_observation(output, name, audio, audio, rate, get_profile(name),
            provenance=provenance | {"post_fx_kind": "bypass_reference_not_current_ableton_chain"},
            chain_state={"kind": "local_bypass_reference", "devices": []}, aligned=True,
            context=context, previews=previews)
    summary = {"kind": "wcfi_role_take_analysis", "source_take": str(Path(take).resolve()),
               "start_seconds": start, "seconds": seconds, "tracks": list(reports),
               "elapsed_seconds": time.perf_counter() - started,
               "daw_writes": 0, "audio_uploads": 0, "gpu_used": False,
               "report": str((output / "report.html").resolve())}
    write_json(output / "summary.json", summary)
    render_report(output, reports)
    return summary


def analyze_files(before, output, profile, *, after=None, start=0, seconds=20,
                  aligned=False, chain_state=None, previews=False):
    if output.exists():
        raise ValueError("Choose a new output directory; previous results are immutable")
    a, rate, provenance = read_excerpt(before, start=start, seconds=seconds)
    if after:
        b, rate_b, provenance_b = read_excerpt(after, start=start, seconds=seconds)
        if rate_b != rate:
            raise ValueError("Paired recordings must have the same sample rate")
        provenance["after"] = provenance_b
        provenance["post_fx_kind"] = "supplied_capture"
    else:
        b = a
        aligned = True
        provenance["post_fx_kind"] = "bypass_reference_not_current_ableton_chain"
    output.mkdir(parents=True, exist_ok=False)
    name = get_profile(profile).name.upper()
    report = save_observation(output, name, a, b, rate, get_profile(profile),
        provenance=provenance, chain_state=chain_state, aligned=aligned, previews=previews)
    render_report(output, {name: report})
    return {"report": str((output / "report.html").resolve()), "profile": name,
            "proposals": len(report["proposals"]), "daw_writes": 0}
