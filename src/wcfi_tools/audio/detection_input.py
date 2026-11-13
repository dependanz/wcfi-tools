"""Input-only, bounded-memory adapters for passive acoustic detection.

Audio packages are optional and imported only when an input is opened. Neither
adapter plays audio. A microphone gap must reset temporal detector evidence;
PortAudio cannot always say how many samples were lost.
"""
from __future__ import annotations

import importlib
import math
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from tempfile import TemporaryDirectory
from typing import Any

INSTALL_HINT = 'Install audio support with: python -m pip install -e ".[audio-detect]"'


def _dependency(name):
    try:
        return importlib.import_module(name)
    except (ImportError, OSError) as exc:
        raise RuntimeError(f"Audio dependency {name!r} is unavailable. {INSTALL_HINT}") from exc


def _validate(channel, blocksize):
    if type(channel) is not int or channel < 1:
        raise ValueError("Audio channel must be a positive one-based integer")
    if type(blocksize) is not int or blocksize < 1:
        raise ValueError("Audio blocksize must be a positive integer")


@dataclass
class AudioBlock:
    samples: Any
    gap: bool = False


@dataclass
class AudioInput:
    info: dict
    _reader: Callable[[], AudioBlock | None]

    def read(self) -> AudioBlock | None:
        """Read selected-channel float32 PCM, or None at file EOF."""
        return self._reader()


def list_input_devices() -> list[dict]:
    """Enumerate recording devices without starting a microphone stream."""
    sd = _dependency("sounddevice")
    try:
        devices, hostapis = sd.query_devices(), sd.query_hostapis()
        default = sd.default.device[0]
    except sd.PortAudioError as exc:
        raise RuntimeError(f"Cannot enumerate audio inputs: {exc}") from exc
    return [
        {"index": int(item.get("index", index)), "name": item["name"],
         "hostapi": hostapis[item["hostapi"]]["name"],
         "default": int(item.get("index", index)) == default,
         "sample_rate": float(item["default_samplerate"]),
         "channels": int(item["max_input_channels"])}
        for index, item in enumerate(devices) if item["max_input_channels"] > 0
    ]


class _MicrophoneReader:
    def __init__(self, stream, blocks, timeout):
        self.stream, self.blocks, self.timeout = stream, blocks, timeout

    def __call__(self):
        deadline, gap = time.monotonic() + self.timeout, False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Microphone timed out: no fresh audio arrived")
            try:
                captured_at, block = self.blocks.get(timeout=min(.1, remaining))
            except Empty:
                if not self.stream.active:
                    raise RuntimeError("Microphone stream stopped or the device disconnected")
                continue
            # A paused/slow consumer must not mistake old audio for a live event.
            if time.monotonic() - captured_at > 1.0:
                gap = True
                continue
            block.gap = block.gap or gap
            return block


@contextmanager
def open_microphone(device=None, channel=1, sample_rate=None, blocksize=2048, timeout=3.0):
    """Open one input device; Ctrl+C/exception exit always closes its stream.

    The selected one-based channel is copied, never averaged with other mics.
    Platform/driver processing is unknown; PortAudio does not guarantee raw input.
    """
    _validate(channel, blocksize)
    if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Microphone timeout must be finite and positive")
    _dependency("numpy")
    sd = _dependency("sounddevice")
    if isinstance(device, str) and device.strip().lstrip("-").isdigit():
        device = int(device)
    try:
        selected = sd.query_devices(device, "input")
        index = selected.get("index")
        if index is None:  # Compatibility with older sounddevice device dictionaries.
            matches = [i for i, item in enumerate(sd.query_devices())
                       if item["name"] == selected["name"]
                       and item["hostapi"] == selected["hostapi"]]
            if len(matches) != 1:
                raise ValueError("Cannot uniquely resolve this input; select a device index")
            index = matches[0]
        rate = sample_rate if sample_rate is not None else selected["default_samplerate"]
        if (not isinstance(rate, (int, float)) or not math.isfinite(rate)
                or rate < 1 or int(rate) != rate):
            raise ValueError("Sample rate must be a finite positive integer")
        if channel > selected["max_input_channels"]:
            raise ValueError(f"Input has {selected['max_input_channels']} channels; requested {channel}")
        hostapi = sd.query_hostapis(selected["hostapi"])["name"]
        sd.check_input_settings(device=index, channels=channel, dtype="float32", samplerate=rate)
    except sd.PortAudioError as exc:
        raise RuntimeError(f"Cannot select microphone: {exc}. Run 'wcfi audio detect devices'.") from exc

    # Approximately half a second of backlog, never an unbounded audio recording.
    blocks = Queue(maxsize=max(1, int(.5 * rate / blocksize)))

    def callback(indata, frames, timing, status):
        block = AudioBlock(indata[:, channel - 1].copy(), gap=bool(status))
        item = (time.monotonic(), block)
        try:
            blocks.put_nowait(item)
        except Full:
            # Keep the newest block and mark the break in continuity explicitly.
            while True:
                try:
                    blocks.get_nowait()
                except Empty:
                    break
            block.gap = True
            blocks.put_nowait(item)

    stream = None
    try:
        try:
            stream = sd.InputStream(device=index, channels=channel, samplerate=rate,
                                    blocksize=blocksize, dtype="float32", callback=callback)
            stream.start()
        except sd.PortAudioError as exc:
            raise RuntimeError(f"Cannot open microphone: {exc}") from exc
        info = {"kind": "microphone", "name": selected["name"], "device": int(index),
                "hostapi": hostapi, "sample_rate": round(stream.samplerate),
                "channels": int(selected["max_input_channels"]), "channel": channel,
                "processing": "platform_processing_unknown",
                "time_basis": "captured_samples; gap lengths may be unknown"}
        yield AudioInput(info, _MicrophoneReader(stream, blocks, timeout))
    finally:
        if stream is not None:
            try:
                stream.abort(ignore_errors=True)
            finally:
                stream.close(ignore_errors=True)


@contextmanager
def open_audio_file(path, channel=1, blocksize=2048):
    """Read a file without playback, optionally decoding unsupported formats with FFmpeg.

    FFmpeg preserves sample rate and channel count in a temporary float WAV;
    decoding never mixes opposing stereo channels into silence. The temporary
    file is removed on normal exit, failure, or interruption.
    """
    _validate(channel, blocksize)
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Audio file does not exist: {path}")
    _dependency("numpy")
    sf = _dependency("soundfile")
    with ExitStack() as stack:
        decoder = "libsndfile"
        try:
            source = sf.SoundFile(path)
        except (RuntimeError, OSError) as original:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError(
                    f"Cannot decode {path.name}. Install FFmpeg on PATH for M4A/MP4 and other "
                    "formats, or supply WAV/FLAC audio."
                ) from original
            temporary = stack.enter_context(TemporaryDirectory(prefix="wcfi-howler-"))
            decoded = Path(temporary) / "decoded.wav"
            result = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
                 "-map", "0:a:0", "-vn", "-c:a", "pcm_f32le", "-rf64", "auto", str(decoded)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"FFmpeg could not decode {path.name}: {result.stderr[-1500:].strip()}")
            try:
                source = sf.SoundFile(decoded)
            except (RuntimeError, OSError) as exc:
                raise RuntimeError(f"Cannot read FFmpeg-decoded audio: {exc}") from exc
            decoder = "ffmpeg"
        source = stack.enter_context(source)
        if channel > source.channels:
            raise ValueError(f"File has {source.channels} channels; requested {channel}")
        info = {"kind": "file", "name": str(path), "sample_rate": int(source.samplerate),
                "channels": source.channels, "channel": channel, "frames": len(source),
                "duration_seconds": len(source) / source.samplerate, "decoder": decoder,
                "processing": "recording_processing_unknown", "time_basis": "file_samples"}

        def read():
            data = source.read(blocksize, dtype="float32", always_2d=True)
            if not len(data):
                return None
            return AudioBlock(data[:, channel - 1].copy())

        yield AudioInput(info, read)
