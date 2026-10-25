"""Read Live 11 sets and write calibrated COPIES using same-set stock templates.

ALS is not a public stable API. Restrict supported versions and fail closed on
unrecognized devices. No network, Remote Scripts, plugin state decoding, or
interaction with the running audio engine is involved.
"""

from __future__ import annotations

import copy
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

from .profiles import file_hash, guess_role, number

GAIN_NAME = "WCFI Input Gain"
EQ_NAME = "WCFI Soundcheck EQ"
GLOBAL_IDS = {"AutomationTarget", "ModulationTarget", "Pointee"}
MAX_XML = 64 * 1024 * 1024


def read_set(path: Path) -> ET.Element:
    with gzip.open(path, "rb") as stream:
        xml = stream.read(MAX_XML + 1)
    if len(xml) > MAX_XML or b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError("Unsupported or oversized Live set")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("Malformed Live set XML") from exc
    if not root.get("Creator", "").startswith("Ableton Live 11."):
        raise ValueError("Soundcheck currently supports saved Ableton Live 11 sets only")
    if root.find("LiveSet") is None:
        raise ValueError("Not an Ableton Live set")
    return root


def value(node: ET.Element, path: str, default: str = "") -> str:
    element = node.find(path)
    return element.get("Value", default) if element is not None else default


def put(node: ET.Element, path: str, item) -> None:
    element = node.find(path)
    if element is None:
        raise ValueError(f"Unsupported device schema: missing {path}")
    element.set("Value", str(item).lower() if isinstance(item, bool) else str(item))


def devices(track: ET.Element) -> ET.Element:
    chain = track.find("./DeviceChain/DeviceChain/Devices")
    if chain is None:
        raise ValueError("Track has no supported device chain")
    return chain


def inspect_set(path: Path) -> dict:
    root = read_set(path)
    tracks = []
    for track in root.findall("./LiveSet/Tracks/AudioTrack"):
        route = value(track, "./DeviceChain/AudioInputRouting/Target")
        if not route.startswith("AudioIn/External/"):
            continue
        name = value(track, "./Name/EffectiveName")
        role, enabled = guess_role(name)
        tracks.append({"track_id": int(track.get("Id")), "name": name,
                       "input": value(track, "./DeviceChain/AudioInputRouting/LowerDisplayString"),
                       "input_target": route, "role": role, "enabled": enabled, "file": None})
    return {"schema_version": 1, "source_set": str(path.resolve()),
            "capture_kind": "raw_pre_fx", "eq_mode": "gentle_cuts",
            "max_boost_db": 12.0, "max_cut_db": 24.0, "peak_ceiling_dbfs": -12.0,
            "routing_note": "Ableton 23/24 -> TouchMix Aux 3/4 stereo -> ATEM line input",
            "tracks": tracks}


def _clone(template: ET.Element, root: ET.Element, device_id: int) -> ET.Element:
    clone = copy.deepcopy(template)
    clone.set("Id", str(device_id))
    next_id = max([int(e.get("Id")) for e in root.iter() if e.tag in GLOBAL_IDS] + [0]) + 1
    hint = root.find("./LiveSet/NextPointeeId")
    if hint is not None:
        next_id = max(next_id, int(hint.get("Value")))
    for node in clone.iter():
        if node.tag in GLOBAL_IDS:
            node.set("Id", str(next_id))
            next_id += 1
        if node.tag in {"LomId", "LomIdView"}:
            node.set("Value", "0")
        if "LomId" in node.attrib:
            node.set("LomId", "0")
    # Keep preset/context wrapper structure intact; their IDs are local list IDs.
    locked = clone.find("LockedScripts")
    if locked is not None:
        locked.clear()
    # Templates cannot carry their own modulation/envelope/rack references.
    if clone.findall(".//PointeeId") or clone.findall(".//KeyMidi") or clone.findall(".//MidiCC"):
        raise ValueError("Template has control mappings; use an unmodified stock device template")
    if hint is not None:
        hint.set("Value", str(next_id))
    return clone


def _assert_unautomated(root: ET.Element, device: ET.Element) -> None:
    ids = {node.get("Id") for node in device.iter() if node.tag in GLOBAL_IDS}
    if any(node.get("Value") in ids for node in root.findall(".//PointeeId")):
        raise ValueError("Calibration device has automation/macro references; remove those first")
    if device.findall(".//KeyMidi") or device.findall(".//MidiCC"):
        raise ValueError("Calibration device has MIDI/key mappings; remove those first")


def _neutral_utility(device: ET.Element) -> None:
    settings = {"On/Manual": True, "PhaseInvertL/Manual": False, "PhaseInvertR/Manual": False,
                "ChannelMode/Manual": 1, "StereoWidth/Manual": 1,
                "MidSideBalance/Manual": 1, "MidSideBalanceOn": False,
                "Mono/Manual": False, "BassMono/Manual": False,
                "BassMonoAudition": False, "Balance/Manual": 0,
                "Mute/Manual": False, "DcFilter/Manual": False,
                "LegacyMode": False, "Gain/Manual": 1, "LegacyGain/Manual": 0}
    for key, item in settings.items():
        put(device, key, item)


def _configure_eq(device: ET.Element, filters: list) -> None:
    if len(filters) > 3:
        raise ValueError("At most three restrained EQ filters are supported")
    for path, item in (("Mode", 0), ("GlobalGain/Manual", 0), ("Scale/Manual", 1),
                       ("AuditionOnOff", False), ("AdaptiveQ/Manual", False)):
        put(device, path, item)
    for i in range(8):
        for side in ("ParameterA", "ParameterB"):
            base = f"Bands.{i}/{side}"
            put(device, f"{base}/IsOn/Manual", False)
            put(device, f"{base}/Gain/Manual", 0)
    for i, band in enumerate(filters):
        kind = band.get("kind")
        if kind not in {"highpass", "bell"}:
            raise ValueError("Unsupported EQ filter")
        frequency = number(band["frequency_hz"], "EQ frequency", 20, 16000)
        gain = number(band["gain_db"], "EQ gain", -3, 0)
        q = number(band["q"], "EQ Q", .5, 1)
        base = f"Bands.{i}/ParameterA"
        for key, item in (("IsOn", True), ("Mode", 1 if kind == "highpass" else 3),
                          ("Freq", frequency), ("Gain", gain), ("Q", q)):
            put(device, f"{base}/{key}/Manual", item)


def apply_plan(plan: dict, output: Path) -> list[str]:
    if plan.get("schema_version") != 1 or plan.get("kind") != "wcfi_soundcheck_plan":
        raise ValueError("Not a supported calibration plan")
    source = Path(plan["source_set"]).resolve()
    output = output.resolve()
    if output == source or output.exists():
        raise ValueError("Output must be a new file; the source and existing files cannot be overwritten")
    if output.parent != source.parent or output.suffix.lower() != ".als":
        raise ValueError("Write the .als copy beside its source so relative sample paths stay valid")
    if file_hash(source) != plan["source_sha256"]:
        raise ValueError("Source set changed since analysis; save it and calibrate again")
    root = read_set(source)
    utility_template = root.find(".//StereoGain")
    eq_template = root.find(".//Eq8")
    if utility_template is None or eq_template is None:
        raise ValueError("Source set needs stock Utility and EQ Eight devices as version-matched templates")
    utility_template = copy.deepcopy(utility_template)
    eq_template = copy.deepcopy(eq_template)
    lookup = {t.get("Id"): t for t in root.findall("./LiveSet/Tracks/AudioTrack")}
    applied = []
    seen = set()
    for item in plan["tracks"]:
        if item.get("status") != "ready":
            continue
        key = str(item["track_id"])
        if key in seen:
            raise ValueError("Plan contains duplicate track IDs")
        seen.add(key)
        if file_hash(Path(item["file"])) != item["audio_sha256"]:
            raise ValueError(f"Recording changed: {item['name']}; recalibrate")
        track = lookup.get(key)
        if track is None or value(track, "Name/EffectiveName") != item["name"]:
            raise ValueError("Track identity changed; regenerate profile")
        if value(track, "DeviceChain/AudioInputRouting/Target") != item["input_target"]:
            raise ValueError(f"Input routing changed on {item['name']}")
        chain = devices(track)
        gain_db = number(item["gain_db"], "input gain", -24, 12)
        owned_gain = [d for d in chain if value(d, "UserName") == GAIN_NAME]
        owned_eq = [d for d in chain if value(d, "UserName") == EQ_NAME]
        if len(owned_gain) > 1 or len(owned_eq) > 1:
            raise ValueError("Duplicate WCFI calibration devices")
        gain_device = owned_gain[0] if owned_gain else None
        eq_device = owned_eq[0] if owned_eq else None
        next_device = max([int(d.get("Id", 0)) for d in chain] + [-1]) + 1
        if gain_device is None:
            if len(chain) and chain[0].tag == "StereoGain":
                gain_device = chain[0]
            else:
                gain_device = _clone(utility_template, root, next_device)
                next_device += 1
                _neutral_utility(gain_device)
                chain.insert(0, gain_device)
        if gain_device.tag != "StereoGain" or list(chain).index(gain_device) != 0:
            raise ValueError("WCFI Input Gain must be the first device and must be Utility")
        if value(gain_device, "LegacyMode") != "false":
            raise ValueError("Legacy Utility is unsupported")
        if value(gain_device, "On/Manual") != "true" or value(gain_device, "Mute/Manual") != "false":
            raise ValueError(f"Input Utility is bypassed or muted on {item['name']}")
        _assert_unautomated(root, gain_device)
        put(gain_device, "UserName", GAIN_NAME)
        # Absolute value from RAW recordings, not an increment to last Sunday's gain.
        put(gain_device, "Gain/Manual", format(10 ** (gain_db / 20), ".10g"))
        if eq_device is None:
            eq_device = _clone(eq_template, root, next_device)
            chain.insert(1, eq_device)
        if eq_device.tag != "Eq8" or list(chain).index(eq_device) != 1:
            raise ValueError("WCFI Soundcheck EQ must be second and must be EQ Eight")
        _assert_unautomated(root, eq_device)
        put(eq_device, "UserName", EQ_NAME)
        put(eq_device, "On/Manual", True)
        _configure_eq(eq_device, item["eq"])
        applied.append(item["name"])
    if not applied:
        raise ValueError("No channels passed calibration; no set was written")
    # An all-or-nothing write after every ready track has been validated.
    if file_hash(source) != plan["source_sha256"]:
        raise ValueError("Source changed during apply; no set was written")
    payload = gzip.compress(ET.tostring(root, encoding="utf-8", xml_declaration=True), mtime=0)
    with output.open("xb") as stream:
        try:
            stream.write(payload)
        except BaseException:
            stream.close()
            output.unlink()
            raise
    return applied
