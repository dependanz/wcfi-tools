"""Offline, bounded, CPU-only execution of the released SongBench architecture."""
from __future__ import annotations

import gc
import importlib
import importlib.util
import os
import statistics
import sys
import threading
import time
import types
from pathlib import Path

from .master_benchmark_cpu import _deadline, _memory, configure_cpu, offline_connections
from .profiles import file_hash, write_json
from .songbench_external import AXES, read_json


def _assert_cpu(torch, models):
    if torch.cuda.is_initialized() or torch.get_num_threads() != 1 or torch.get_num_interop_threads() != 1:
        raise RuntimeError("SongBench left its CPU-only thread budget")
    if any(p.device.type != "cpu" for m in models for p in (*m.parameters(), *m.buffers())):
        raise RuntimeError("Non-CPU model tensor detected")


def load_models(root):
    """Load reviewed local source and safetensors; no pickle or remote-code loader."""
    from safetensors.torch import load_file

    # Avoid MuQ's eager root import of its unrelated text/multimodal model.
    # This aliases the unmodified published audio-only package; architecture is unchanged.
    package = types.ModuleType("_wcfi_songbench_muq_audio")
    package.__path__ = [str(root/"deps/muq/muq")]
    sys.modules[package.__name__] = package
    muq_class = importlib.import_module(package.__name__+".muq").MuQ
    muq = muq_class(read_json(root/"assets/muq-config.json")).eval().cpu()
    muq.load_state_dict(load_file(str(root/"assets/muq-model.safetensors"), device="cpu"), strict=True)
    spec = importlib.util.spec_from_file_location("_wcfi_songbench_head", root/"assets/songbench-model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    head = module.Generator(1024, 4096, 7, 4).eval().cpu()
    head.load_state_dict(load_file(str(root/"assets/songbench.safetensors"), device="cpu"), strict=True)
    return muq, head


def run(directory, output, assets_root, *, progress=print):
    configure_cpu()
    directory, output, root = [Path(p).resolve() for p in (directory, output, assets_root)]
    if output.exists():
        raise ValueError("Choose a new immutable inference run directory")
    os.environ["HF_HOME"] = str(root/"runtime-cache")
    os.environ["TRANSFORMERS_CACHE"] = str(root/"runtime-cache/transformers")
    os.environ["NUMBA_CACHE_DIR"] = str(root/"runtime-cache/numba")
    sys.path.insert(0, str(root/"deps"))
    manifest, policy = read_json(directory/"blind-manifest.json"), read_json(directory/"protocol.json")
    if (manifest["protocol_sha256"] != file_hash(directory/"protocol.json")
            or policy["cpu_implementation_sha256"] != file_hash(Path(__file__))
            or policy["implementation_sha256"] != file_hash(Path(__file__).with_name("songbench_external.py"))):
        raise ValueError("Frozen protocol or implementation changed")
    for name, digest in policy["assets_sha256"].items():
        if file_hash(root/"assets"/name) != digest:
            raise ValueError("Frozen model/data asset changed")
    for clip in manifest["clips"]:
        path = (directory/clip["file"]).resolve()
        path.relative_to(directory)
        if file_hash(path) != clip["sha256"]:
            raise ValueError("Frozen audio input changed")
    output.mkdir(parents=True)
    write_json(output/"run-request.json", {"manifest_sha256": file_hash(directory/"blind-manifest.json"),
        "device": "cpu", "logical_cpus": 1, "priority": "idle", "network": "blocked",
        "answer_key_read_during_inference": False, "retries": 0, "planned_inputs": len(manifest["clips"]),
        "per_input_deadline_seconds": 180, "whole_run_deadline_seconds": 5400,
        "working_set_limit_gib": 7, "minimum_available_ram_gib": 3,
        "cooldown": "inference duration capped at 15 seconds", "audio_uploads": 0, "daw_operations": 0})
    began, cpu_began = time.monotonic(), time.process_time()
    whole = _deadline(5400, output, "whole-run")
    stop = threading.Event()
    records, elapsed_values = [], []

    def memory_watch():
        import psutil

        process = psutil.Process()
        while not stop.wait(.5):
            if process.memory_info().rss > 7*2**30 or psutil.virtual_memory().available < 3*2**30:
                write_json(output/"memory-budget-stop.json", {"status": "stopped_no_retry", **_memory()})
                os._exit(125)

    try:
        with offline_connections() as attempts:
            loading = _deadline(120, output, "model-load")
            try:
                from importlib import metadata

                import psutil
                import soundfile as sf
                import torch

                torch.set_num_threads(1)
                torch.set_num_interop_threads(1)
                torch.manual_seed(policy["seed"])
                torch.use_deterministic_algorithms(True)
                watcher = threading.Thread(target=memory_watch, daemon=True)
                watcher.start()
                models = load_models(root)
                _assert_cpu(torch, models)
            finally:
                loading.cancel()
            runtime = {"device": "cpu", "gpu_initialized": False,
                "parameters_and_buffers_cpu": True, "torch_threads": torch.get_num_threads(),
                "torch_interop_threads": torch.get_num_interop_threads(),
                "logical_cpu_affinity": psutil.Process().cpu_affinity(),
                "priority": "idle", "network_connections_blocked": True,
                "load_seconds": time.monotonic()-began, **_memory(),
                "versions": {p: metadata.version(p) for p in ("torch", "muq", "transformers", "librosa", "numpy", "soundfile", "soxr", "safetensors")},
                "architecture": "Published MuQ + SongBench Generator; strict local safetensors loads; hidden_states[6]",
                "input": "Full excerpt, mono 24kHz float32; no level normalization or chunk averaging"}
            write_json(output/"runtime.json", runtime)
            progress(f"SongBench loaded on CPU; {len(manifest['clips'])} full excerpts scheduled.")
            muq, head = models
            for index, clip in enumerate(manifest["clips"], 1):
                path = directory/clip["file"]
                if file_hash(path) != clip["sha256"]:
                    raise ValueError("Input changed during inference")
                waveform, rate = sf.read(path, dtype="float32", always_2d=False)
                if rate != 24000 or waveform.ndim != 1:
                    raise ValueError("Model input must be mono 24kHz")
                deadline = _deadline(180, output, f"inference-{index:04d}")
                started, cpu_start = time.monotonic(), time.process_time()
                try:
                    with torch.inference_mode():
                        tensor = torch.from_numpy(waveform).unsqueeze(0).cpu()
                        features = muq(tensor, output_hidden_states=True)["hidden_states"][6]
                        values = head(features)[0].tolist()
                    _assert_cpu(torch, models)
                finally:
                    deadline.cancel()
                elapsed = time.monotonic()-started
                if attempts or not all(1 <= v <= 10 for v in values):
                    raise RuntimeError("Unexpected network attempt or invalid model score")
                filename = f"prediction-{index:04d}.json"
                write_json(output/filename, {"id": clip["id"], "input_sha256": clip["sha256"],
                    "scores": dict(zip(AXES, values)), "elapsed_seconds": elapsed,
                    "process_cpu_seconds": time.process_time()-cpu_start,
                    "audio_seconds": len(waveform)/rate, "memory": _memory(),
                    "gpu_initialized": torch.cuda.is_initialized()})
                records.append({"file": filename, "sha256": file_hash(output/filename)})
                elapsed_values.append(elapsed)
                progress(f"{index}/{len(manifest['clips'])}: {len(waveform)/rate:.1f}s audio, {elapsed:.1f}s inference, {_memory().get('working_set_mib')} MiB; cooling down.")
                del tensor, features, waveform, values
                gc.collect()
                time.sleep(min(15, max(1, elapsed)))
            result = {"status": "complete", "manifest_sha256": file_hash(directory/"blind-manifest.json"),
                "runtime": runtime, "predictions": records,
                "timing": {"inference_sum_seconds": sum(elapsed_values),
                    "median_seconds": statistics.median(elapsed_values), "maximum_seconds": max(elapsed_values),
                    "wall_seconds_including_load_and_cooldowns": time.monotonic()-began,
                    "process_cpu_seconds": time.process_time()-cpu_began},
                "final_memory": _memory(), "gpu_initialized": torch.cuda.is_initialized(),
                "network_connection_attempts": len(attempts), "audio_uploads": 0, "daw_operations": 0}
            write_json(output/"batch-result.json", result)
            return {k: v for k, v in result.items() if k != "predictions"}
    except Exception as exc:
        write_json(output/"failure.json", {"status": "failed_partial_no_retry",
            "completed": len(records), "error": str(exc), "audio_uploads": 0, "daw_operations": 0})
        raise
    finally:
        stop.set()
        whole.cancel()
