"""Small causal detector for persistent, isolated tonal interference.

This is an experimental spectral/temporal heuristic, not NINOS2-T or a trained
classifier. A growing tone is labelled ``feedback_like``; a stationary tone is
``sustained_tone``. Neither label establishes its physical cause or intent.
Harmonic-rich tones are conservatively rejected, which can also miss distorted
feedback. Input is one finite, floating-point PCM channel, nominally [-1, 1].
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class _Track:
    identifier: int
    start: float
    last: float
    frequency: float
    prominence: float
    peak: float
    history: deque = field(default_factory=lambda: deque(maxlen=64))
    announced: bool = False
    last_notice: float = 0.0
    grew: bool = False
    growth: float = 0.0


class HowlerDetector:
    """Feed arbitrary mono chunks; receive bounded-rate start/update/end events.

    Times use the input sample clock. The first/last times describe analysis
    windows containing evidence, so onset/offset precision is limited by the
    window duration. ``score`` is a heuristic strength, not a probability.
    No microphone, output device, filesystem, or network operations occur here.
    """

    def __init__(self, sample_rate: int, *, min_frequency: float = 100.0,
                 max_frequency: float = 10000.0, min_duration: float = 0.5,
                 threshold_db: float = 15.0):
        if (isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer))
                or not 4000 <= sample_rate <= 384000):
            raise ValueError("sample_rate must be an integer between 4000 and 384000 Hz")
        options = (min_frequency, max_frequency, min_duration, threshold_db)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) for v in options):
            raise ValueError("Detector options must be finite numbers")
        if not 20 <= min_frequency < max_frequency:
            raise ValueError("Frequency range must satisfy 20 <= minimum < maximum")
        if not 0.1 <= min_duration <= 10:
            raise ValueError("min_duration must be between 0.1 and 10 seconds")
        if not 3 <= threshold_db <= 80:
            raise ValueError("threshold_db must be between 3 and 80 dB")
        self.sample_rate = int(sample_rate)
        self.window_size = 2 ** math.ceil(math.log2(self.sample_rate * 0.08))
        self.hop_size = round(self.sample_rate * 0.02)
        self.window_seconds = self.window_size / self.sample_rate
        self.hop_seconds = self.hop_size / self.sample_rate
        self.bin_hz = self.sample_rate / self.window_size
        self.min_frequency = float(min_frequency)
        self.max_frequency = min(float(max_frequency), self.sample_rate / 2 - self.bin_hz)
        if self.min_frequency >= self.max_frequency:
            raise ValueError("Frequency range lies above the input Nyquist frequency")
        self.min_duration = float(min_duration)
        self.threshold_db = float(threshold_db)
        self._window = np.hanning(self.window_size)
        self._window_sum = float(self._window.sum())
        self._frequencies = np.fft.rfftfreq(self.window_size, 1 / self.sample_rate)
        self._buffer = np.empty(self.window_size, dtype=np.float64)
        self._filled = 0
        self._frame_start = 0
        self.samples_received = 0
        self._offset = 0.0
        self._next_id = 1
        self._tracks: list[_Track] = []
        self._finished = False

    @property
    def processed_seconds(self) -> float:
        """Input duration, including a final incomplete analysis window."""
        return self._offset + self.samples_received / self.sample_rate

    @property
    def configuration(self) -> dict:
        return {
            "algorithm": "spectral_temporal_howler_v1",
            "sample_rate": self.sample_rate,
            "min_frequency_hz": self.min_frequency,
            "max_frequency_hz": self.max_frequency,
            "min_duration_seconds": self.min_duration,
            "threshold_db": self.threshold_db,
            "window_seconds": self.window_seconds,
            "hop_seconds": self.hop_seconds,
            "score_is_probability": False,
        }

    def feed(self, mono_samples) -> list[dict]:
        """Consume mono PCM without depending on the caller's chunk boundaries."""
        if self._finished:
            raise ValueError("Detector is finished; call reset() before feeding again")
        samples = np.asarray(mono_samples)
        if samples.ndim != 1 or samples.dtype.kind not in "fiu":
            raise ValueError("Expected a one-dimensional numeric PCM channel")
        if not np.isfinite(samples).all():
            raise ValueError("Audio contains nonfinite samples")
        # Integer PCM has different full-scale conventions; require explicit
        # conversion rather than silently reporting incorrect dBFS values.
        if samples.dtype.kind in "iu" and samples.size:
            raise ValueError("Convert integer PCM to floating point in [-1, 1] first")
        self.samples_received += len(samples)
        events = []
        position = 0
        while position < len(samples):
            count = min(self.window_size - self._filled, len(samples) - position)
            self._buffer[self._filled:self._filled + count] = samples[position:position + count]
            self._filled += count
            position += count
            if self._filled == self.window_size:
                start = self._offset + self._frame_start / self.sample_rate
                events.extend(self._analyze(self._buffer, start))
                remaining = self.window_size - self.hop_size
                self._buffer[:remaining] = self._buffer[self.hop_size:].copy()
                self._filled = remaining
                self._frame_start += self.hop_size
        return events

    def finish(self) -> list[dict]:
        """Close active events at their last evidence window; do not pad silence."""
        events = [self._event(track, "end") for track in self._tracks if track.announced]
        self._tracks.clear()
        self._finished = True
        return events

    def reset(self, *, start_seconds: float = 0.0) -> list[dict]:
        """End events and start an independent segment after an input gap.

        The caller supplies the new segment's sample-clock offset. Event IDs
        remain unique across resets. Missing audio is never treated as silence.
        """
        if not isinstance(start_seconds, (int, float)) or not math.isfinite(start_seconds):
            raise ValueError("start_seconds must be a finite nonnegative number")
        if start_seconds < 0:
            raise ValueError("start_seconds must be nonnegative")
        events = self.finish()
        self._filled = self._frame_start = self.samples_received = 0
        self._offset = float(start_seconds)
        self._finished = False
        return events

    def _candidates(self, samples) -> list[dict]:
        spectrum = np.fft.rfft((samples - samples.mean()) * self._window)
        power = np.abs(spectrum) ** 2
        total = float(power[1:].sum())
        if total <= 1e-18:
            return []
        maxima = np.flatnonzero((power[1:-1] > power[:-2])
                                & (power[1:-1] >= power[2:])) + 1
        # Include out-of-band fundamentals when rejecting harmonic instruments.
        family_peaks = maxima[(self._frequencies[maxima] >= 20)
                              & (power[maxima] >= power.max() * 0.005)]
        family_peaks = family_peaks[np.argsort(power[family_peaks])[-32:]]
        maxima = maxima[(self._frequencies[maxima] >= self.min_frequency)
                        & (self._frequencies[maxima] <= self.max_frequency)]
        maxima = maxima[np.argsort(power[maxima])[-16:]][::-1]
        results = []
        for index in maxima:
            low, high = max(1, index - 16), min(len(power) - 1, index + 17)
            neighbors = np.r_[power[low:max(low, index - 3)], power[index + 4:high]]
            if not len(neighbors):
                continue
            floor = max(float(np.median(neighbors)), total / len(power) * 0.05, 1e-24)
            prominence = float(10 * np.log10(max(power[index], 1e-24) / floor))
            fraction = float(power[max(1, index - 1):index + 2].sum() / total)
            if prominence < self.threshold_db or fraction < 0.08:
                continue
            if self._harmonic_family(index, power, family_peaks):
                continue
            # Parabolic interpolation estimates a tone's frequency between bins.
            logs = np.log(np.maximum(power[index - 1:index + 2], 1e-30))
            denominator = logs[0] - 2 * logs[1] + logs[2]
            delta = float(np.clip(0.5 * (logs[0] - logs[2]) / denominator, -.5, .5))
            frequency = (index + delta) * self.bin_hz
            peak_log = logs[1] - 0.25 * (logs[0] - logs[2]) * delta
            peak_dbfs = float(20 * np.log10(2 / self._window_sum)
                              + 10 / np.log(10) * peak_log)
            # Below this floor numerical or inaudible PCM residue is unhelpful.
            if peak_dbfs < -100:
                continue
            results.append({"frequency": frequency, "prominence": prominence,
                            "peak": peak_dbfs})
            if len(results) == 8:
                break
        return results

    def _harmonic_family(self, index, power, peaks) -> bool:
        frequency = self._frequencies[index]
        for base in peaks:
            base_frequency = self._frequencies[base]
            order = round(frequency / base_frequency)
            if not 1 <= order <= 6 or abs(frequency - order * base_frequency) > 1.5 * self.bin_hz:
                continue
            matches = 0
            for harmonic in range(1, 7):
                near = peaks[np.abs(self._frequencies[peaks] - harmonic * base_frequency)
                             <= 1.5 * self.bin_hz]
                if len(near) and float(power[near].max()) >= power[index] * 0.01:
                    matches += 1
            if matches >= 3:
                return True
        return False

    def _analyze(self, samples, start) -> list[dict]:
        end = start + self.window_seconds
        events = []
        used = set()
        for candidate in self._candidates(samples):
            possible = [track for track in self._tracks if track.identifier not in used
                        and abs(track.frequency - candidate["frequency"])
                        <= max(self.bin_hz * 1.5, candidate["frequency"] * 0.005)]
            if possible:
                track = min(possible, key=lambda t: abs(t.frequency - candidate["frequency"]))
                track.frequency = candidate["frequency"]
                track.last = end
                track.prominence = candidate["prominence"]
                track.peak = max(track.peak, candidate["peak"])
            elif len(self._tracks) < 8:
                track = _Track(self._next_id, start, end, candidate["frequency"],
                               candidate["prominence"], candidate["peak"])
                self._next_id += 1
                self._tracks.append(track)
            else:
                continue
            used.add(track.identifier)
            track.history.append((end, candidate["peak"], candidate["frequency"]))
            previously_grew = track.grew
            self._growth(track)
            stable = self._stable(track)
            if not track.announced and end - track.start >= self.min_duration and stable:
                track.announced = True
                track.last_notice = end
                events.append(self._event(track, "start"))
            elif track.announced and (end - track.last_notice >= 1.0
                                      or (track.grew and not previously_grew)):
                track.last_notice = end
                events.append(self._event(track, "update"))
        active = []
        for track in self._tracks:
            if end - track.last > 0.1 + 1e-9:
                if track.announced:
                    events.append(self._event(track, "end"))
            else:
                active.append(track)
        self._tracks = active
        return events

    def _stable(self, track) -> bool:
        history = np.asarray(track.history)
        return float(np.std(history[:, 2])) <= max(self.bin_hz * .4, track.frequency * .002)

    def _growth(self, track):
        # Ignore the first window's attack: a switched-on sine is not evidence
        # of sustained exponential growth. Work only with recent causal samples.
        points = [(t, level) for t, level, _ in track.history
                  if t >= max(track.start + self.window_seconds + .12, track.last - 1.0)]
        if len(points) < 2 or points[-1][0] - points[0][0] < .35:
            return
        times, levels = np.asarray(points).T
        centered = times - times.mean()
        slope = float(centered @ (levels - levels.mean()) / (centered @ centered))
        track.growth = slope
        residual = levels - (levels.mean() + slope * centered)
        if (slope >= 6 and levels[-1] - levels[0] >= 4
                and np.mean(np.diff(levels) >= -.5) >= .75 and np.std(residual) < 3):
            track.grew = True

    def _event(self, track, kind) -> dict:
        strength = np.clip((track.prominence - self.threshold_db) / 30, 0, 1)
        score = min(.95, .35 + .2 * float(strength) + (.35 if track.grew else 0))
        return {
            "event": kind,
            "id": track.identifier,
            "classification": "feedback_like" if track.grew else "sustained_tone",
            "start_seconds": round(track.start, 6),
            "end_seconds": round(track.last, 6),
            "duration_seconds": round(track.last - track.start, 6),
            "frequency_hz": round(track.frequency, 2),
            "prominence_db": round(track.prominence, 2),
            "peak_dbfs": round(track.peak, 2),
            "growth_db_per_second": round(track.growth, 2),
            "score": round(score, 3),
        }
