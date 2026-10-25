"""Repeatable raw inputs; no device access, playback, network, GPU or inference.

Render work happens before streaming, never in an audio callback. Generated
signals are engineering probes, not substitutes for real singing performances.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from ..soundcheck.profiles import ROLES, file_hash, number, write_json

SCENARIOS = {
    "baseline": "Configured sources and per-channel faults, with no extra preset faults.",
    "quiet-vocal": "Lead input attenuated by 24 dB.",
    "clipped-vocal": "Lead preamp driven into hard clipping, then attenuated 6 dB.",
    "guitar-noise": "Electric guitar with 60 Hz hum/harmonics and broadband hiss.",
    "bad-drums": "One stereo kit feed with a 300 Hz buildup and reduced top end.",
    "level-jumps": "Lead changes from normal to -24 dB to +12 dB and back.",
    "chaos": "Clipped lead, noisy guitar, and a muddy stereo drum submix together.",
}
MAX_MEMORY = 256 * 1024 * 1024  # Per bank, clean and affected banks are separate.
SOURCE_KINDS = {"file", "silence", "tone", "noise", "notes", "drum-kit"}


def _integer(item, label, low, high):
    result = number(item, label, low, high)
    if result != int(result):
        raise ValueError(f"{label} must be an integer")
    return int(result)


def make_profile(*, soundcheck_profile: Path | None = None) -> dict:
    """Make editable settings; real source paths retain their original meaning."""
    if soundcheck_profile:
        old = json.loads(soundcheck_profile.read_text(encoding="utf-8"))
        tracks = [t for t in old["tracks"] if t.get("enabled")]
    else:
        tracks = [
            {"name": "RED", "role": "vocal", "input_target": "AudioIn/External/M0"},
            {"name": "YELLOW", "role": "vocal", "input_target": "AudioIn/External/M1"},
            {"name": "GREEN", "role": "vocal", "input_target": "AudioIn/External/M2"},
            {"name": "EGUITAR", "role": "electric_guitar", "input_target": "AudioIn/External/M9"},
            {"name": "KEYS", "role": "keys", "input_target": "AudioIn/External/S6"},
            {"name": "BASS", "role": "bass", "input_target": "AudioIn/External/M10"},
            {"name": "KIT", "role": "drums", "input_target": "AudioIn/External/S7"},
        ]
    channels = []
    for index, track in enumerate(tracks):
        route = track.get("input_target", "").removeprefix("AudioIn/External/")
        if len(route) < 2 or route[0] not in {"M", "S"} or not route[1:].isdigit():
            raise ValueError(f"Unsupported input mapping on {track['name']}")
        offset = int(route[1:])
        inputs = [offset + 1] if route[0] == "M" else [offset * 2 + 1, offset * 2 + 2]
        role = track.get("role", "generic")
        source = {"kind": "tone", "frequency_hz": 220 + index * 55, "level_dbfs": -24}
        if role == "drums":
            source = {"kind": "drum-kit", "bpm": 105, "kick_db": -18,
                      "snare_db": -22, "hat_db": -32}
        elif role in {"keys", "bass", "electric_guitar"}:
            source = {"kind": "notes", "bpm": 105,
                      "midi_notes": [40, 43, 45, 43] if role == "bass" else [60, 64, 67, 64],
                      "level_dbfs": -24}
        if track.get("file"):
            path = Path(track["file"])
            if not path.is_absolute():
                path = soundcheck_profile.resolve().parent / path
            source = {"kind": "file", "path": str(path.resolve()), "start_seconds": 0}
        channels.append({"name": track["name"], "role": role, "inputs": inputs,
                         "source": source, "faults": []})
    return {"schema_version": 1, "kind": "wcfi_audio_emulator", "sample_rate": 48000,
            "input_count": max(32, max((max(c["inputs"]) for c in channels), default=0)),
            "seconds": 30, "seed": 42, "loop": True,
            "lead_name": next((c["name"] for c in channels if c["role"] == "vocal"), None),
            "source_note": "Synthetic vocals are tones, not human voices. Use file sources for "
                           "real performances or externally generated MIDI/neural stems.",
            "channels": channels}


def load_profile(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Emulator profile must be an object")  # noqa: TRY004 - configuration
    if data.get("kind") != "wcfi_audio_emulator" or data.get("schema_version") != 1:
        raise ValueError("Not a version 1 audio emulator profile")
    data = copy.deepcopy(data)
    data["sample_rate"] = _integer(data.get("sample_rate"), "sample rate", 16000, 96000)
    data["input_count"] = _integer(data.get("input_count"), "input count", 1, 64)
    data["seed"] = _integer(data.get("seed", 42), "seed", 0, 2**32 - 1)
    data["seconds"] = number(data.get("seconds"), "seconds", 1, 120)
    if not isinstance(data.get("loop"), bool):
        raise ValueError("loop must be true or false")  # noqa: TRY004 - configuration
    tracks = data.get("channels")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 32:
        raise ValueError("Use 1–32 named sources")
    seen, names = set(), set()
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("Each source must be an object")  # noqa: TRY004 - configuration
        name = track.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or name in names:
            raise ValueError("Source names must be nonempty and unique")
        names.add(name)
        if track.get("role") not in ROLES:
            raise ValueError(f"Unknown source role on {name}")
        inputs = track.get("inputs")
        if not isinstance(inputs, list) or len(inputs) not in {1, 2}:
            raise ValueError(f"{name}: map one mono input or two stereo inputs")
        track["inputs"] = [_integer(port, "input channel", 1, data["input_count"])
                           for port in inputs]
        for port in track["inputs"]:
            if port in seen:
                raise ValueError(f"Input {port} is mapped more than once")
            seen.add(port)
        source = track.get("source", {})
        if not isinstance(source, dict):
            raise ValueError(f"Invalid source on {name}")  # noqa: TRY004 - configuration
        if source.get("kind") not in SOURCE_KINDS:
            raise ValueError(f"Unsupported source on {name}")
        if source["kind"] == "file":
            if not isinstance(source.get("path"), str) or not source["path"]:
                raise ValueError(f"Missing source file on {name}")
            audio = Path(source["path"])
            source["path"] = str((path.resolve().parent / audio).resolve())
        if not isinstance(track.get("faults", []), list) or len(track.get("faults", [])) > 16:
            raise ValueError("Use at most 16 faults per source")
        if any(not isinstance(f, dict) for f in track.get("faults", [])):
            raise ValueError("Each fault must be an object")
    if data.get("lead_name") is not None and data["lead_name"] not in names:
        raise ValueError("lead_name does not identify a source")
    size = int(data["seconds"] * data["sample_rate"]) * len(seen) * 4
    if size > MAX_MEMORY:
        raise ValueError("Source bank exceeds 256 MiB; shorten the excerpt or reduce sources")
    return data


def _amplitude(db):
    return 10 ** (number(db, "level dB", -120, 48) / 20)


def _source(track, profile, index):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    rate = profile["sample_rate"]
    count = int(profile["seconds"] * rate)
    channels = len(track["inputs"])
    source = track["source"]
    rng = np.random.default_rng(np.random.SeedSequence([profile["seed"], index]))
    t = np.arange(count, dtype=np.float64) / rate
    provenance = {"kind": source["kind"]}
    if source["kind"] == "file":
        path = Path(source["path"])
        before = path.stat()
        digest = file_hash(path)
        start = number(source.get("start_seconds", 0), "source start", 0, 86400)
        with sf.SoundFile(path) as stream:
            selected_channel = source.get("file_channel")
            if selected_channel is not None:
                if channels != 1:
                    raise ValueError("file_channel selects one mono input from a source file")
                selected_channel = _integer(selected_channel, "file channel", 1, stream.channels) - 1
            if ((selected_channel is None and stream.channels != channels) or
                    not 8000 <= stream.samplerate <= 192000):
                raise ValueError(f"{track['name']}: file channel count must match its input mapping")
            source_rate = stream.samplerate
            stream.seek(min(round(start * source_rate), len(stream)))
            audio = stream.read(math.ceil(profile["seconds"] * source_rate),
                                dtype="float32", always_2d=True)
            if selected_channel is not None:
                audio = audio[:, selected_channel:selected_channel + 1]
                provenance["file_channel"] = selected_channel + 1
        if len(audio) < math.ceil(profile["seconds"] * source_rate):
            raise ValueError(f"{track['name']}: source is too short; choose an earlier start")
        if source_rate != rate:
            divisor = math.gcd(source_rate, rate)
            audio = resample_poly(audio, rate // divisor, source_rate // divisor, axis=0)
        audio = audio[:count]
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Source file changed while preparing the emulator")
        provenance.update(path=str(path), sha256=digest, start_seconds=start,
                          original_sample_rate=source_rate)
    else:
        amplitude = _amplitude(source.get("level_dbfs", -24))
        kind = source["kind"]
        if kind == "silence":
            mono = np.zeros(count)
        elif kind == "noise":
            mono = rng.standard_normal(count) * amplitude
        elif kind == "tone":
            frequency = number(source.get("frequency_hz", 440), "tone Hz", 20, rate / 2 - 1)
            mono = amplitude * np.sin(2 * np.pi * frequency * t)
        elif kind == "notes":
            notes = source.get("midi_notes", [60, 64, 67, 64])
            if not isinstance(notes, list) or not 1 <= len(notes) <= 128:
                raise ValueError("Provide 1–128 MIDI note numbers")
            notes = np.array([_integer(n, "MIDI note", 0, 127) for n in notes])
            beat = 60 / number(source.get("bpm", 105), "source BPM", 30, 240)
            note = notes[(t / beat).astype(int) % len(notes)]
            frequencies = 440 * 2 ** ((note - 69) / 12)
            phase = 2 * np.pi * np.cumsum(frequencies) / rate
            envelope = np.minimum((t % beat) / .01, 1) * np.exp(-3 * (t % beat) / beat)
            mono = amplitude * envelope * (np.sin(phase) + .25 * np.sin(2 * phase))
        else:  # One mixed drum feed, never separate drum outputs.
            beat = 60 / number(source.get("bpm", 105), "kit BPM", 30, 240)
            kick_t = t % beat
            kick = np.sin(2 * np.pi * (45 * kick_t + 45 * .025 *
                                     (1 - np.exp(-kick_t / .025)))) * np.exp(-kick_t / .07)
            snare_t = (t + beat) % (2 * beat)
            snare = rng.standard_normal(count) * np.exp(-snare_t / .05)
            hat_t = t % (beat / 2)
            noise = rng.standard_normal(count)
            hat = np.diff(noise, prepend=0) * np.exp(-hat_t / .018)
            mono = (kick * _amplitude(source.get("kick_db", -18)) +
                    snare * _amplitude(source.get("snare_db", -22)) +
                    hat * _amplitude(source.get("hat_db", -32)))
            if "level_dbfs" in source:
                mono *= amplitude / max(float(np.max(np.abs(mono))), 1e-12)
        audio = np.repeat(mono[:, None], channels, axis=1)
        provenance["note"] = "Procedural probe; not a real singer or captured instrument"
    if not np.isfinite(audio).all():
        raise ValueError(f"Non-finite source samples on {track['name']}")
    return np.asarray(audio, dtype=np.float32), provenance


def faults_for(profile, scenario):
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario}")
    faults = [copy.deepcopy(c.get("faults", [])) for c in profile["channels"]]
    targets = set()
    for index, channel in enumerate(profile["channels"]):
        lead = channel["name"] == profile.get("lead_name")
        role = channel["role"]
        extra = []
        if lead and scenario == "quiet-vocal":
            extra = [{"kind": "gain", "db": -24}]
        if lead and scenario in {"clipped-vocal", "chaos"}:
            extra = [{"kind": "clip", "drive_db": 30, "ceiling": .7, "post_db": -6}]
        if role == "electric_guitar" and scenario in {"guitar-noise", "chaos"}:
            extra = [{"kind": "hum", "hz": 60, "dbfs": -24, "harmonics": 5},
                     {"kind": "noise", "dbfs": -38}]
        if role == "drums" and scenario in {"bad-drums", "chaos"}:
            extra = [{"kind": "mud", "hz": 300, "gain_db": 12, "lowpass_hz": 4000}]
        if lead and scenario == "level-jumps":
            length = profile["seconds"]
            extra = [{"kind": "levels", "steps": [
                {"seconds": 0, "db": 0}, {"seconds": length / 4, "db": -24},
                {"seconds": length / 2, "db": 12}, {"seconds": length * .75, "db": 0}]}]
        if extra:
            targets.add(channel["name"])
            faults[index].extend(extra)
    if scenario != "baseline" and not targets:
        raise ValueError(f"Scenario {scenario} has no matching lead/source role in this profile")
    if any(len(items) > 20 for items in faults):
        raise ValueError("Too many faults after applying the preset")
    return faults


def apply_faults(audio, rate, faults, seed):
    import numpy as np
    from scipy.signal import butter, iirpeak, lfilter, sosfilt

    result = audio.copy()
    rng = np.random.default_rng(seed)
    events = []
    for fault in faults:
        kind = fault.get("kind")
        event = copy.deepcopy(fault)
        if kind == "gain":
            result *= _amplitude(fault["db"])
        elif kind == "clip":
            ceiling = number(fault.get("ceiling", 1), "clip ceiling", .01, 1)
            driven = result * _amplitude(fault.get("drive_db", 24))
            event["clipped_sample_fraction"] = float(np.mean(np.abs(driven) >= ceiling))
            result = np.clip(driven, -ceiling, ceiling) * _amplitude(fault.get("post_db", 0))
            event["expected_response"] = "Flag upstream damage; attenuation does not undo clipping"
        elif kind == "noise":
            result += rng.standard_normal(result.shape).astype(np.float32) * _amplitude(fault["dbfs"])
        elif kind == "hum":
            hz = number(fault.get("hz", 60), "hum Hz", 40, 400)
            harmonics = _integer(fault.get("harmonics", 3), "hum harmonics", 1, 10)
            t = np.arange(len(result)) / rate
            hum = sum(np.sin(2 * np.pi * hz * n * t) / n for n in range(1, harmonics + 1))
            hum *= _amplitude(fault["dbfs"]) / max(float(np.sqrt(np.mean(hum**2))), 1e-12)
            result += hum[:, None].astype(np.float32)
        elif kind == "mud":
            hz = number(fault.get("hz", 300), "mud Hz", 80, 1000)
            gain = _amplitude(number(fault.get("gain_db", 12), "mud gain", 0, 24))
            b, a = iirpeak(hz, .8, fs=rate)
            result += (gain - 1) * lfilter(b, a, result, axis=0).astype(np.float32)
            cutoff = number(fault.get("lowpass_hz", 4000), "lowpass Hz", 1000, rate / 2 - 1)
            result = sosfilt(butter(2, cutoff, fs=rate, output="sos"), result, axis=0)
            event["expected_response"] = "One kit feed: cannot independently rebalance captured drums"
        elif kind == "levels":
            steps = fault.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
                raise ValueError("Use 1–32 level steps")
            positions = [round(number(s["seconds"], "step time", 0, len(result) / rate) * rate)
                         for s in steps]
            if positions[0] != 0 or positions != sorted(set(positions)):
                raise ValueError("Level steps must start at zero and be strictly increasing")
            for n, step in enumerate(steps):
                end = positions[n + 1] if n + 1 < len(positions) else len(result)
                result[positions[n]:end] *= _amplitude(step["db"])
        elif kind == "dropout":
            start = round(number(fault["start_seconds"], "dropout start", 0, len(result) / rate) * rate)
            duration = round(number(fault["seconds"], "dropout duration", .001, 120) * rate)
            result[start:start + duration] = 0
        else:
            raise ValueError(f"Unsupported input fault: {kind}")
        events.append(event)
    if not np.isfinite(result).all():
        raise ValueError("Fault chain produced non-finite samples")
    # Model the final ADC boundary. A quiet downstream trim cannot repair prior clipping.
    overloaded = float(np.mean(np.abs(result) > 1))
    if overloaded:
        events.append({"kind": "adc_clip", "clipped_sample_fraction": overloaded,
                       "expected_response": "Flag input overload; no recovery of lost waveform"})
    return np.clip(result, -1, 1).astype(np.float32), events


class InputBank:
    """Immutable prepared raw stems. Streaming only copies their samples to ports."""
    def __init__(self, profile):
        self.profile = profile
        self.clean, self.provenance = [], []
        for index, track in enumerate(profile["channels"]):
            audio, origin = _source(track, profile, index)
            self.clean.append(audio)
            self.provenance.append(origin)
        self.frames = len(self.clean[0])

    def prepare(self, scenario):
        import numpy as np

        recipes = faults_for(self.profile, scenario)
        audio, truth = [], []
        for index, (track, source, faults) in enumerate(zip(
                self.profile["channels"], self.clean, recipes, strict=True)):
            samples, events = apply_faults(source, self.profile["sample_rate"], faults,
                                          np.random.SeedSequence([self.profile["seed"], index, 1]))
            samples.flags.writeable = False
            audio.append(samples)
            truth.append({"name": track["name"], "role": track["role"],
                          "inputs": track["inputs"], "source": self.provenance[index],
                          "faults": events, "sample_peak_dbfs":
                          float(20 * np.log10(max(float(np.max(np.abs(samples))), 1e-12)))})
        return audio, truth


def render(profile_path: Path, output: Path, *, scenario="baseline") -> dict:
    """Diagnostic files for the same engine used by the native ASIO adapter."""
    import soundfile as sf

    if output.exists():
        raise ValueError("Choose a new output directory; existing runs are never overwritten")
    profile = load_profile(profile_path)
    bank = InputBank(profile)
    audio, truth = bank.prepare(scenario)
    output.mkdir(parents=True, exist_ok=False)
    tracks = []
    for index, (samples, item) in enumerate(zip(audio, truth, strict=True)):
        path = output / f"input-{index + 1:02d}.wav"
        sf.write(path, samples, profile["sample_rate"], subtype="FLOAT")
        tracks.append(dict(item, file=path.name, sha256=file_hash(path)))
    report = {"schema_version": 1, "kind": "wcfi_emulator_render",
              "scenario": scenario, "sample_rate": profile["sample_rate"],
              "frames": bank.frames, "input_count": profile["input_count"],
              "profile_sha256": file_hash(profile_path), "seed": profile["seed"],
              "tracks": tracks, "note": "Raw inputs only; this is not an approved mix, "
              "virtual driver installation, or proof of an Ableton connection."}
    write_json(output / "manifest.json", report)
    return report
