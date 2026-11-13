"""Local learned audio-quality estimates, separate from signal measurements."""

from __future__ import annotations

import json
import math
import time
from importlib.metadata import version
from pathlib import Path

from .local_listening import read_model_audio
from .profiles import file_hash

MODEL_ID = "facebook/audiobox-aesthetics"


class AudioboxListener:
    """Use the official architecture/weights in float32 with CPU-only inference."""

    def __init__(self, checkpoint: Path, *, cpu_threads: int = 1):
        if type(cpu_threads) is not int or not 1 <= cpu_threads <= 4:
            raise ValueError("Use 1–4 CPU inference threads")
        import torch
        from audiobox_aesthetics.model.aes import AesMultiOutput
        from safetensors.torch import load_file

        checkpoint = checkpoint.resolve()
        config = json.loads((checkpoint / "config.json").read_text(encoding="utf-8"))
        if set(config.get("target_transform", {})) != {"CE", "CU", "PC", "PQ"}:
            raise ValueError("Use the Audiobox Aesthetics checkpoint with --engine audiobox")
        # Do not invoke an auto-downloading predictor or load the pickle checkpoint.
        # Full precision CPU inference avoids reduced precision and GPU contention.
        config["precision"] = "32"
        torch.set_num_threads(cpu_threads)
        with torch.device("cpu"):
            self.model = AesMultiOutput(**config).cpu().eval()
        self.model.load_state_dict(load_file(str(checkpoint / "model.safetensors"), device="cpu"),
                                   strict=True)
        self.runtime = {
            "engine": "audiobox-aesthetics-local", "model": MODEL_ID,
            "checkpoint": str(checkpoint), "config_sha256": file_hash(checkpoint / "config.json"),
            "weights_sha256": file_hash(checkpoint / "model.safetensors"),
            "torch_version": torch.__version__, "audiobox_version": version("audiobox_aesthetics"),
            "device": "cpu", "precision": "float32", "cpu_threads": cpu_threads,
            "local_files_only": True,
            "audio_upload": False, "remote_code": False, "license": "CC-BY-4.0",
            "limitations": [
                "Learned quality scores are predictions, not measured dB or professional certification.",
                "Scores cannot identify a faulty channel or prescribe EQ, gain or compressor settings.",
                "Use production-quality scores for controlled comparisons, not channel importance.",
            ],
        }

    def observe(self, audio: Path, *, context: str = "") -> dict:
        import torch
        from audiobox_aesthetics.infer import make_inference_batch

        if context:
            raise ValueError("Audiobox scores only audio; operator context is not an input")
        started = time.monotonic()
        samples = read_model_audio(audio)
        rate = 16000
        wave = torch.from_numpy(samples).unsqueeze(0)
        # Match the official predictor: 10 s non-overlapping windows, zero-pad
        # the final window, and weight its score by actual duration.
        wavs, masks, weights, _ = make_inference_batch([wave], sample_rate=rate)
        values = {axis: [] for axis in self.model.axes_name}
        with torch.inference_mode():
            for wav, mask in zip(wavs, masks, strict=True):
                estimates = self.model({"wav": wav.unsqueeze(0), "mask": mask.unsqueeze(0)})
                for axis in self.model.axes_name:
                    transform = self.model.target_transform[axis]
                    value = (estimates[axis].item() * transform["std"] + transform["mean"])
                    if not math.isfinite(value):
                        raise RuntimeError("Non-finite audio-quality prediction")
                    values[axis].append(value)
        scores = {axis: sum(value * weight for value, weight in zip(values[axis], weights,
                                                                  strict=True)) / sum(weights)
                  for axis in values}
        return {
            "text": f"Predicted production quality (PQ): {scores['PQ']:.3f} on a 1–10 rating scale.\n"
                    "This is an experimental learned rating, not a professional mix certification. "
                    "It does not diagnose distortion, identify channels or prescribe settings.",
            "model": MODEL_ID, "runtime": self.runtime, "scores": scores,
            "window_scores": values, "window_weights": weights, "truncated": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "evidence_type": "unvalidated_learned_audio_quality_estimates",
        }
