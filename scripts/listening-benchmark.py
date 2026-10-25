"""Prepare and submit a small blind listening trial without opening an audio device.

Preparation is local. Submission is an explicit second command, using the existing
OpenAI listening integration. The private answer key is never read by submission.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from wcfi_tools.soundcheck.listening import DEFAULT_MODEL, listen
from wcfi_tools.soundcheck.mix import parse_measurements
from wcfi_tools.soundcheck.profiles import file_hash, number, write_json


def limit_resources() -> None:
    # Limit only this task's processes; never change Ableton or system priorities.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
            raise RuntimeError("Could not set this benchmark process to below-normal priority")


def measure(path: Path, log: Path, ffmpeg: Path) -> dict:
    command = [str(ffmpeg), "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
               "-threads", "1", "-filter_threads", "1", "-i", str(path),
               "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", "ebur128=peak=sample+true",
               "-f", "null", "-"]
    options = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS} if os.name == "nt" else {}
    result = subprocess.run(command, capture_output=True, text=True, timeout=120,
                            check=False, **options)
    if result.returncode:
        raise RuntimeError("FFmpeg measurement failed; no model request was made")
    log.write_text(result.stderr, encoding="utf-8")
    return parse_measurements(result.stderr)


def variants(samples, rate: int) -> list[tuple[str, object, dict]]:
    import numpy as np
    from scipy.signal import butter, sosfiltfilt

    driven = samples * 10 ** (18 / 20)
    clipped_fraction = float(np.mean(np.abs(driven) >= 1))
    if clipped_fraction < .01:
        raise ValueError("Choose an active excerpt: fewer than 1% of samples would be clipped")
    muffled = sosfiltfilt(butter(6, 1500, fs=rate, output="sos"), samples, axis=0)
    return [
        ("control", samples.copy(), {"introduced_fault": None}),
        ("hard_clipping", np.clip(driven, -1, 1), {
            "introduced_fault": "18 dB drive followed by hard clipping at +/-1",
            "clipped_sample_fraction_before_matching": clipped_fraction,
        }),
        ("lowpass", muffled, {
            "introduced_fault": "Sixth-order zero-phase low-pass at 1500 Hz",
            "filter": "scipy.signal.butter + sosfiltfilt; applied to both channels",
        }),
    ]


def prepare(source: Path, output: Path, *, starts: list[float], seconds: float,
            seed: int, ffmpeg: Path) -> dict:
    import numpy as np
    import soundfile as sf

    source, output, ffmpeg = source.resolve(), output.resolve(), ffmpeg.resolve()
    if output.exists():
        raise ValueError("Choose a new output directory; previous trials are never overwritten")
    if not ffmpeg.is_file():
        raise ValueError("FFmpeg executable missing")
    seconds = number(seconds, "seconds", 10, 30)
    if len(starts) != 2 or starts[0] == starts[1]:
        raise ValueError("Supply two different file-relative start times")
    starts = [number(start, "start", 0, 86400) for start in starts]
    before = source.stat()
    source_hash = file_hash(source)
    candidates = []
    with sf.SoundFile(source) as stream:
        rate = stream.samplerate
        frames = round(seconds * rate)
        if stream.channels not in {1, 2} or not 8000 <= rate <= 48000:
            raise ValueError("Use a finished mono/stereo WAV at 8-48 kHz")
        for section, start in enumerate(starts):
            offset = round(start * rate)
            if offset + frames > len(stream):
                raise ValueError("An excerpt extends beyond the finished recording")
            stream.seek(offset)
            samples = stream.read(frames, dtype="float64", always_2d=True)
            if not np.isfinite(samples).all():
                raise ValueError("Non-finite source samples")
            for condition, processed, facts in variants(samples, rate):
                candidates.append({"section": section, "source_start_seconds": offset / rate,
                                   "condition": condition, "samples": processed, "facts": facts})
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed; wait until recording/export has finished")
    random.Random(seed).shuffle(candidates)
    output.mkdir(parents=True, exist_ok=False)
    (output / "clips").mkdir()
    (output / "private-measurements").mkdir()
    pre_metrics = []
    for index, candidate in enumerate(candidates, 1):
        clip_id = f"clip-{index:02d}"
        raw = output / "private-measurements" / f"{clip_id}-before.wav"
        sf.write(raw, candidate["samples"], rate, subtype="FLOAT")
        pre_metrics.append(measure(raw, raw.with_suffix(".log"), ffmpeg))
    if any(m["integrated_lufs"] is None or m["true_peak_dbtp"] is None for m in pre_metrics):
        raise ValueError("Unmeasurable programme; choose active excerpts")
    # Use one common LUFS target. Reserve at least 3 dB true-peak headroom for every clip.
    target = min(-18.0, *(m["integrated_lufs"] - m["true_peak_dbtp"] - 3 for m in pre_metrics))
    public, private = [], []
    for index, (candidate, metrics) in enumerate(zip(candidates, pre_metrics, strict=True), 1):
        clip_id = f"clip-{index:02d}"
        gain = target - metrics["integrated_lufs"]
        matched = candidate["samples"] * 10 ** (gain / 20)
        if not np.isfinite(matched).all() or np.max(np.abs(matched)) >= .999:
            raise ValueError("Matched audio lacks PCM16 headroom")
        clip = output / "clips" / f"{clip_id}.wav"
        sf.write(clip, matched, rate, subtype="PCM_16")
        actual = measure(clip, output / "private-measurements" / f"{clip_id}-matched.log", ffmpeg)
        if abs(actual["integrated_lufs"] - target) > .21 or actual["true_peak_dbtp"] > -2.7:
            raise ValueError("Loudness matching or headroom verification failed")
        public.append({"id": clip_id, "file": f"clips/{clip_id}.wav",
                       "sha256": file_hash(clip), "duration_seconds": frames / rate})
        private.append({"id": clip_id, "section": candidate["section"],
                        "source_start_seconds": candidate["source_start_seconds"],
                        "condition": candidate["condition"], **candidate["facts"],
                        "match_gain_db": gain, "before_matching": metrics,
                        "after_matching": actual})
    final_stat = source.stat()
    if (before.st_size, before.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
        raise ValueError("Source changed during preparation; do not submit this trial")
    write_json(output / "private-answer-key.json", {
        "source": str(source), "source_sha256": source_hash, "seed": seed,
        "target_integrated_lufs": target, "clips": private,
        "limits": ["Controls have no introduced fault; the original mix may have issues",
                   "Two excerpts from one recording do not establish general listening accuracy",
                   "No isolated lead-vocal or instrument-balance tests are included",
                   "No human listening validation has occurred; synthetic faults are known operations",
                   "Equal integrated LUFS is a comparison control, not identical perceived loudness"]})
    manifest = {"schema_version": 1, "created_at": datetime.now(UTC).isoformat(),
                "status": "prepared_no_upload", "clips": public,
                "audio_device_access": False, "ableton_access": False}
    write_json(output / "blind-manifest.json", manifest)
    return manifest


def submit(directory: Path, *, api_key: str, model: str, ffmpeg: Path) -> dict:
    """Run serial independent requests. No answer key, paths, metrics or faults in prompts."""
    directory = directory.resolve()
    manifest = json.loads((directory / "blind-manifest.json").read_text(encoding="utf-8"))
    if not api_key:
        raise ValueError("OpenAI key missing; the prepared trial remains local")
    if (directory / "submission.json").exists() or (directory / "reviews").exists():
        raise ValueError("Submission was already attempted; no automatic retries")
    resolved = []
    for case in manifest["clips"]:
        clip = (directory / case["file"]).resolve()
        if not clip.is_relative_to(directory / "clips") or file_hash(clip) != case["sha256"]:
            raise ValueError("Benchmark clip changed or escaped the clips directory")
        resolved.append((case, clip))
    (directory / "reviews").mkdir()
    completed = []
    failure = None
    for case, clip in resolved:
        print(f"Submitting {case['id']} ({case['duration_seconds']:g} s)", flush=True)
        try:
            review = listen(clip, directory / "reviews" / case["id"],
                            seconds=case["duration_seconds"], api_key=api_key,
                            model=model, context="", ffmpeg=ffmpeg)
        except Exception as exc:  # noqa: BLE001 - record redacted failure, stop paid requests
            failure = {"clip_id": case["id"], "error_type": type(exc).__name__}
            break
        completed.append({"id": case["id"], "status": review["status"],
                          "report": f"reviews/{case['id']}/listen.json",
                          "model": review["observations"]["model"],
                          "usage": review["observations"]["usage"]})
        print(f"Completed {case['id']}", flush=True)
    result = {"completed_at": datetime.now(UTC).isoformat(),
              "status": "failed_partial" if failure else "awaiting_manual_scoring",
              "model": model, "completed": completed, "failure": failure,
              "audio_device_access": False, "ableton_access": False,
              "automatic_mix_changes": False,
              "note": "Read independent observations before comparing with the private answer key"}
    write_json(directory / "submission.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="Local copies and verified fault labels")
    prepare_parser.add_argument("source", type=Path)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--starts", type=float, nargs=2, required=True)
    prepare_parser.add_argument("--seconds", type=float, default=15)
    prepare_parser.add_argument("--seed", type=int, default=20261011)
    prepare_parser.add_argument("--ffmpeg", type=Path, required=True)
    submit_parser = commands.add_parser("submit", help="Upload the six blind clips to OpenAI")
    submit_parser.add_argument("directory", type=Path)
    submit_parser.add_argument("--model", default=DEFAULT_MODEL)
    submit_parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    limit_resources()
    if args.command == "prepare":
        result = prepare(args.source, args.output, starts=args.starts, seconds=args.seconds,
                         seed=args.seed, ffmpeg=args.ffmpeg)
        print(f"Prepared {len(result['clips'])} blind clips locally. No upload or playback.")
    else:
        from wcfi_tools.config import get_secret, load_config

        load_config()
        result = submit(args.directory, api_key=get_secret("openai"),
                        model=args.model, ffmpeg=args.ffmpeg)
        print(result["status"])
    return 1 if result.get("failure") else 0


if __name__ == "__main__":
    raise SystemExit(main())
