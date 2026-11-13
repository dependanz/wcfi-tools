"""Install a public checkpoint or run the blind audio trial entirely on local files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from wcfi_tools.soundcheck.local_listening import MODEL_ID, MusicFlamingoListener, listen_local
from wcfi_tools.soundcheck.profiles import file_hash, write_json


def download(output: Path) -> dict:
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_XET_CACHE"] = str(output.resolve() / "xet-cache")
    from huggingface_hub import HfApi, snapshot_download

    output = output.resolve()
    # Public model metadata/weights only; no credential lookup, remote Python, or audio.
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "checkpoint.json"
    if manifest.exists():
        raise ValueError("Checkpoint already prepared; use its recorded local path")
    pending = output / "download-request.json"
    if pending.exists():
        request = json.loads(pending.read_text(encoding="utf-8"))
    else:
        info = HfApi(token=False).model_info(MODEL_ID)
        request = {"model": MODEL_ID, "revision": info.sha}
        write_json(pending, request)
    if request["model"] != MODEL_ID:
        raise ValueError("Unexpected checkpoint download request")
    print(f"Downloading {MODEL_ID} at revision {request['revision']}", flush=True)
    checkpoint = snapshot_download(
        repo_id=MODEL_ID, revision=request["revision"], token=False,
        cache_dir=output / "cache", max_workers=2,
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.jinja",
                        "LICENSE*", "README.md"],
    )
    result = {**request, "checkpoint": str(Path(checkpoint).resolve()),
              "created_at": datetime.now(UTC).isoformat(), "audio_upload": False,
              "trust_remote_code": False}
    write_json(manifest, result)
    return result


def download_http(output: Path, *, model_id: str = MODEL_ID, workers: int = 4) -> dict:
    """Bounded resumable ranges when the Hub's accelerated downloader stalls."""
    import httpx2 as httpx
    from huggingface_hub import HfApi

    if not 1 <= workers <= 8:
        raise ValueError("Use 1–8 download workers")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pending = output / "download-request.json"
    if not pending.exists():
        info = HfApi(token=False).model_info(model_id)
        write_json(pending, {"model": model_id, "revision": info.sha})
    request = json.loads(pending.read_text(encoding="utf-8"))
    if request["model"] != model_id:
        raise ValueError("Unexpected checkpoint download request")
    checkpoint = output / "https-checkpoint"
    checkpoint.mkdir(exist_ok=True)
    if (output / "https-checkpoint.json").exists():
        raise ValueError("HTTP checkpoint already complete")
    info = HfApi(token=False).model_info(model_id, revision=request["revision"], files_metadata=True)
    expected = next(file for file in info.siblings if file.rfilename == "model.safetensors")
    size, digest = expected.size, expected.lfs.sha256
    url_root = f"https://huggingface.co/{model_id}/resolve/{request['revision']}/"
    filenames = (("config.json", "README.md") if model_id == "facebook/audiobox-aesthetics" else
                 ("config.json", "generation_config.json", "processor_config.json",
                  "tokenizer.json", "tokenizer_config.json", "chat_template.jinja", "README.md"))
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for name in filenames:
            target = checkpoint / name
            if not target.exists():
                response = client.get(url_root + quote(name))
                response.raise_for_status()
                with target.open("xb") as stream:
                    stream.write(response.content)
    chunks = output / "http-chunks"
    chunks.mkdir(exist_ok=True)
    chunk_size = 64 * 1024 * 1024
    count = (size + chunk_size - 1) // chunk_size

    def fetch(index: int) -> Path:
        start, end = index * chunk_size, min(size, (index + 1) * chunk_size) - 1
        target = chunks / f"chunk-{index:04d}"
        if target.exists() and target.stat().st_size == end - start + 1:
            return target
        partial = chunks / f"chunk-{index:04d}.partial"
        with (
            httpx.Client(follow_redirects=True, timeout=120) as client,
            client.stream("GET", url_root + "model.safetensors",
                          headers={"Range": f"bytes={start}-{end}"}) as response,
        ):
            response.raise_for_status()
            if response.status_code != 206 or response.headers.get("content-range") != (
                f"bytes {start}-{end}/{size}"
            ):
                raise RuntimeError("Model server did not honor the requested byte range")
            with partial.open("wb") as stream:
                for data in response.iter_bytes(chunk_size=1024 * 1024):
                    stream.write(data)
        if partial.stat().st_size != end - start + 1:
            raise RuntimeError("Incomplete model range")
        partial.replace(target)
        return target

    print(f"Downloading {size / 1e9:.2f} GB in {count} verified-size ranges", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, _ in enumerate(pool.map(fetch, range(count)), 1):
            if done % 8 == 0 or done == count:
                print(f"Model download: {done}/{count} ranges", flush=True)
    partial_weights = checkpoint / "model.safetensors.partial"
    actual = hashlib.sha256()
    with partial_weights.open("wb") as assembled:
        for index in range(count):
            with (chunks / f"chunk-{index:04d}").open("rb") as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(data)
                    assembled.write(data)
    if partial_weights.stat().st_size != size or actual.hexdigest() != digest:
        raise RuntimeError("Model weights failed their official SHA256 integrity check")
    partial_weights.replace(checkpoint / "model.safetensors")
    result = {**request, "checkpoint": str(checkpoint), "weights_sha256": digest,
              "weights_bytes": size, "created_at": datetime.now(UTC).isoformat(),
              "audio_upload": False, "trust_remote_code": False}
    write_json(output / "https-checkpoint.json", result)
    # Delete only this downloader's enumerated chunks after verified assembly.
    for index in range(count):
        (chunks / f"chunk-{index:04d}").unlink()
    return result


def benchmark(directory: Path, output: Path, checkpoint: Path, ffmpeg: Path,
              engine: str = "audiobox", resume: bool = False, allow_gpu: bool = False) -> dict:
    directory, output = directory.resolve(), output.resolve()
    manifest_path = directory / "blind-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if output.exists() and not resume:
        raise ValueError("Choose a new results directory")
    if (output / "submission.json").exists():
        raise ValueError("This trial already completed")
    resolved = []
    ids = set()
    for case in manifest["clips"]:
        clip = (directory / case["file"]).resolve()
        identifier = case["id"]
        if (not isinstance(identifier, str) or not identifier.startswith("clip-")
                or not identifier[5:].isdigit() or identifier in ids):
            raise ValueError("Invalid or duplicate clip id")
        ids.add(identifier)
        if not clip.is_relative_to(directory / "clips") or file_hash(clip) != case["sha256"]:
            raise ValueError("Benchmark clip changed or escaped the clips directory")
        resolved.append((case, clip))
    if resume:
        for case, _ in resolved:
            review_dir = output / case["id"]
            if review_dir.exists() and not (review_dir / "listen.json").is_file():
                raise ValueError(f"Interrupted review {case['id']} has no saved result. "
                                 "Choose a new output directory; partial evidence is preserved.")
    # Load once; labels, metrics and private answer keys never enter the prompt.
    print(f"Loading local model: {engine}", flush=True)
    if engine == "audiobox":
        from wcfi_tools.soundcheck.quality_listening import AudioboxListener

        listener = AudioboxListener(checkpoint)
    else:
        listener = MusicFlamingoListener(checkpoint, allow_gpu=allow_gpu)
    if resume and output.exists():
        previous = json.loads((output / "trial.json").read_text(encoding="utf-8"))
        if (previous["blind_manifest_sha256"] != file_hash(manifest_path)
                or previous["runtime"] != listener.runtime):
            raise ValueError("Cannot resume with changed inputs, model or runtime")
    else:
        output.mkdir(parents=True)
        write_json(output / "trial.json", {"blind_manifest_sha256": file_hash(manifest_path),
                                          "runtime": listener.runtime,
                                          "private_answer_key_read": False, "audio_upload": False})
    completed = []
    for case, clip in resolved:
        print(f"Listening locally: {case['id']}", flush=True)
        review_path = output / case["id"] / "listen.json"
        if resume and review_path.exists():
            review = json.loads(review_path.read_text(encoding="utf-8"))
            if (review["status"] not in {"reviewed", "reviewed_truncated"}
                    or review["excerpt"]["source_sha256"] != case["sha256"]
                    or review["operator_context"] != ""
                    or review["observations"]["runtime"] != listener.runtime
                    or file_hash(review_path.parent / "excerpt-model.wav")
                    != review["model_audio"]["sha256"]):
                raise ValueError("Existing review cannot be reused; no automatic retry")
            print(f"Reusing completed {case['id']} (hashes/runtime verified)", flush=True)
        else:
            review = listen_local(clip, output / case["id"], listener=listener,
                                  seconds=case["duration_seconds"], context="", ffmpeg=ffmpeg)
        completed.append({"id": case["id"], "report": f"{case['id']}/listen.json",
                          "status": review["status"]})
        print(review["observations"]["text"], flush=True)
    result = {"completed_at": datetime.now(UTC).isoformat(),
              "status": "awaiting_manual_scoring", "completed": completed,
              "audio_device_access": False, "ableton_access": False, "audio_upload": False,
              "automatic_mix_changes": False, "runtime": listener.runtime}
    write_json(output / "submission.json", result)
    return result


def main() -> None:
    # Generated descriptions may contain Unicode that a Windows legacy pipe
    # cannot encode. Keep the saved result and console output consistent.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("download", help="Download official public safetensors only")
    install.add_argument("--output", required=True, type=Path)
    direct = commands.add_parser("download-http", help="Resume pinned public model via HTTPS ranges")
    direct.add_argument("--output", required=True, type=Path)
    direct.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    quality = commands.add_parser("download-quality", help="Download Meta's audio quality model")
    quality.add_argument("--output", required=True, type=Path)
    trial = commands.add_parser("benchmark", help="Run a prepared blind manifest locally")
    trial.add_argument("directory", type=Path)
    trial.add_argument("--output", required=True, type=Path)
    trial.add_argument("--checkpoint", required=True, type=Path)
    trial.add_argument("--ffmpeg", required=True, type=Path)
    trial.add_argument("--engine", choices=["music-flamingo", "audiobox"], default="audiobox")
    trial.add_argument("--allow-gpu", action="store_true", help="Explicit research GPU opt-in")
    trial.add_argument("--resume", action="store_true", help="Reuse verified completed local reviews")
    args = parser.parse_args()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "2"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    if args.command == "download-quality":
        print(json.dumps(download_http(args.output, model_id="facebook/audiobox-aesthetics"),
                         indent=2))
    elif args.command in {"download", "download-http"}:
        result = (download(args.output) if args.command == "download" else
                  download_http(args.output, workers=args.workers))
        print(json.dumps(result, indent=2))
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"
        # Reuse only the process-priority helper; it does not touch a sound device.
        spec = importlib.util.spec_from_file_location(
            "listening_benchmark", Path(__file__).with_name("listening-benchmark.py"),
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        helper.limit_resources()
        benchmark(args.directory, args.output, args.checkpoint, args.ffmpeg,
                  args.engine, args.resume, args.allow_gpu)


if __name__ == "__main__":
    main()
