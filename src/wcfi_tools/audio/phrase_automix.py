"""Offline phrase-policy adapter around the unchanged native gain/peak stage.

No host, playback device, network or installed-plugin access. Completed raw
100ms bins control following samples. This is not a native-policy parity claim.
"""
from __future__ import annotations

import math
from copy import deepcopy

from .lapel_automix import LevelerSettings, NativeControlDsp
from .phrase_gain import PhraseGain, PhraseSettings


class PhraseControlDsp:
    def __init__(self, dll_path, settings=PhraseSettings(), *, rate=48000, channels=2,
                 native_factory=NativeControlDsp):
        import numpy as np
        if not isinstance(settings, PhraseSettings):
            raise TypeError("Expected PhraseSettings")
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or not 8000 <= rate <= 384000
                or type(channels) is not int or channels not in (1, 2)):
            raise ValueError("Invalid native stream format")
        frames = rate * settings.bin_seconds
        if abs(frames - round(frames)) > 1e-9:
            raise ValueError("Policy bins must contain a whole number of sample frames")
        self.settings, self.rate, self.channels = settings, rate, channels
        self.policy = PhraseGain(settings)
        self._measure = np.empty((round(frames), channels), dtype=np.float32)
        self._used = self._bin = self._processed = 0
        self._decisions = []
        self._closed = False
        self._native = native_factory(dll_path, self._native_settings(0.),
                                      rate=rate, channels=channels)

    def _native_settings(self, offset):
        # Native auto correction is bounded to zero. Auto stays enabled solely
        # to reuse the original linked sample-peak guard and gain smoothing.
        return LevelerSettings(auto_enabled=True,
            trim_db=max(-24., min(24., self.settings.trim_db + offset)),
            target_dbfs=self.settings.target_dbfs, max_boost_db=0., max_cut_db=0.,
            activity_dbfs=self.settings.activity_dbfs)

    def process(self, block):
        import numpy as np
        if self._closed:
            raise RuntimeError("Processor is closed")
        values = np.asarray(block)
        if (values.ndim != 2 or values.shape[1] != self.channels
                or not 0 < len(values) <= 1048576 or not np.isfinite(values).all()):
            raise ValueError("Expected finite nonempty interleaved audio block")
        values = np.ascontiguousarray(values, dtype=np.float32)
        if not np.isfinite(values).all():
            raise ValueError("Audio exceeds float32 range")
        output = np.empty_like(values)
        self._decisions = []
        start = 0
        while start < len(values):
            count = min(len(values) - start, len(self._measure) - self._used)
            part = values[start:start + count]
            output[start:start + count] = self._native.process(part)
            self._measure[self._used:self._used + count] = part
            self._used += count
            start += count
            self._processed += count
            if self._used == len(self._measure):
                # Compute over the same complete bin regardless of caller block
                # boundaries. A partial final bin never becomes fake evidence.
                power = float(np.mean(self._measure.astype(np.float64) ** 2))
                level = 10. * math.log10(power) if power else None
                decision = self.policy.observe(level, bin_index=self._bin)
                self._decisions.append(decision)
                self._native.configure(self._native_settings(self.policy.offset_db))
                self._bin += 1
                self._used = 0
        return output

    @property
    def decisions(self):
        """Completed decisions from the most recent process call, copied."""
        return deepcopy(self._decisions)

    def status(self):
        native = self._native.status()
        return {"samples_processed": self._processed, "completed_bins": self._bin,
                "partial_bin_frames": self._used, "policy_offset_db": self.policy.offset_db,
                "native": native}

    def close(self):
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
