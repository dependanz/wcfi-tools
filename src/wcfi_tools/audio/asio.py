"""Windows native bridge. DSP, Python and file IO never run in the ASIO callback."""
from __future__ import annotations

import ctypes
import math
import sys
import time
from pathlib import Path

from .emulator import InputBank, load_profile

CHANNELS, RATE = 32, 48000
CLSID = "{F9EB9932-2A49-48D7-9893-7D4FB48B9789}"


class _Status(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "version", "rate", "channels", "capacity", "blocksize", "running", "host_pid",
        "generation")] + [(name, ctypes.c_uint64) for name in (
        "input_queued", "output_queued", "underruns", "output_drops", "late_callbacks",
        "callbacks", "sample_position")] + [
        ("service_pid", ctypes.c_uint32), ("service_active", ctypes.c_uint32),
        ("heartbeat_ms", ctypes.c_uint64)]


def registration() -> dict:
    if sys.platform != "win32":
        return {"registered": False, "platform": sys.platform}
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ASIO\WCFI Emulator",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            clsid = winreg.QueryValueEx(key, "CLSID")[0]
        if clsid.upper() != CLSID:
            return {"registered": False, "error": "ASIO name has a different CLSID"}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            rf"SOFTWARE\Classes\CLSID\{CLSID}\InprocServer32",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            path = winreg.QueryValueEx(key, None)[0]
        return {"registered": True, "dll": path, "clsid": clsid}
    except FileNotFoundError:
        return {"registered": False}


def default_dll() -> Path:
    registered = registration()
    if registered.get("registered"):
        return Path(registered["dll"])
    return Path(__file__).resolve().parents[3] / "native" / "wcfi_asio" / "build" / "WcfiAsio.dll"


class NativeBridge:
    """Exclusive single producer/consumer claims are enforced across processes in C."""
    def __init__(self, dll: Path | None = None, *, roles=0):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("WCFI ASIO currently requires 64-bit Python on Windows x64")
        self.path = (dll or default_dll()).resolve(strict=True)
        self.library = ctypes.CDLL(str(self.path))
        self.library.WcfiOpenBridge.argtypes = [ctypes.c_uint32]
        self.library.WcfiOpenBridge.restype = ctypes.c_void_p
        self.library.WcfiCloseBridge.argtypes = [ctypes.c_void_p]
        self.library.WcfiCloseBridge.restype = None
        for name in ("WcfiFeed", "WcfiReadOutput"):
            fn = getattr(self.library, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_uint32]
            fn.restype = ctypes.c_int
        self.library.WcfiGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Status)]
        self.library.WcfiGetStatus.restype = ctypes.c_int
        self.library.WcfiDiscardOutput.argtypes = [ctypes.c_void_p]
        self.library.WcfiDiscardOutput.restype = None
        self.handle = self.library.WcfiOpenBridge(roles)
        if not self.handle:
            raise RuntimeError("No open WCFI ASIO device, or the requested feed/capture role "
                               "is already in use. Select WCFI Emulator in the DAW first.")
        status = self.status()
        self.generation, self.host_pid = status["generation"], status["host_pid"]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        self.kernel = kernel
        if not self.host_pid and roles == 0:
            self.host_process = None
            return
        self.host_process = kernel.OpenProcess(0x00100000, False, self.host_pid)  # SYNCHRONIZE
        if not self.host_process:
            self.close()
            raise RuntimeError("The DAW host exited, or its process cannot be inspected")

    def status(self):
        result = _Status()
        if not self.handle or not self.library.WcfiGetStatus(self.handle, ctypes.byref(result)):
            raise RuntimeError("Cannot read the native bridge")
        report = {name: getattr(result, name) for name, _ in _Status._fields_}
        if getattr(self, "host_process", None):
            report["host_alive"] = self.kernel.WaitForSingleObject(self.host_process, 0) == 258
        return report

    def check_session(self):
        status = self.status()
        if not status.get("host_alive", False):
            raise RuntimeError("The DAW host has exited; restart the emulator/capture")
        if status["host_pid"] != self.host_pid or status["generation"] != self.generation:
            raise RuntimeError("The DAW reopened its audio device; restart the emulator/capture")
        if not status["running"]:
            raise RuntimeError("The WCFI audio device is stopped. Enable the DAW audio engine; "
                               "the arrangement transport does not need to play.")
        return status

    def feed(self, samples):
        import numpy as np

        block = np.ascontiguousarray(samples, dtype=np.float32)
        if block.ndim != 2 or block.shape[1] != CHANNELS or len(block) > 32768:
            raise ValueError("Feed requires at most 32768 frames with exactly 32 channels")
        result = self.library.WcfiFeed(self.handle,
                                      block.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(block))
        if result < 0:
            raise ValueError("Native feed rejected invalid samples, role, or device lifecycle")
        return result

    def read(self, frames=4096):
        import numpy as np

        if not isinstance(frames, int) or not 1 <= frames <= 32768:
            raise ValueError("Read 1–32768 frames")
        block = np.empty((frames, CHANNELS), dtype=np.float32)
        result = self.library.WcfiReadOutput(self.handle,
                                            block.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), frames)
        if result < 0:
            raise RuntimeError("Native capture rejected this role or device lifecycle")
        return block[:result]

    def discard_output(self):
        self.library.WcfiDiscardOutput(self.handle)

    def close(self):
        if getattr(self, "host_process", None):
            self.kernel.CloseHandle(self.host_process)
            self.host_process = None
        if self.handle:
            self.library.WcfiCloseBridge(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class NativeService:
    """Own driver availability independently of the DAW and the desktop window."""
    def __init__(self, dll: Path | None = None):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("WCFI ASIO requires 64-bit Windows Python")
        self.path = (dll or default_dll()).resolve(strict=True)
        self.library = ctypes.CDLL(str(self.path))
        if not hasattr(self.library, "WcfiOpenService"):
            raise RuntimeError("The ASIO driver needs the v2 service update; see docs/audio-emulator.md")
        self.library.WcfiOpenService.argtypes = []
        self.library.WcfiOpenService.restype = ctypes.c_void_p
        self.library.WcfiServiceActive.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.library.WcfiServiceActive.restype = ctypes.c_int
        self.library.WcfiHeartbeat.argtypes = [ctypes.c_void_p]
        self.library.WcfiHeartbeat.restype = None
        self.library.WcfiCloseBridge.argtypes = [ctypes.c_void_p]
        self.library.WcfiCloseBridge.restype = None
        self.library.WcfiGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Status)]
        self.library.WcfiGetStatus.restype = ctypes.c_int
        self.handle = self.library.WcfiOpenService()
        if not self.handle:
            raise RuntimeError("An emulator service already owns this driver")

    def set_active(self, active: bool):
        if not self.library.WcfiServiceActive(self.handle, int(active)):
            raise RuntimeError("Cannot change driver availability")

    def pulse(self):
        self.library.WcfiHeartbeat(self.handle)

    def status(self):
        result = _Status()
        if not self.library.WcfiGetStatus(self.handle, ctypes.byref(result)):
            raise RuntimeError("Cannot read service status")
        return {name: getattr(result, name) for name, _ in _Status._fields_}

    def close(self):
        if self.handle:
            self.library.WcfiCloseBridge(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def mapped_block(profile, stems, position, frames):
    """Bounded 32-port interleave; looping is sample aligned across all sources."""
    import numpy as np

    result = np.zeros((frames, CHANNELS), dtype=np.float32)
    length, written = len(stems[0]), 0
    while written < frames:
        if position >= length and not profile["loop"]:
            break
        start = position % length
        take = min(frames - written, length - start)
        for track, stem in zip(profile["channels"], stems, strict=True):
            ports = [port - 1 for port in track["inputs"]]
            result[written:written + take, ports] = stem[start:start + take]
        position += take
        written += take
    return result


def stream(profile_path: Path, *, scenario="baseline", dll: Path | None = None,
           seconds: float | None = None, ready=None):
    profile = load_profile(profile_path)
    if profile["sample_rate"] != RATE or profile["input_count"] > CHANNELS:
        raise ValueError("This driver requires 48 kHz and at most 32 input ports")
    if seconds is not None and (not math.isfinite(seconds) or not 0 < seconds <= 86400):
        raise ValueError("Stream seconds must be greater than zero and at most 86400")
    bank = InputBank(profile)
    stems, truth = bank.prepare(scenario)
    limit = round(seconds * RATE) if seconds is not None else None
    if not profile["loop"]:
        limit = min(limit, bank.frames) if limit is not None else bank.frames
    sent, interrupted = 0, False
    with NativeBridge(dll, roles=1) as bridge:
        initial = bridge.check_session()
        if initial["input_queued"]:
            raise RuntimeError("Previous input samples are still draining; retry shortly")
        if ready:
            ready(initial)
        try:
            while limit is None or sent < limit:
                status = bridge.check_session()
                # Keep no more than two ASIO blocks queued. The native cycle always
                # runs independently and emits explicit silence on starvation.
                ahead = max(2048, 2 * status["blocksize"])
                count = min(1024, max(0, ahead - status["input_queued"]))
                if limit is not None:
                    count = min(count, limit - sent)
                if count:
                    block = mapped_block(profile, stems, sent, count)
                    sent += bridge.feed(block)
                else:
                    time.sleep(.002)
        except KeyboardInterrupt:
            interrupted = True
        deadline = time.monotonic() + 1
        while bridge.status()["running"] and bridge.status()["input_queued"] and time.monotonic() < deadline:
            time.sleep(.002)
        final = bridge.status()
    return {"kind": "wcfi_asio_emulator_run", "scenario": scenario,
            "frames_enqueued": sent, "seconds_enqueued": sent / RATE,
            "interrupted": interrupted, "initial": initial, "final": final, "sources": truth,
            "note": "Audio transport only; no mix controls, model calls, or uploads."}


def capture(output: Path, *, seconds=10.0, channels=(23, 24), dll: Path | None = None):
    """Explicit local capture of DAW outputs, independent of arrangement transport."""
    import numpy as np
    import soundfile as sf

    if not math.isfinite(seconds) or not .1 <= seconds <= 60:
        raise ValueError("Capture 0.1–60 seconds")
    if (not 1 <= len(channels) <= CHANNELS or len(set(channels)) != len(channels)
            or any(type(ch) is not int or not 1 <= ch <= CHANNELS for ch in channels)):
        raise ValueError("Select distinct output channels numbered 1–32")
    if output.suffix.lower() != ".wav":
        raise ValueError("Capture output must be a .wav file")
    if output.exists():
        raise ValueError("Choose a new output file; existing audio is never overwritten")
    remaining = round(seconds * RATE)
    frames = remaining
    with NativeBridge(dll, roles=2) as bridge:
        bridge.check_session()
        bridge.discard_output()
        initial = bridge.status()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as file, sf.SoundFile(
                file, mode="w", samplerate=RATE, channels=len(channels),
                subtype="FLOAT", format="WAV", closefd=False) as writer:
            timeout = time.monotonic() + seconds + 5
            while remaining:
                bridge.check_session()
                block = bridge.read(min(4096, remaining))
                if len(block):
                    selected = block[:, [ch - 1 for ch in channels]]
                    if not np.isfinite(selected).all():
                        raise ValueError("DAW output contains non-finite samples")
                    writer.write(selected)
                    remaining -= len(block)
                elif time.monotonic() > timeout:
                    raise RuntimeError("Timed out waiting for DAW output")
                else:
                    time.sleep(.002)
        final = bridge.status()
    return {"kind": "wcfi_asio_capture", "output": str(output.resolve()),
            "sample_rate": RATE, "frames": frames, "channels": list(channels),
            "output_drops_during_capture": final["output_drops"] - initial["output_drops"],
            "initial": initial, "final": final,
            "note": "Local DAW output capture. No export, model call, or upload."}
