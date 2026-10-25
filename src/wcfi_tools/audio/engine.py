"""Shared emulator state, prepared sources, smooth gain changes and output metrics."""
from __future__ import annotations

import copy
import itertools
import json
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

from .analysis_nodes import AnalysisNodes
from .asio import CHANNELS, RATE, NativeBridge, NativeService
from .emulator import InputBank, load_profile
from .takes import RecordedTake, catalog, library_directory

# Disk-backed rehearsal feeds need headroom for Windows scheduling stalls.
# This is producer lookahead, separate from the ASIO callback buffer size.
INPUT_LEAD_FRAMES = 8192


def input_lead_frames():
    """Read an opt-in rehearsal queue target; never change the native ASIO buffer."""
    configured = os.environ.get("WCFI_EMULATOR_INPUT_LEAD_FRAMES", str(INPUT_LEAD_FRAMES))
    try:
        frames = int(configured)
    except ValueError:
        raise ValueError("WCFI_EMULATOR_INPUT_LEAD_FRAMES must be an integer from 4096 to "
                         "24576 in multiples of 512") from None
    if not 4096 <= frames <= 24576 or frames % 512:
        raise ValueError("WCFI_EMULATOR_INPUT_LEAD_FRAMES must be an integer from 4096 to "
                         "24576 in multiples of 512")
    return frames


def default_channels():
    names = {1: ("Lead vocal", "vocal"), 2: ("Backing vocal", "vocal"),
             3: ("Backing vocal", "vocal"), 10: ("Electric guitar", "electric_guitar"),
             11: ("Bass", "bass"), 13: ("Keys L", "keys"), 14: ("Keys R", "keys"),
             15: ("Drum mix L", "drums"), 16: ("Drum mix R", "drums")}
    return [{"id": n, "name": names.get(n, (f"Input {n:02}", "generic"))[0],
             "role": names.get(n, ("", "generic"))[1], "enabled": False,
             "muted": False, "solo": False, "gain_db": 0.0,
             "source": {"kind": "silence", "level_dbfs": -30, "frequency_hz": 220},
             "faults": [], "seconds": 10.0} for n in range(1, CHANNELS + 1)]


def db(value):
    return max(-120.0, float(20 * np.log10(max(float(value), 1e-6))))


def meters(block):
    if not len(block):
        return [{"peak_dbfs": -120, "rms_dbfs": -120, "clips": 0} for _ in range(CHANNELS)]
    valid = np.nan_to_num(block, nan=0, posinf=0, neginf=0)
    peak = np.max(np.abs(valid), axis=0)
    rms = np.sqrt(np.mean(valid.astype(np.float64) ** 2, axis=0))
    clips = np.sum(np.abs(valid) >= .99999, axis=0)
    return [{"peak_dbfs": db(peak[n]), "rms_dbfs": db(rms[n]), "clips": int(clips[n])}
            for n in range(CHANNELS)]


class EmulatorEngine:
    def __init__(self, state_dir: Path, *, dll=None, native_factory=NativeService, library_root=None):
        self.input_lookahead_frames = input_lead_frames()
        self.state_dir = state_dir
        self.dll = dll
        self.native_factory = native_factory
        self.lock = threading.RLock()
        self.control_lock = threading.RLock()
        self.render_lock = threading.RLock()
        self.native_lock = threading.RLock()
        self.library_root = Path(library_root) if library_root is not None else library_directory()
        self.take = None
        self.before_take_channels = None
        self.before_take_prepared = None
        self.warning = None
        self.channels = default_channels()
        self.prepared = {}
        self.revisions = {n: 0 for n in range(1, 33)}
        self.native = None
        self.running = False
        self.sending = False
        self.error = None
        self.bridge_status = None
        self.position = 0
        self.gains = np.zeros(CHANNELS, dtype=np.float32)
        self.reset_gains = True
        self.last_sources = {}
        self.source_fades = {}
        self.input_meters = meters(np.empty((0, CHANNELS)))
        self.output_meters = meters(np.empty((0, CHANNELS)))
        self.history = np.zeros((RATE * 3, CHANNELS), dtype=np.float32)
        self.history_written = 0
        self.last_output = 0.0
        self.nodes = AnalysisNodes()
        self.shutdown_event = threading.Event()
        self.threads = []
        self.revision = 0
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "channels.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            take_settings = None
            current_channels = saved
            if isinstance(saved, dict) and saved.get("schema_version") == 2:
                take_settings = saved.get("take")
                current_channels = saved.get("channels")
                saved = saved.get("manual_channels")
            if (not isinstance(saved, list) or len(saved) != CHANNELS or
                    not isinstance(current_channels, list) or len(current_channels) != CHANNELS):
                raise ValueError("Saved channel configuration must contain 32 inputs")
            for index, channel in enumerate(saved, 1):
                self.configure(index, channel, persist=False)
            if take_settings is not None:
                try:
                    if not isinstance(take_settings, dict):
                        raise TypeError("Invalid saved recorded take")
                    self.load_take(take_settings["id"], start_frame=take_settings["start_frame"],
                                   end_frame=take_settings["end_frame"], fade_ms=take_settings["fade_ms"],
                                   persist=False)
                    for index, channel in enumerate(current_channels, 1):
                        self.configure(index, channel, persist=False)
                except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
                    if self.take:
                        self.unload_take(persist=False)
                    self.warning = f"Recorded take unavailable; previous input setup restored. {exc}"

    def launch_workers(self):
        for fn in (self._heartbeat, self._audio, self._node_analysis):
            thread = threading.Thread(target=fn, daemon=True)
            thread.start()
            self.threads.append(thread)

    def start(self):
        with self.native_lock:
            if not self.native:
                self.native = self.native_factory(self.dll)
            self.native.set_active(True)
            with self.lock:
                self.running, self.error = True, None
                self.revision += 1
        return self.snapshot()

    def stop(self):
        with self.native_lock:
            if self.native:
                self.native.set_active(False)
            with self.lock:
                self.running, self.sending = False, False
                self.reset_gains = True
                self.input_meters = meters(np.empty((0, CHANNELS)))
                self.revision += 1
        return self.snapshot()

    def set_sending(self, value):
        if not isinstance(value, bool):
            raise ValueError("sending must be true or false")  # noqa: TRY004 - API validation
        with self.lock:
            if value and not self.running:
                raise ValueError("Start the emulator first")
            self.sending = value
            if self.take and not value:
                self.reset_gains = True
            self.revision += 1
        return self.snapshot()

    def _save(self):
        path = self.state_dir / "channels.json"
        temporary = path.with_suffix(".tmp")
        data = self.channels if self.take is None else {
            "schema_version": 2, "channels": self.channels,
            "manual_channels": self.before_take_channels, "take": self.take.settings()}
        temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)

    def available_takes(self):
        return {"takes": catalog(self.library_root)}

    def _take_transaction(self, mutate, *, persist):
        """Publish a prepared configuration atomically against the disk reader."""
        fields = ("take", "channels", "prepared", "before_take_channels", "before_take_prepared",
                  "sending", "reset_gains", "warning", "error")
        with self.render_lock, self.lock:
            old = {key: getattr(self, key) for key in fields}
            try:
                mutate()
                self.sending = False
                self.reset_gains = True
                self.warning, self.error = None, None
                if persist:
                    self._save()
            except Exception:
                for key, item in old.items():
                    setattr(self, key, item)
                raise
            self.last_sources.clear()
            self.source_fades.clear()
            self.input_meters = meters(np.empty((0, CHANNELS)))
            self.revision += 1
            return old["take"]

    def load_take(self, take_id, *, start_frame=0, end_frame=None, fade_ms=5, persist=True):
        from ..soundcheck.profiles import guess_role

        with self.control_lock:
            # Hash-check and open files outside state/audio/native locks.
            reader = RecordedTake(self.library_root, take_id, start_frame=start_frame,
                                  end_frame=end_frame, fade_ms=fade_ms)
            desired = default_channels()
            for source in reader.data["sources"]:
                for index, port in enumerate(source["emulator_inputs"]):
                    suffix = (" L" if index == 0 else " R") if source["channels"] == 2 else ""
                    desired[port - 1].update(name=source["track_name"][:100 - len(suffix)] + suffix, enabled=True,
                        role=guess_role(source["track_name"])[0], source={"kind": "take",
                            "take_id": reader.data["take_id"], "track_name": source["track_name"],
                            "file_channel": index + 1, "original_name": Path(source["file"]).name})

            def commit():
                if self.take is None:
                    self.before_take_channels = copy.deepcopy(self.channels)
                    self.before_take_prepared = self.prepared.copy()
                self.take, self.channels, self.prepared = reader, desired, {}

            try:
                old = self._take_transaction(commit, persist=persist)
            except Exception:
                reader.close()
                raise
            if old:
                old.close()
        return self.snapshot()

    def unload_take(self, *, persist=True):
        with self.control_lock:
            if self.take is None:
                return self.snapshot()

            def commit():
                self.channels = copy.deepcopy(self.before_take_channels)
                self.prepared = self.before_take_prepared.copy()
                self.take, self.before_take_channels, self.before_take_prepared = None, None, None

            old = self._take_transaction(commit, persist=persist)
            old.close()
        return self.snapshot()

    def set_take_loop(self, start_frame, end_frame, fade_ms=5):
        with self.control_lock, self.render_lock, self.lock:
            if self.take is None:
                raise ValueError("Load a recorded take first")
            start_frame, end_frame, fade_ms = self.take.validate_range(start_frame, end_frame, fade_ms)
            previous = self.take.settings(), self.take.cursor

            def commit():
                self.take.set_range(start_frame, end_frame, fade_ms)

            try:
                self._take_transaction(commit, persist=True)
            except Exception:
                settings, cursor = previous
                self.take.set_range(settings["start_frame"], settings["end_frame"], settings["fade_ms"])
                self.take.cursor = cursor
                raise
        return self.snapshot()

    def restart_take(self):
        with self.control_lock:
            if self.take is None:
                raise ValueError("Load a recorded take first")
            settings = self.take.settings()
            return self.set_take_loop(settings["start_frame"], settings["end_frame"], settings["fade_ms"])

    def configure(self, port, changes, *, persist=True):
        from ..soundcheck.profiles import ROLES, number

        if type(port) is not int or not 1 <= port <= CHANNELS or not isinstance(changes, dict):
            raise ValueError("Select one input numbered 1–32")
        allowed = {"name", "role", "enabled", "muted", "solo", "gain_db", "source", "faults", "seconds", "id"}
        if changes.keys() - allowed:
            raise ValueError("Unknown channel setting")
        with self.control_lock:
            with self.lock:
                candidate = copy.deepcopy(self.channels[port - 1])
                original = copy.deepcopy(candidate)
                linked = self.take is not None and port in self.take.ports
            candidate.update(copy.deepcopy(changes))
            candidate["id"] = port
            if not isinstance(candidate["name"], str) or not 1 <= len(candidate["name"].strip()) <= 100:
                raise ValueError("Use a channel name of 1–100 characters")
            if candidate["role"] not in ROLES:
                raise ValueError("Unknown channel role")
            for field in ("enabled", "muted", "solo"):
                if type(candidate[field]) is not bool:
                    raise ValueError(f"{field} must be true or false")
            candidate["gain_db"] = number(candidate["gain_db"], "gain dB", -60, 12)
            candidate["seconds"] = number(candidate["seconds"], "source seconds", 1, 30)
            if linked and any(candidate[k] != original[k] for k in ("source", "faults", "seconds")):
                raise ValueError("Recorded inputs use the shared take. Unload it to change individual sources or faults.")
            if not isinstance(candidate["source"], dict):
                raise TypeError("Source must be an object")
            if not linked and candidate["source"].get("kind") == "take":
                raise ValueError("Load recorded sources through the take library")
            source_changed = any(k in changes for k in ("source", "faults", "seconds", "role"))
            samples = None
            if not linked and (source_changed or port not in self.prepared):
                if candidate["source"].get("kind") == "file":
                    # Web/API clients may only use files imported into this service.
                    source_path = Path(candidate["source"].get("path", "")).resolve()
                    uploads = (self.state_dir / "uploads").resolve()
                    if not source_path.is_relative_to(uploads) or not source_path.is_file():
                        raise ValueError("Choose an imported local audio file")
                profile = {"schema_version": 1, "kind": "wcfi_audio_emulator", "sample_rate": RATE,
                           "input_count": CHANNELS, "seconds": candidate["seconds"], "seed": port * 31,
                           "loop": True, "lead_name": candidate["name"], "channels": [
                               {"name": candidate["name"], "role": candidate["role"], "inputs": [port],
                                "source": candidate["source"], "faults": candidate["faults"]}]}
                # Validate with the same schema as the CLI; never modify the live source on error.
                check_path = self.state_dir / ".prepare.json"
                check_path.write_text(json.dumps(profile, allow_nan=False), encoding="utf-8")
                try:
                    validated = load_profile(check_path)
                    if candidate["source"]["kind"] == "silence" and not candidate["faults"]:
                        samples = np.zeros(1, dtype=np.float32)
                    else:
                        bank = InputBank(validated)
                        samples = bank.prepare("baseline")[0][0][:, 0]
                finally:
                    check_path.unlink(missing_ok=True)
            with self.lock:
                previous = self.channels[port - 1]
                self.channels[port - 1] = candidate
                try:
                    if persist:
                        self._save()
                except OSError:
                    self.channels[port - 1] = previous
                    raise
                if samples is not None:
                    self.prepared[port] = samples
                self.revisions[port] += 1
                self.revision += 1
                return copy.deepcopy(candidate)

    def preset(self, name):
        with self.control_lock:
            return self._preset(name)

    def _preset(self, name):
        if name not in {"silence", "two-tones", "band-probes"}:
            raise ValueError("Unknown preset")
        with self.control_lock:
            self.unload_take(persist=False)
        # Presets only select deterministic local engineering probes.
        self.set_sending(False)
        desired = default_channels()
        if name != "silence":
            for index, hz in enumerate((220, 330)):
                desired[index].update(enabled=True, source={"kind": "tone", "frequency_hz": hz,
                                                           "level_dbfs": -30})
        if name == "band-probes":
            for port, kind in ((10, "notes"), (11, "notes"), (13, "notes"), (15, "drum-kit")):
                desired[port - 1].update(enabled=True, source={"kind": kind, "level_dbfs": -30,
                    "bpm": 105, "midi_notes": [40, 43, 45, 43] if port == 11 else [60, 64, 67, 64],
                    "kick_db": -26, "snare_db": -30, "hat_db": -38})
        with self.control_lock:
            for channel in desired:
                self.configure(channel["id"], channel, persist=False)
            with self.lock:
                self._save()
        return self.snapshot()

    def render(self, frames):
        """Bounded producer work, outside the native ASIO callback."""
        with self.render_lock:
            return self._render(frames)

    def _render(self, frames):
        with self.lock:
            if self.reset_gains:
                self.gains.fill(0)
                self.source_fades.clear()
                self.reset_gains = False
            channels = copy.deepcopy(self.channels)
            sources = self.prepared.copy()
            position, active = self.position, self.running and self.sending
            take = self.take
        take_audio = None
        if take and active:
            try:
                take_audio = take.read(frames)
            except (OSError, RuntimeError, ValueError) as exc:
                with self.lock:
                    self.sending, self.reset_gains = False, True
                    self.error = f"Recorded take playback paused: {exc}"
                    self.gains.fill(0)
                active = False
        solo = any(c["solo"] for c in channels)
        block = np.zeros((frames, CHANNELS), dtype=np.float32)
        ramp = min(frames, 2400)  # Gain ramps continue across chunks (~50 ms).
        for channel in channels:
            port = channel["id"]
            audio = sources.get(port)
            enabled = active and channel["enabled"] and not channel["muted"] and (not solo or channel["solo"])
            target = 10 ** (channel["gain_db"] / 20) if enabled else 0.0
            initial = float(self.gains[port - 1])
            step = (target - initial) * min(1.0, ramp / 2400)
            ending = initial + step
            if channel["source"]["kind"] == "take":
                if take_audio is not None:
                    block[:, port - 1] = take_audio[:, port - 1] * np.linspace(initial, ending, frames, dtype=np.float32)
            elif audio is not None and (initial > 0 or target > 0):
                indexes = np.arange(frames) + position
                samples = audio[indexes % len(audio)].copy()
                previous = self.last_sources.get(port)
                if previous is not audio:
                    self.source_fades[port] = (previous, 0)
                if port in self.source_fades:
                    old, progress = self.source_fades[port]
                    take = min(frames, 480 - progress)
                    blend = np.arange(progress, progress + take, dtype=np.float32) / 480
                    samples[:take] *= blend
                    if old is not None:
                        samples[:take] += old[indexes[:take] % len(old)] * (1 - blend)
                    if progress + take == 480:
                        self.source_fades.pop(port)
                    else:
                        self.source_fades[port] = (old, progress + take)
                gains = np.linspace(initial, ending, frames, dtype=np.float32)
                block[:, port - 1] = samples * gains
            self.last_sources[port] = audio
            self.gains[port - 1] = ending
        np.clip(block, -1, 1, out=block)
        with self.lock:
            self.position += frames
            self.input_meters = meters(block)
        return block

    def receive(self, block):
        if not len(block):
            return
        with self.lock:
            length = len(self.history)
            if len(block) > length:
                block = block[-length:]
            start = self.history_written % length
            take = min(len(block), length - start)
            self.history[start:start + take] = block[:take]
            self.history[:len(block) - take] = block[take:]
            self.history_written += len(block)
            self.output_meters = meters(block)
            self.last_output = time.monotonic()

    def analyze(self, ports):
        if (not isinstance(ports, list) or not 1 <= len(ports) <= 2 or
                any(type(n) is not int or not 1 <= n <= 32 for n in ports) or len(set(ports)) != len(ports)):
            raise ValueError("Analyze one output or a pair, numbered 1–32")
        with self.lock:
            count = min(self.history_written, RATE)
            indexes = (np.arange(count) + self.history_written - count) % len(self.history)
            audio = self.history[indexes][:, [n - 1 for n in ports]].copy()
            connected = time.monotonic() - self.last_output < 1
        if not connected or count < RATE:
            return {"available": False, "channels": ports, "sample_rate": RATE, "results": []}
        results = []
        for n, port in enumerate(ports):
            raw = audio[:, n]
            bad = int(np.count_nonzero(~np.isfinite(raw)))
            x = np.nan_to_num(raw, nan=0, posinf=0, neginf=0).astype(np.float64)
            peak, rms = float(np.max(np.abs(x))), float(np.sqrt(np.mean(x * x)))
            spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)))) * 4 / len(x)
            frequencies = np.fft.rfftfreq(len(x), 1 / RATE)
            edges = np.geomspace(20, 20000, 96)
            bins = [{"hz": float(math.sqrt(a * b)), "dbfs": db(np.max(
                spectrum[(frequencies >= a) & (frequencies < b)], initial=0))}
                for a, b in itertools.pairwise(edges)]
            slices = np.array_split(x, 200)
            waveform = [{"min": float(np.min(s)), "max": float(np.max(s))} for s in slices]
            clips = int(np.sum(np.abs(x) >= .99999))
            notes = []
            if peak < 1e-5:
                notes.append("No signal detected in this output.")
            if clips:
                notes.append("Samples are reaching full scale; check gain and upstream clipping.")
            if bad:
                notes.append("Non-finite samples were received from the DAW.")
            results.append({"channel": port, "peak_dbfs": db(peak), "rms_dbfs": db(rms),
                            "crest_db": db(peak) - db(rms), "dc_offset": float(np.mean(x)),
                            "clipped_samples": clips, "non_finite_samples": bad,
                            "dominant_hz": float(frequencies[np.argmax(spectrum)]) if peak > 1e-5 else None,
                            "spectrum": bins, "waveform": waveform, "notes": notes})
        correlation = None
        if len(ports) == 2 and np.std(audio[:, 0]) > 1e-6 and np.std(audio[:, 1]) > 1e-6:
            value = float(np.corrcoef(audio.T)[0, 1])
            correlation = value if math.isfinite(value) else None
        return {"available": True, "channels": ports, "sample_rate": RATE,
                "window_seconds": count / RATE, "correlation": correlation, "results": results,
                "method": "Local sample measurements; no AI inference, LUFS or true-peak estimate."}

    def snapshot(self):
        with self.lock:
            stale = time.monotonic() - self.last_output > 1
            return {"schema_version": 1, "running": self.running, "sending": self.sending,
                    "error": self.error, "warning": self.warning, "revision": self.revision,
                    "take": self.take.snapshot() if self.take else None,
                    "sample_rate": RATE, "input_count": CHANNELS, "output_count": CHANNELS,
                    "input_lookahead_frames": self.input_lookahead_frames,
                    "input_lookahead_ms": self.input_lookahead_frames / RATE * 1000,
                    "channels": copy.deepcopy(self.channels), "input_meters": copy.deepcopy(self.input_meters),
                    "output_meters": meters(np.empty((0, CHANNELS))) if stale else copy.deepcopy(self.output_meters),
                    "output_connected": not stale, "driver": copy.deepcopy(self.bridge_status),
                    "transport_note": "Ableton transport is never controlled by this app."}

    def _heartbeat(self):
        while not self.shutdown_event.wait(.25):
            with self.native_lock:
                if self.native:
                    self.native.pulse()

    def _node_analysis(self):
        while not self.shutdown_event.wait(.01):
            try:
                self.nodes.tick()
            except (ValueError, OSError, RuntimeError, FloatingPointError) as exc:
                self.nodes.failure(exc)

    def _audio(self):
        bridge = None
        next_retry = 0.0
        while not self.shutdown_event.wait(.002):
            try:
                with self.native_lock:
                    native = self.native
                    status = native.status() if native else None
                with self.lock:
                    self.bridge_status = status
                if not status or not status["running"] or not status["host_pid"]:
                    if bridge:
                        bridge.close()
                        bridge = None
                    self.shutdown_event.wait(.025)
                    continue
                if bridge and (bridge.generation != status["generation"] or bridge.host_pid != status["host_pid"]):
                    bridge.close()
                    bridge = None
                if not bridge:
                    if time.monotonic() < next_retry:
                        continue
                    bridge = NativeBridge(self.dll, roles=3)
                    bridge.discard_output()
                    with self.lock:
                        self.history_written = 0
                        self.last_output = 0
                bridge.check_session()
                if status["service_active"]:
                    # Refill faster than real time after a scheduling stall. Small
                    # 1024-frame writes could never catch up at a 40 ms worker cadence.
                    # The queue target (and producer lookahead) stays unchanged.
                    target = max(self.input_lookahead_frames, 2 * status["blocksize"])
                    count = min(4096, max(0, target - status["input_queued"]))
                    if count:
                        bridge.feed(self.render(count))
                block = bridge.read(8192)
                self.receive(block)
            except (OSError, RuntimeError, ValueError) as exc:
                if bridge:
                    bridge.close()
                    bridge = None
                with self.lock:
                    self.error = str(exc)
                next_retry = time.monotonic() + .5
        if bridge:
            bridge.close()

    def close(self):
        self.stop()
        self.shutdown_event.set()
        for thread in self.threads:
            thread.join(timeout=3)
        self.nodes.close()
        with self.render_lock:
            if self.take:
                self.take.close()
        with self.native_lock:
            if self.native:
                self.native.close()
                self.native = None
