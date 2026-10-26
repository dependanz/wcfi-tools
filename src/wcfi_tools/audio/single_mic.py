"""Offline access to the RED plugin's exact native calibration/hold engine."""
from dataclasses import dataclass
import ctypes as ct
import math
from pathlib import Path

from .lapel_automix import LevelerSettings, NativeConfig, NativeStatus


@dataclass(frozen=True)
class MicSettings(LevelerSettings):
    target_dbfs: float = -26.
    max_boost_db: float = 6.
    max_cut_db: float = 6.
    activity_dbfs: float = -55.

    def __post_init__(self):
        super().__post_init__()
        if not -6 <= self.trim_db <= 6 or self.max_boost_db > 6 or self.max_cut_db > 6:
            raise ValueError("RED single-mic gain is bounded to +/-6 dB")


class NativeMicDsp:
    """Loads only an explicitly supplied workspace DLL; no host/audio device I/O."""
    def __init__(self, dll_path, settings=MicSettings(), *, rate=48000, channels=2):
        if not isinstance(settings, MicSettings):
            raise TypeError("Expected MicSettings")
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or not 8000 <= rate <= 384000
                or type(channels) is not int or channels not in (1, 2)):
            raise ValueError("Invalid native stream format")
        if ct.sizeof(NativeConfig) != 48 or ct.sizeof(NativeStatus) != 88:
            raise RuntimeError("Unexpected native ABI layout")
        self._lib = ct.CDLL(str(Path(dll_path).resolve(strict=True)))
        self._channels, self._handle = channels, None
        lib = self._lib
        lib.WcfiMicCreate.argtypes = [ct.c_double, ct.c_uint32]
        lib.WcfiMicCreate.restype = ct.c_void_p
        lib.WcfiMicConfigure.argtypes = [ct.c_void_p, ct.POINTER(NativeConfig)]
        lib.WcfiMicConfigure.restype = ct.c_int
        lib.WcfiMicProcess.argtypes = [ct.c_void_p, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float), ct.c_uint32]
        lib.WcfiMicProcess.restype = ct.c_int
        lib.WcfiMicGetStatus.argtypes = [ct.c_void_p, ct.POINTER(NativeStatus)]
        lib.WcfiMicGetStatus.restype = ct.c_int
        lib.WcfiMicDestroy.argtypes = [ct.c_void_p]
        lib.WcfiMicDestroy.restype = None
        self._handle = lib.WcfiMicCreate(rate, channels)
        if not self._handle:
            raise RuntimeError("Native processor creation failed")
        try:
            self.configure(settings)
            if self.status()["version"] != 1:
                raise RuntimeError("Native RED ABI version differs")
        except Exception:
            self.close()
            raise

    def configure(self, settings):
        if not isinstance(settings, MicSettings):
            raise TypeError("Expected MicSettings")
        if self._handle is None:
            raise RuntimeError("Processor is closed")
        config = NativeConfig(version=1, **{**settings.to_dict(), "auto_enabled": int(settings.auto_enabled)})
        if self._lib.WcfiMicConfigure(self._handle, ct.byref(config)) != 0:
            raise RuntimeError("Native processor rejected configuration")

    def process(self, block):
        import numpy as np
        if self._handle is None:
            raise RuntimeError("Processor is closed")
        values = np.asarray(block)
        if (values.ndim != 2 or values.shape[1] != self._channels
                or not 0 < len(values) <= 1048576 or not np.isfinite(values).all()):
            raise ValueError("Expected finite nonempty interleaved audio")
        values = np.ascontiguousarray(values, dtype=np.float32)
        if not np.isfinite(values).all():
            raise ValueError("Audio exceeds float32 range")
        output = np.empty_like(values)
        if self._lib.WcfiMicProcess(self._handle,
                values.ctypes.data_as(ct.POINTER(ct.c_float)),
                output.ctypes.data_as(ct.POINTER(ct.c_float)), len(values)) != 0:
            raise RuntimeError("Native processing failed")
        if not np.isfinite(output).all():
            raise RuntimeError("Native output is nonfinite")
        return output

    def status(self):
        if self._handle is None:
            raise RuntimeError("Processor is closed")
        status = NativeStatus()
        if self._lib.WcfiMicGetStatus(self._handle, ct.byref(status)) != 0:
            raise RuntimeError("Native status failed")
        return {name: getattr(status, name) for name, _ in NativeStatus._fields_}

    def close(self):
        if self._handle is not None:
            self._lib.WcfiMicDestroy(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
