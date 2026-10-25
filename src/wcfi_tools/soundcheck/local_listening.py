"""Local audio inference; audio never leaves the computer.

Model installation is separate. Inference reads local safetensors and uses the
integrated implementation; it never loads remote Python or opens a sound device.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .listening import SYSTEM_PROMPT, _prepare_excerpt, render_report
from .mix import inspect_mix
from .profiles import file_hash, write_json

MODEL_ID = "nvidia/music-flamingo-2601-hf"
MODEL_RATE = 16000


class LocalListener(Protocol):
    runtime: dict

    def observe(self, audio: Path, *, context: str = "") -> dict: ...


def read_model_audio(audio: Path):
    """Reject invalid/silent/DC model inputs without inventing audible content."""
    import numpy as np
    import soundfile as sf

    samples, rate = sf.read(audio, dtype="float32")
    if (rate != MODEL_RATE or samples.ndim != 1
            or not 10 <= len(samples) / rate <= 60 or not np.isfinite(samples).all()):
        raise ValueError("Model input must be finite mono 16 kHz audio, 10–60 seconds")
    if float(np.ptp(samples)) < 1 / 32768:
        raise ValueError("No varying audio above PCM16 resolution; choose an active excerpt")
    return samples


def prepare_model_audio(source: Path, destination: Path) -> dict:
    """Document the mono/16 kHz conversion required by the audio encoders."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    if destination.exists():
        raise ValueError("Model input already exists; choose a new output")
    samples, rate = sf.read(source, dtype="float32", always_2d=True)
    if samples.shape[1] not in {1, 2} or not np.isfinite(samples).all():
        raise ValueError("Use finite mono/stereo samples")
    if not 8000 <= rate <= 48000 or not 10 <= len(samples) / rate <= 60:
        raise ValueError("Use a complete 10–60 second excerpt at 8–48 kHz")
    mono = samples.mean(axis=1)
    channel_power = float(np.mean(samples.astype("float64") ** 2))
    mono_power = float(np.mean(mono.astype("float64") ** 2))
    cancellation_db = (10 * math.log10(max(mono_power, 1e-30) / channel_power)
                       if channel_power else 0.0)
    if cancellation_db < -12:
        raise ValueError("Stereo cancels strongly in mono; this listener cannot assess this excerpt")
    if float(np.ptp(mono)) == 0:
        # Resampling a DC signal can create edge transients. Reject it first.
        raise ValueError("No varying audio above PCM16 resolution; choose an active excerpt")
    divisor = math.gcd(rate, MODEL_RATE)
    converted = resample_poly(mono, MODEL_RATE // divisor, rate // divisor)
    peak = float(np.max(np.abs(converted)))
    gain = min(1.0, (32767 / 32768) / peak) if peak else 1.0
    if float(np.ptp(converted * gain)) < 1 / 32768:
        raise ValueError("No varying audio above PCM16 resolution; choose an active excerpt")
    sf.write(destination, converted * gain, MODEL_RATE, subtype="PCM_16")
    return {
        "file": destination.name, "sha256": file_hash(destination),
        "source_sha256": file_hash(source), "sample_rate": MODEL_RATE, "channels": 1,
        "duration_seconds": len(converted) / MODEL_RATE,
        "mono_sum_energy_change_db": round(cancellation_db, 6),
        "protective_gain_db": round(20 * math.log10(gain), 6),
        "conversion": "Arithmetic stereo mean; scipy resample_poly to mono 16 kHz PCM16. "
                      "No EQ/compression; stereo and content above 8 kHz cannot be assessed.",
    }


class MusicFlamingoListener:
    """Experimental GPU backend. Explicit opt-in is required before loading CUDA."""

    def __init__(self, checkpoint: Path, *, allow_gpu: bool = False):
        if not allow_gpu:
            raise RuntimeError("GPU research listener requires explicit --allow-gpu. "
                               "Use the CPU audiobox engine while the restart is investigated.")
        import json

        import bitsandbytes as bnb
        import torch
        import transformers
        from transformers import (
            AutoProcessor,
            BitsAndBytesConfig,
            MusicFlamingoForConditionalGeneration,
        )

        checkpoint = checkpoint.resolve()
        if not checkpoint.is_dir():
            raise ValueError("Download the checkpoint before starting local inference")
        config = json.loads((checkpoint / "config.json").read_text(encoding="utf-8"))
        if config.get("model_type") != "musicflamingo":
            raise ValueError("This backend requires a Music Flamingo checkpoint")
        if not torch.cuda.is_available():
            raise RuntimeError("Local Music Flamingo requires the isolated CUDA Python environment")
        torch.set_num_threads(2)
        self.processor = AutoProcessor.from_pretrained(
            checkpoint, local_files_only=True, trust_remote_code=False,
        )
        quantization = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.float16,
            llm_int8_skip_modules=["model.audio_tower", "model.multi_modal_projector", "lm_head"],
        )
        self.model = MusicFlamingoForConditionalGeneration.from_pretrained(
            checkpoint, local_files_only=True, trust_remote_code=False, use_safetensors=True,
            quantization_config=quantization, dtype=torch.float16,
            device_map={"": 0}, attn_implementation="sdpa",
        ).eval()
        for component in (self.model.model.audio_tower, self.model.model.multi_modal_projector):
            if (any(isinstance(module, bnb.nn.Linear4bit) for module in component.modules())
                    or any(parameter.dtype != torch.float16 for parameter in component.parameters())):
                raise RuntimeError("Audio encoder/projector precision verification failed")
        self.runtime = {
            "engine": "music-flamingo-local", "model": MODEL_ID,
            "checkpoint": str(checkpoint), "config_sha256": file_hash(checkpoint / "config.json"),
            "torch_version": torch.__version__, "transformers_version": transformers.__version__,
            "gpu": torch.cuda.get_device_name(0), "decoder_quantization": "NF4 double quantization",
            "audio_encoder_dtype": "float16", "attention": "sdpa",
            "audio_encoder_precision_verified": True,
            "weights_bytes": self.model.get_memory_footprint(),
            "local_files_only": True, "remote_code": False,
            "license_scope": "NVIDIA OneWay Noncommercial License; research evaluation",
            "limitations": ["NF4 decoder quantization differs from NVIDIA's reference configuration."],
        }

    def observe(self, audio: Path, *, context: str = "") -> dict:
        import torch

        read_model_audio(audio)
        if len(context) > 2000:
            raise ValueError("Use at most 2000 characters of context")
        # The file path is consumed locally, never rendered as model text.
        conversation = [{"role": "user", "content": [
            {"type": "text", "text": SYSTEM_PROMPT + "\nEvaluate this excerpt. "
             "Times start at 0 seconds.\n" + ("Operator context: " + context
                                             if context else "No operator context.")},
            {"type": "audio", "path": str(audio.resolve())},
        ]}]
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        inputs = self.processor.apply_chat_template(
            conversation, tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors="pt",
        ).to("cuda:0")
        inputs["input_features"] = inputs["input_features"].to(torch.float16)
        with torch.inference_mode():
            outputs = self.model.generate(
                **inputs, max_new_tokens=500, do_sample=False, use_cache=True,
            )
        generated = outputs[:, inputs.input_ids.shape[1]:]
        text = self.processor.batch_decode(generated, skip_special_tokens=True)[0].strip()
        if not text:
            raise RuntimeError("Local model returned empty observations")
        truncated = generated.shape[1] >= 500
        return {
            "text": text, "model": MODEL_ID, "runtime": self.runtime,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_gpu_allocated_bytes": torch.cuda.max_memory_allocated(),
            "input_tokens": inputs.input_ids.shape[1], "output_tokens": generated.shape[1],
            "truncated": truncated, "generation": {
                "max_new_tokens": 500, "do_sample": False, "use_cache": True,
            }, "evidence_type": "unvalidated_audio_model_observations",
        }


def listen_local(source: Path, output: Path, *, listener: LocalListener,
                 start: float = 0, seconds: float = 30, context: str = "",
                 ffmpeg: Path) -> dict:
    """Read disk recordings; no sound device, playback, DAW control or cloud request."""
    source, output, ffmpeg = source.resolve(), output.resolve(), ffmpeg.resolve()
    if output.exists() or not ffmpeg.is_file() or len(context) > 2000:
        raise ValueError("Choose a new output, an existing FFmpeg binary and short context")
    excerpt = _prepare_excerpt(source, output, start, seconds)
    model_audio = prepare_model_audio(output / "excerpt-original.wav", output / "excerpt-model.wav")
    measurements = inspect_mix(output / "excerpt-original.wav", output / "measurements",
                               ffmpeg=ffmpeg)
    report = {
        "schema_version": 1, "kind": "wcfi_audio_listening_review",
        "created_at": datetime.now(UTC).isoformat(), "status": "prepared_local",
        "excerpt": excerpt, "model_audio": model_audio, "measurements": measurements,
        "requested_model": listener.runtime.get("model", MODEL_ID),
        "operator_context": context, "observations": None,
        "limitations": [
            "Model observations are fallible, experimental and unvalidated as mixing expertise.",
            "The model receives mono 16 kHz: no stereo judgment or frequencies above 8 kHz.",
            "Original excerpt measurements are separate evidence and not given to the model.",
            "No mix settings are changed; this is not a calibration/apply plan.",
            "No audio devices are opened and no recordings are uploaded.",
        ] + listener.runtime.get("limitations", []),
    }
    write_json(output / "request.json", {
        "mode": "local_inference", "runtime": listener.runtime,
        "prompt": None if listener.runtime.get("engine") == "audiobox-aesthetics-local"
        else SYSTEM_PROMPT,
        "operator_context": context, "model_audio": model_audio,
        "excerpt": excerpt, "audio_upload": False, "automatic_retries": 0,
    })
    try:
        report["observations"] = listener.observe(output / model_audio["file"], context=context)
        report["status"] = ("reviewed_truncated" if report["observations"]["truncated"]
                            else "reviewed")
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__
        write_json(output / "listen.json", report)
        (output / "report.html").write_text(render_report(report), encoding="utf-8")
        raise
    write_json(output / "listen.json", report)
    (output / "report.html").write_text(render_report(report), encoding="utf-8")
    return report
