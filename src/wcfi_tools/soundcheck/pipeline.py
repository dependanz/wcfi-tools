"""Rehearsal file binding, calibration plans, and a volunteer-facing report."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

from .ableton import inspect_set
from .analysis import analyze_audio
from .profiles import file_hash, load_profile, write_json


def find_recording(folder: Path, name: str, recorded_on: date, latest: bool) -> Path:
    """Only match exact track names or Ableton's numbered recording filenames.

    Never silently mix historical services or guess between multiple takes.
    """
    pattern = re.compile(r"^" + re.escape(name) + r"(?:$|[ _-]+\d+(?:\s|$|\[))", re.IGNORECASE)
    matches = [p for p in folder.iterdir() if p.is_file()
               and p.suffix.lower() in {".wav", ".aif", ".aiff", ".flac"}
               and pattern.match(p.stem)
               and datetime.fromtimestamp(p.stat().st_mtime, UTC).astimezone().date() == recorded_on]
    if not matches:
        raise ValueError(f"No recording named {name!r} from {recorded_on}; set its file in the profile")
    if len(matches) != 1 and not latest:
        raise ValueError(f"{len(matches)} takes for {name}; choose file in profile or use --latest")
    return max(matches, key=lambda p: (p.stat().st_mtime_ns, p.name))


def calibrate(profile_path: Path, output_dir: Path, *, recordings: Path | None = None,
              recorded_on: date | None = None, latest: bool = False,
              seconds: float = 90, start: float = 0) -> dict:
    profile_path = profile_path.resolve()
    profile = load_profile(profile_path)
    source = Path(profile["source_set"])
    if not source.is_absolute():
        source = profile_path.parent / source
    source = source.resolve()
    source_sha = file_hash(source)
    inventory = {t["track_id"]: t for t in inspect_set(source)["tracks"]}
    rows = []
    used_files = set()
    for config in profile["tracks"]:
        row = {"track_id": config["track_id"], "name": config["name"], "role": config["role"],
               "input_target": config.get("input_target"), "status": "skipped",
               "reasons": [], "notes": [], "gain_db": None, "eq": []}
        rows.append(row)
        if not config["enabled"]:
            row["reasons"].append("Disabled in profile")
            continue
        try:
            track = inventory.get(int(config["track_id"]))
            if track is None or track["name"] != config["name"]:
                raise ValueError("Track identity changed; regenerate profile")
            if track["input_target"] != config.get("input_target"):
                raise ValueError("Track input routing changed; regenerate profile")
            if config.get("file"):
                audio = Path(config["file"])
                if not audio.is_absolute():
                    audio = profile_path.parent / audio
            elif recordings:
                day = recorded_on or datetime.now(UTC).astimezone().date()
                audio = find_recording(recordings, config["name"], day, latest)
            else:
                raise ValueError("Set file in profile or supply --recordings")
            audio = audio.resolve()
            if audio in used_files:
                raise ValueError("This recording is already assigned to another track")
            used_files.add(audio)
            measurement = analyze_audio(audio, config["role"], seconds=seconds, start=start,
                                        max_boost_db=profile["max_boost_db"],
                                        max_cut_db=profile["max_cut_db"],
                                        peak_ceiling_dbfs=profile["peak_ceiling_dbfs"],
                                        eq_mode=profile["eq_mode"])
            row.update(measurement)
            expected_channels = 2 if "/S" in track["input_target"] else 1
            if row["channels"] != expected_channels:
                row.update(status="blocked", gain_db=None, eq=[])
                row["reasons"].append("Recording channel count does not match this track's input routing")
        except (OSError, ValueError, RuntimeError) as exc:
            row["status"] = "blocked"
            row["reasons"].append(str(exc))
    if file_hash(source) != source_sha:
        raise ValueError("Live set changed during analysis; rerun after saving")
    plan = {"schema_version": 1, "kind": "wcfi_soundcheck_plan",
            "created_at": datetime.now(UTC).isoformat(),
            "source_set": str(source), "source_sha256": source_sha,
            "profile": str(profile_path), "profile_sha256": file_hash(profile_path),
            "capture_kind": "raw_pre_fx", "eq_mode": profile["eq_mode"],
            "routing_note": profile.get("routing_note", ""), "tracks": rows}
    # A run directory is immutable; choosing a new name avoids stale-cache mistakes.
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "plan.json", plan)
    (output_dir / "report.html").write_text(render_report(plan), encoding="utf-8")
    return plan


def render_report(plan: dict) -> str:
    esc = lambda value: html.escape(str(value))
    rows = []
    ready = sum(t["status"] == "ready" for t in plan["tracks"])
    for track in plan["tracks"]:
        filters = ", ".join(f"{b['frequency_hz']:g} Hz low cut" if b["kind"] == "highpass"
                            else f"{b['frequency_hz']:g} Hz: {b['gain_db']:+g} dB"
                            for b in track["eq"]) or "No EQ change"
        gain = "—" if track["gain_db"] is None else f"{track['gain_db']:+.2f} dB"
        messages = track["reasons"] + track["notes"]
        detail = "".join(f"<li>{esc(m)}</li>" for m in messages)
        metrics = track.get("metrics", {})
        measured = f"Active RMS: {metrics.get('active_rms_dbfs', '—')} dBFS · " \
                   f"Sample peak: {metrics.get('sample_peak_dbfs', '—')} dBFS"
        filename = Path(track["file"]).name if track.get("file") else "No recording"
        rows.append(f'<tr><td><strong>{esc(track["name"])}</strong><small>{esc(track["role"])} · '
                    f'{esc(filename)}</small></td><td class="{esc(track["status"])}">'
                    f'{esc(track["status"].title())}</td><td>{gain}</td><td>{esc(filters)}</td>'
                    f'<td>{esc(measured)}<ul>{detail}</ul></td></tr>')
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WCFI · Morning soundcheck</title><style>
body{font:16px/1.5 system-ui,sans-serif;color:#173132;background:#f3f6f3;margin:0;padding:3vw}
main{max-width:1280px;margin:auto}h1{font-size:2.5rem;margin:.2em 0}p{max-width:85ch}
.eyebrow{letter-spacing:.15em;text-transform:uppercase;color:#41786e}
.callout{background:#e1ece6;padding:18px 24px;border-left:4px solid #387361;margin:24px 0}
.table{overflow:auto}table{border-collapse:collapse;background:white;width:100%}th,td{padding:16px;
text-align:left;border-bottom:1px solid #dae1da;vertical-align:top}th{background:#173132;color:white}
small{display:block;color:#61726c;font-size:.8em;overflow-wrap:anywhere}ul{padding-left:20px;
font-size:.85em}.ready{color:#216545}.blocked{color:#9c3a22}.skipped{color:#64746d}
code{background:#e1ece6;padding:.2em .4em}footer{font-size:.85em;margin-top:28px;color:#61726c}
</style><main><div class="eyebrow">WCFI · Rehearsal calibration</div><h1>Morning soundcheck</h1>
""" + f"<p>{ready} channels passed the technical checks. Review the performers and settings " \
        "before loading the calibrated copy.</p>" \
        f"<div class='callout'>{esc(plan.get('routing_note', ''))}</div>" \
        "<p><strong>This is input calibration.</strong> It does not certify a professional mix. " \
        "Full-band bleed can look like the intended performer. Confirm that each enabled singer " \
        "or player actually performed. Existing effects, group faders, Master processing and " \
        "the TouchMix/ATEM still affect the broadcast.</p>" \
        "<div class='table'><table><thead><tr><th>Input</th><th>Status</th><th>Absolute input gain</th>" \
        "<th>Soundcheck EQ</th><th>Evidence / action</th></tr></thead><tbody>" + "".join(rows) + \
        "</tbody></table></div><h2>Next steps</h2><ol>" \
        "<li>Fix blocked inputs at their source, or leave them unchanged for this pass.</li>" \
        "<li>Use <code>wcfi soundcheck apply plan.json --output &lt;new-set.als&gt;</code>. " \
        "Only ready channels are changed. The copy must be beside the original set.</li>" \
        "<li>Save your work in Ableton, then load and audition the copy during rehearsal. " \
        "Check Master, Aux 3/4 and ATEM levels with the whole band.</li>" \
        "<li>Restore the original set if the result is unsuitable.</li></ol>" \
        f"<footer>Source: {esc(plan['source_set'])}<br>Created: {esc(plan['created_at'])}" \
        "<br>EQ uses restrained source-role heuristics, not an engineer-approved reference. " \
        "Peak estimates apply only to input gain, not to the full effect chain.</footer></main></html>"


def read_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
