"""Read-only EBU R128 measurements of an Ableton-rendered mix, not raw inputs."""

from __future__ import annotations

import html
import math
import re
import shutil
import subprocess
from pathlib import Path
from statistics import median

from .profiles import file_hash, write_json

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|inf)"
FRAME = re.compile(rf"t:\s*([\d.]+).*?M:\s*({NUMBER})\s+S:\s*({NUMBER})")


def parse_measurements(log: str) -> dict:
    """Parse the final scanner summary; startup summaries are not measurements."""
    if "Summary:" not in log:
        raise ValueError("FFmpeg did not return an EBU R128 summary")
    summary = log.rsplit("Summary:", 1)[1]

    def metric(pattern: str) -> float | None:
        match = re.search(pattern, summary)
        if not match:
            raise ValueError("Incomplete FFmpeg loudness/peak summary")
        item = float(match[1])
        return item if math.isfinite(item) else None

    integrated = metric(rf"Integrated loudness:\s*I:\s*({NUMBER})")
    result = {
        "integrated_lufs": integrated if integrated is not None and integrated > -70 else None,
        "loudness_range_lu": metric(rf"Loudness range:\s*LRA:\s*({NUMBER})"),
        "sample_peak_dbfs": metric(rf"Sample peak:\s*Peak:\s*({NUMBER})"),
        "true_peak_dbtp": metric(rf"True peak:\s*Peak:\s*({NUMBER})"),
    }
    minutes: dict[int, list[float]] = {}
    loudest = None
    for match in FRAME.finditer(log):
        time, momentary, shortterm = map(float, match.groups())
        # Startup windows and readings below the absolute loudness gate are excluded.
        if (time >= .39 and math.isfinite(momentary) and momentary > -70
                and (loudest is None or momentary > loudest["lufs"])):
            loudest = {"window_end_seconds": round(time, 2), "lufs": momentary}
        if time >= 2.99 and math.isfinite(shortterm) and shortterm > -70:
            minutes.setdefault(int(time // 60), []).append(shortterm)
    result["loudest_momentary"] = loudest
    result["minute_shortterm"] = [
        {"start_seconds": minute * 60, "median_lufs": round(median(levels), 1),
         "min_lufs": min(levels), "max_lufs": max(levels), "readings": len(levels)}
        for minute, levels in sorted(minutes.items())
    ]
    return result


def inspect_mix(source: Path, output: Path, *, ffmpeg: Path | None = None) -> dict:
    """Measure the complete mono/stereo WAV without playing or rewriting any audio."""
    import soundfile as sf

    source = source.resolve()
    if output.exists():
        raise ValueError("Choose a new results directory; existing results are never overwritten")
    binary = str(ffmpeg.resolve()) if ffmpeg else shutil.which("ffmpeg")
    if not binary or not Path(binary).is_file():
        raise ValueError("FFmpeg is required: install it on PATH or pass --ffmpeg PATH")
    before = source.stat()
    info = sf.info(str(source))
    if info.channels not in {1, 2} or info.duration < 10:
        raise ValueError("Use a mono/stereo rendered mix at least 10 seconds long")
    digest = file_hash(source)
    command = [binary, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
               "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-af", "ebur128=peak=sample+true", "-f", "null", "-"]
    try:
        process = subprocess.run(command, capture_output=True, text=True, check=False,
                                 timeout=max(120, info.duration * 2))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg mix measurement timed out; no report was written") from exc
    if process.returncode:
        raise RuntimeError(f"FFmpeg mix measurement failed: {process.stderr[-2000:]}")
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Mix file changed during measurement; wait for export to finish")
    metrics = parse_measurements(process.stderr)
    warnings = []
    if metrics["integrated_lufs"] is None:
        warnings.append("No measurable programme above the loudness gate; check the render")
    if metrics["true_peak_dbtp"] is not None and metrics["true_peak_dbtp"] > -1:
        warnings.append("True peak is above -1 dBTP; review output headroom before encoding")
    result = {"schema_version": 1, "kind": "wcfi_rendered_mix_inspection",
              "source": str(source), "audio_sha256": digest,
              "duration_seconds": round(info.duration, 3), "sample_rate": info.samplerate,
              "channels": info.channels, "metrics": metrics, "warnings": warnings,
              "method": "FFmpeg ebur128 scanner, sample and true peak; no gain or normalization",
              "limitations": [
                  "Rendered mix only: cannot identify which input caused a problem or measure the ATEM",
                  "Minute rows summarize overlapping 3-second windows above -70 LUFS, not integrated loudness",
                  "Times are relative to the exported file, not necessarily the Ableton arrangement",
                  "Measurements do not judge musical balance, intelligibility, or EQ quality",
              ]}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "mix.json", result)
    (output / "ffmpeg.log").write_text(process.stderr, encoding="utf-8")
    (output / "report.html").write_text(render_mix_report(result), encoding="utf-8")
    return result


def render_mix_report(report: dict) -> str:
    metrics = report["metrics"]
    rows = "".join(
        f"<tr><td>{row['start_seconds'] // 60}:00</td><td>{row['median_lufs']:.1f}</td>"
        f"<td>{row['min_lufs']:.1f} to {row['max_lufs']:.1f}</td></tr>"
        for row in metrics["minute_shortterm"]
    )
    notes = "".join(f"<li>{html.escape(item)}</li>"
                    for item in report["warnings"] + report["limitations"])
    headline = "".join(f"<p><strong>{label}:</strong> {metrics[key]}</p>" for key, label in (
        ("integrated_lufs", "Integrated loudness (LUFS)"),
        ("loudness_range_lu", "Loudness range (LU)"),
        ("sample_peak_dbfs", "Sample peak (dBFS)"),
        ("true_peak_dbtp", "True peak (dBTP)"),
    ))
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>WCFI recorded mix inspection</title><style>"
            "body{font:17px system-ui;max-width:900px;margin:40px auto;padding:0 24px;"
            "background:#101923;color:#e4ecf3}table{border-collapse:collapse;width:100%}"
            "td,th{padding:10px;text-align:left;border-bottom:1px solid #394956}"
            "li{margin:10px 0}code{overflow-wrap:anywhere}</style>"
            "<h1>Recorded mix inspection</h1>"
            f"<p><code>{html.escape(report['source'])}</code></p>"
            f"<p>{report['duration_seconds']:.1f} seconds · {report['sample_rate']} Hz</p>"
            f"{headline}<ul>{notes}</ul><h2>Loudness through the recording</h2>"
            "<p>Each row summarizes short-term loudness readings in one minute. "
            "Minutes without readings above -70 LUFS are omitted. None means unmeasurable.</p>"
            "<table><thead><tr><th>File time</th><th>Median (LUFS)</th>"
            f"<th>Range of readings (LUFS)</th></tr></thead><tbody>{rows}</tbody></table></html>")
