"""Prepare small fader trials for rendering through the complete Live signal chain."""

from __future__ import annotations

import copy
import gzip
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from .ableton import _assert_unautomated, put, read_set, value
from .profiles import file_hash, number, write_json


def prepare_session_comparisons(source: Path, output: Path, *, lead_track_id: int,
                                start_bar: int, bars: int, step_db: float = 3) -> dict:
    """Write three set copies beside the source; Live must render each Master offline.

    These are experimental alternatives, not an automatically selected or approved mix.
    Keeping copies beside the source preserves relative sample references and plugin state.
    """
    source, output = source.resolve(), output.resolve()
    number(lead_track_id, "lead track ID", 0, 2**31 - 1)
    number(start_bar, "start bar", 1, 100000)
    number(bars, "bars", 1, 1024)
    number(step_db, "step dB", .5, 3)
    if output.parent != source.parent or output.exists():
        raise ValueError("Choose a new trial directory beside the source .als")
    root = read_set(source)
    digest = file_hash(source)
    lead = root.find(f"./LiveSet/Tracks/AudioTrack[@Id='{lead_track_id}']")
    if lead is None:
        raise ValueError("Lead ID must identify an audio track in this set")
    volume = lead.find("DeviceChain/Mixer/Volume")
    if volume is None:
        raise ValueError("Lead track has no supported fader")
    _assert_unautomated(root, volume)
    initial = float(value(volume, "Manual"))
    if not math.isfinite(initial) or initial <= 0:
        raise ValueError("Lead fader must have finite, nonzero gain")
    if initial * 10 ** (step_db / 20) > 10 ** (6 / 20):
        raise ValueError("Louder comparison would exceed Live's +6 dB fader range")
    if root.findall(".//ExternalAudioEffect") or root.findall(".//ExternalInstrument"):
        raise ValueError("External effects/instruments require connected hardware; render separately")

    changes = []
    tracks = root.findall("./LiveSet/Tracks/*")
    for track in tracks:
        name = value(track, "Name/EffectiveName")
        solo = track.find("DeviceChain/Mixer/SoloSink")
        if solo is not None:
            if solo.get("Value") == "true":
                changes.append(f"Cleared solo on {name} in trial copies")
            solo.set("Value", "false")
        if track.tag == "AudioTrack":
            monitor = track.find("DeviceChain/MainSequencer/MonitoringEnum")
            if monitor is None:
                raise ValueError(f"Missing audio monitoring setting on {name}")
            monitor.set("Value", "2")  # Live 11: Off; render recorded clips, never live inputs.
    tempo = float(value(root, "LiveSet/MasterTrack/DeviceChain/Mixer/Tempo/Manual"))
    if not math.isfinite(tempo) or tempo <= 0:
        raise ValueError("Unsupported tempo")
    # The UI render range is explicit; do not silently assume the current selection.
    settings = {"rendered_track": "Master", "start": f"{start_bar}.1.1",
                "length": f"{bars}.0.0", "normalize": False, "mono": False,
                "file_type": "WAV", "bit_depth": 32, "mp3": False}
    alternatives = []
    payloads = []
    for label, gain in (("baseline", 0), ("lead-quieter", -step_db),
                        ("lead-louder", step_db)):
        target = source.with_name(f"{output.name}-{label}.als")
        if target.exists():
            raise ValueError(f"Trial set already exists: {target.name}")
        candidate = copy.deepcopy(root)
        candidate_lead = candidate.find(f"./LiveSet/Tracks/AudioTrack[@Id='{lead_track_id}']")
        put(candidate_lead, "DeviceChain/Mixer/Volume/Manual", initial * 10 ** (gain / 20))
        payloads.append((target, gzip.compress(ET.tostring(candidate, encoding="utf-8",
                                                         xml_declaration=True), mtime=0)))
        alternatives.append({"condition": label, "lead_fader_delta_db": gain,
                             "set": str(target), "render": str(output / f"{label}.wav")})
    if file_hash(source) != digest:
        raise ValueError("Source set changed during preparation")
    output.mkdir(exist_ok=False)
    for target, payload in payloads:
        with target.open("xb") as stream:
            stream.write(payload)
    report = {"schema_version": 1, "kind": "wcfi_full_chain_session_trial",
              "status": "awaiting_offline_master_renders", "source_set": str(source),
              "source_sha256": digest, "lead_track_id": lead_track_id,
              "lead_name": value(lead, "Name/EffectiveName"),
              "mix_purpose": "livestream", "alternatives": alternatives,
              "export_settings": settings, "preparation_changes": changes,
              "audio_monitoring": "Off in trial copies", "running_ableton_modified": False,
              "limitations": [
                  "No alternative has been selected as an improvement.",
                  "Render each Master in Ableton; summing stems omits nonlinear bus processing.",
                  "Plugin state, sends, groups and Master processing are preserved.",
                  "External output buses outside Master are not included in the render.",
                  "Check rendered duration, activity and headroom before a listening comparison.",
                  "Use loudness-matched copies for preference tests; retain unnormalized originals.",
                  "Worship trials do not validate sermon/speech behavior.",
              ]}
    write_json(output / "trial.json", report)
    return report
