"""Offline harness for the exact WCFI Control Node DSP; no host or playback I/O.

The leveler lives in the native component. This module validates configuration,
streams aligned exported audio through it, and reports descriptive comparisons.
Energy activity is not a speech classifier or proof of musical correctness.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes as ct
import math
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class LevelerSettings:
    auto_enabled: bool = False
    trim_db: float = 0.0
    target_dbfs: float = -24.0
    max_boost_db: float = 12.0
    max_cut_db: float = 12.0
    activity_dbfs: float = -60.0

    def __post_init__(self):
        if type(self.auto_enabled) is not bool:
            raise ValueError("auto_enabled must be boolean")
        limits = {"trim_db": (-24, 24), "target_dbfs": (-36, -12),
                  "max_boost_db": (0, 24), "max_cut_db": (0, 24),
                  "activity_dbfs": (-90, -24)}
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high):
                raise ValueError(f"Invalid {name}")

    def to_dict(self):
        return asdict(self)


class BlockProcessor(Protocol):
    """Dependency boundary: native bridge in production; a fake in tests."""
    def process(self, block): ...
    def status(self) -> dict: ...
    def close(self) -> None: ...


class NativeConfig(ct.Structure):
    _fields_ = [("version", ct.c_uint32), ("auto_enabled", ct.c_uint32)] + [
        (name, ct.c_double) for name in
        ("trim_db", "target_dbfs", "max_boost_db", "max_cut_db", "activity_dbfs")]


class NativeStatus(ct.Structure):
    _fields_ = [(name, ct.c_uint32) for name in ("version", "activity", "state", "reserved")] + [
        (name, ct.c_double) for name in ("input_rms_dbfs", "input_peak_dbfs", "auto_gain_db",
            "applied_gain_db", "target_gain_db", "guard_attenuation_db")] + [
        (name, ct.c_uint64) for name in ("samples_processed", "nonfinite_samples", "headroom_restraints")]


class NativeControlDsp:
    """Explicit handle for ABIv1; constructor loads only the supplied local DLL."""

    def __init__(self, dll_path, settings=LevelerSettings(), *, rate=48000, channels=2):
        if not isinstance(settings, LevelerSettings):
            raise TypeError("Expected validated LevelerSettings")
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or not 8000 <= rate <= 384000
                or type(channels) is not int or channels not in (1, 2)):
            raise ValueError("Invalid native stream format")
        if ct.sizeof(NativeConfig) != 48 or ct.sizeof(NativeStatus) != 88:
            raise RuntimeError("Unexpected native ABI structure layout")
        self._lib = ct.CDLL(str(Path(dll_path).resolve(strict=True)))
        self._channels = channels
        self._handle = None
        lib = self._lib
        lib.WcfiControlCreate.argtypes = [ct.c_double, ct.c_uint32]
        lib.WcfiControlCreate.restype = ct.c_void_p
        lib.WcfiControlConfigure.argtypes = [ct.c_void_p, ct.POINTER(NativeConfig)]
        lib.WcfiControlConfigure.restype = ct.c_int
        lib.WcfiControlProcess.argtypes = [ct.c_void_p, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float), ct.c_uint32]
        lib.WcfiControlProcess.restype = ct.c_int
        lib.WcfiControlGetStatus.argtypes = [ct.c_void_p, ct.POINTER(NativeStatus)]
        lib.WcfiControlGetStatus.restype = ct.c_int
        lib.WcfiControlDestroy.argtypes = [ct.c_void_p]
        lib.WcfiControlDestroy.restype = None
        self._handle = lib.WcfiControlCreate(rate, channels)
        if not self._handle:
            raise RuntimeError("Native processor creation failed")
        try:
            self.configure(settings)
        except Exception:
            self.close()
            raise
        if self.status()["version"] != 1:
            self.close()
            raise RuntimeError("Native ABI version differs")

    def configure(self, settings):
        """Apply validated settings at the next process call without resetting DSP."""
        if not isinstance(settings, LevelerSettings):
            raise TypeError("Expected validated LevelerSettings")
        if self._handle is None:
            raise RuntimeError("Native processor is closed")
        config = NativeConfig(version=1, **{**settings.to_dict(), "auto_enabled": int(settings.auto_enabled)})
        if self._lib.WcfiControlConfigure(self._handle, ct.byref(config)) != 0:
            raise RuntimeError("Native processor rejected configuration")

    def process(self, block):
        import numpy as np
        if self._handle is None:
            raise RuntimeError("Native processor is closed")
        values = np.asarray(block)
        if (values.ndim != 2 or values.shape[1] != self._channels
                or not 0 < values.shape[0] <= 1048576 or not np.isfinite(values).all()):
            raise ValueError("Expected finite nonempty interleaved audio block")
        values = np.ascontiguousarray(values, dtype=np.float32)
        if not np.isfinite(values).all():
            raise ValueError("Audio exceeds float32 range")
        output = np.empty_like(values)
        if self._lib.WcfiControlProcess(self._handle,
                values.ctypes.data_as(ct.POINTER(ct.c_float)),
                output.ctypes.data_as(ct.POINTER(ct.c_float)), len(values)) != 0:
            raise RuntimeError("Native processing failed")
        if not np.isfinite(output).all():
            raise RuntimeError("Native processing returned nonfinite samples")
        return output

    def status(self):
        if self._handle is None:
            raise RuntimeError("Native processor is closed")
        status = NativeStatus()
        if self._lib.WcfiControlGetStatus(self._handle, ct.byref(status)) != 0:
            raise RuntimeError("Native status failed")
        return {name: getattr(status, name) for name, _ in NativeStatus._fields_}

    def close(self):
        if self._handle is not None:
            self._lib.WcfiControlDestroy(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def dbfs(power_or_peak: float, *, power: bool = False):
    if not math.isfinite(power_or_peak) or power_or_peak < 0:
        raise ValueError("Invalid power or peak")
    return (10 if power else 20) * math.log10(power_or_peak) if power_or_peak else None


def quantiles(values):
    """Linear interpolation, kept separate from the DSP's own control logic."""
    if not values:
        return None
    ordered = sorted(values)
    result = {}
    for name, fraction in (("p10", .1), ("p50", .5), ("p90", .9)):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        result[name] = ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])
    return result


def summarize_windows(windows, start, end, *, target_dbfs=-26., activity_dbfs=-55.):
    """Describe a fixed interval; do not infer speech from the activity mask."""
    selected = [row for row in windows if start <= row["start_seconds"] < end]
    active = [row for row in selected if row["input_rms_dbfs"] is not None
              and row["input_rms_dbfs"] > activity_dbfs]
    overlap = [row for row in active if row["instruments_rms_dbfs"] is not None
               and row["instruments_rms_dbfs"] > -60.]
    metrics = {"window_count": len(selected), "activity_proxy_windows": len(active),
               "overlapping_music_activity_windows": len(overlap),
               "activity_is_semantic_speech": False}
    for prefix in ("input", "fixed", "candidate"):
        levels = [row[prefix + "_rms_dbfs"] for row in active
                  if row[prefix + "_rms_dbfs"] is not None]
        errors = [abs(level - target_dbfs) for level in levels]
        ratios = [row[prefix + "_rms_dbfs"] - row["instruments_rms_dbfs"] for row in overlap
                  if row[prefix + "_rms_dbfs"] is not None]
        metrics[prefix] = {"active_rms_dbfs": quantiles(levels),
                           "absolute_target_error_db": quantiles(errors),
                           "lapel_minus_instruments_db": quantiles(ratios)}
    return metrics


def verify_aligned_headers(paths):
    """Read only named export headers, including stereo sample-frame alignment."""
    import soundfile as sf
    descriptions = {}
    for role, path in paths.items():
        info = sf.info(Path(path))
        if info.frames <= 0 or info.samplerate != 48000 or info.channels != 2:
            raise ValueError("Expected nonempty48kHzstereo export for " + role)
        descriptions[role] = {"frames": info.frames, "sample_rate": info.samplerate,
                              "channels": info.channels, "subtype": info.subtype}
    if len({(x["frames"], x["sample_rate"], x["channels"]) for x in descriptions.values()}) != 1:
        raise ValueError("Export alignment differs")
    return descriptions


def replacement_mix(master, old_lapel, candidate, *, additive_path_verified):
    """Replace only the verified direct additive LAPEL contribution."""
    import numpy as np
    if additive_path_verified is not True:
        raise ValueError("An independently verified additive LAPEL path is required")
    if master.shape != old_lapel.shape or master.shape != candidate.shape:
        raise ValueError("Replacement shapes differ")
    if not all(np.isfinite(value).all() for value in (master, old_lapel, candidate)):
        raise ValueError("Nonfinite mix input")
    return (master.astype(np.float64) - old_lapel + candidate).astype(np.float32)
