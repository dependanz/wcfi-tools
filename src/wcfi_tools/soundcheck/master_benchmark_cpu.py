"""Single-worker CPU-only Audiobox benchmark inference; no online fallback."""
from __future__ import annotations

import contextlib
import ctypes as ct
import os
import socket
import statistics
import threading
import time
from pathlib import Path

from .profiles import file_hash, write_json


def configure_cpu():
    # Set before importing torch or any numerical runtime.
    os.environ.update(CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false")
    from ..commands.role_analysis import limit_resources

    limit_resources()
    if os.name != "nt":
        if hasattr(os, "sched_getaffinity"):
            os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
        os.nice(19)


@contextlib.contextmanager
def offline_connections():
    """Deny network connections in this worker only, including accidental downloads."""
    original = socket.socket.connect
    attempts = []

    def denied(*_args, **_kwargs):
        attempts.append(True)
        raise RuntimeError("Network access is disabled in the Master benchmark worker")

    socket.socket.connect = denied
    try:
        yield attempts
    finally:
        socket.socket.connect = original


def _memory():
    if os.name != "nt":
        return {}

    class Counters(ct.Structure):
        _fields_ = [("cb", ct.c_ulong), ("faults", ct.c_ulong)] + [
            (name, ct.c_size_t) for name in ("peak_working", "working", "peak_paged",
            "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]

    kernel, psapi = ct.WinDLL("kernel32"), ct.WinDLL("psapi")
    kernel.GetCurrentProcess.restype = ct.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (ct.c_void_p, ct.POINTER(Counters), ct.c_ulong)
    counters = Counters(); counters.cb = ct.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ct.byref(counters), counters.cb):
        return {}
    return {"working_set_mib": round(counters.working/2**20, 1),
            "peak_working_set_mib": round(counters.peak_working/2**20, 1)}


def _cpu_assertions(listener, torch):
    if torch.cuda.is_initialized():
        raise RuntimeError("GPU initialization detected; CPU benchmark stopped")
    if (torch.get_num_threads() != 1 or torch.get_num_interop_threads() != 1
            or any(p.device.type != "cpu" for p in listener.model.parameters())
            or any(p.device.type != "cpu" for p in listener.model.buffers())):
        raise RuntimeError("Model left its single-thread CPU budget")


def _deadline(seconds, output, stage):
    def stop():
        write_json(output/("timeout-"+stage+".json"), {
            "status": "stopped_no_retry", "stage": stage, "limit_seconds": seconds})
        os._exit(124)

    timer = threading.Timer(seconds, stop); timer.daemon = True; timer.start()
    return timer


def run(directory, output, checkpoint, *, progress=print):
    """Read only the blinded manifest during inference; no deduplication or retries."""
    directory, output, checkpoint = [Path(p).resolve() for p in (directory, output, checkpoint)]
    if output.exists():
        raise ValueError("No automatic retries or overwrites; choose a new run directory")
    configure_cpu()
    from .master_benchmark import validate_blind

    manifest = validate_blind(directory)
    if not all((checkpoint/name).is_file() for name in ("config.json", "model.safetensors")):
        raise ValueError("Provide an existing local Audiobox safetensors checkpoint")
    clips = {c["id"]: c for c in manifest["clips"]}
    schedule = [(c["id"], False) for c in manifest["clips"]]
    schedule += [(identifier, True) for identifier in manifest["repeat_ids"]]
    output.mkdir(parents=True)
    write_json(output/"run-request.json", {"kind": "wcfi_master_benchmark_cpu_run",
        "manifest_sha256": file_hash(directory/"blind-manifest.json"),
        "checkpoint_config_sha256": file_hash(checkpoint/"config.json"),
        "checkpoint_weights_sha256": file_hash(checkpoint/"model.safetensors"),
        "device": "cpu", "logical_cpus_allowed": 1, "torch_threads": 1,
        "priority": "idle", "automatic_retries": 0, "audio_uploads": 0,
        "answer_key_read_during_inference": False, "deduplicate_identical_inputs": False,
        "cooldown": "Pause after each inference for its duration, capped at 10 seconds",
        "load_deadline_seconds": 45, "prediction_deadline_seconds": 30,
        "whole_run_deadline_seconds": 1200, "planned_inferences": len(schedule)})
    began = time.monotonic(); process_start = time.process_time()
    whole = _deadline(1200, output, "whole-run")
    records, elapsed_values = [], []
    try:
        with offline_connections() as attempts:
            loading = _deadline(45, output, "model-load")
            try:
                import torch

                from .quality_listening import AudioboxListener

                torch.set_num_threads(1); torch.set_num_interop_threads(1)
                if torch.cuda.is_initialized():
                    raise RuntimeError("GPU already initialized in the inference worker")
                listener = AudioboxListener(checkpoint, cpu_threads=1)
                _cpu_assertions(listener, torch)
            finally:
                loading.cancel()
            runtime = listener.runtime | {"gpu_initialized": False, "torch_interop_threads": 1,
                "logical_cpus_allowed": 1, "priority": "idle", "network_connections_blocked": True,
                "load_seconds": round(time.monotonic()-began, 3), **_memory()}
            write_json(output/"runtime.json", runtime)
            progress(f"Audiobox loaded on CPU only; {len(schedule)} scheduled inferences.")
            for index, (identifier, repeat) in enumerate(schedule, 1):
                clip = clips[identifier]; path = directory/clip["model_file"]
                if file_hash(path) != clip["model_sha256"]:
                    raise ValueError("Model input changed after preflight")
                prediction_timer = _deadline(30, output, f"inference-{index:04d}")
                try:
                    prediction = listener.observe(path)
                    _cpu_assertions(listener, torch)
                finally:
                    prediction_timer.cancel()
                if attempts:
                    raise RuntimeError("Unexpected network attempt; benchmark stopped")
                prediction["runtime"] = runtime
                filename = f"prediction-{index:04d}.json"
                write_json(output/filename, {"id": identifier, "repeat": repeat,
                    "input_sha256": clip["model_sha256"], "prediction": prediction,
                    "worker_memory": _memory()})
                records.append({"file": filename, "sha256": file_hash(output/filename)})
                seconds = prediction["elapsed_seconds"]; elapsed_values.append(seconds)
                progress(f"{index}/{len(schedule)} · {seconds:.2f}s CPU inference; cooldown.")
                time.sleep(min(10, max(1, seconds)))
            result = {"status": "complete", "runtime": runtime,
                "manifest_sha256": file_hash(directory/"blind-manifest.json"), "predictions": records,
                "timing": {"inference_sum_seconds": sum(elapsed_values),
                    "minimum_seconds": min(elapsed_values), "median_seconds": statistics.median(elapsed_values),
                    "maximum_seconds": max(elapsed_values),
                    "wall_seconds_including_load_and_cooldowns": time.monotonic()-began,
                    "process_cpu_seconds": time.process_time()-process_start},
                "final_worker_memory": _memory(), "gpu_initialized": False,
                "network_connection_attempts": len(attempts), "audio_uploads": 0, "daw_operations": 0}
            write_json(output/"batch-result.json", result)
            return result
    except Exception as exc:
        write_json(output/"failure.json", {"status": "failed_partial_no_retry",
            "completed_inferences": len(records), "error": str(exc),
            "audio_uploads": 0, "daw_operations": 0})
        raise
    finally:
        whole.cancel()
