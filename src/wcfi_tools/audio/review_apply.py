"""Narrow output-gain adapter. Preview policy never authorizes arbitrary writes."""
from __future__ import annotations

import math
import re

from ..ableton.client import choose_track
from .role_analysis import revision


def gain_target(report):
    if report["provenance"].get("post_fx_kind") != "current_node_capture":
        raise ValueError("Capture current Ableton nodes before applying; raw-stem previews cannot authorize a DAW change")
    state = report.get("chain_state")
    if not state or not state.get("session_id"):
        raise ValueError("No captured Ableton chain state")
    devices = state["track"]["devices"]
    if (len(devices) < 3 or devices[0]["name"] != "WCFI Analysis Node"
            or devices[-1]["name"] != "WCFI Analysis Node"
            or sum(d["name"] == "WCFI Analysis Node" for d in devices) != 2):
        raise ValueError("The before/after Analysis Nodes must bracket this track's effects")
    device = devices[-2]
    if device["class_name"] != "Compressor2" or device.get("chains"):
        raise ValueError("Apply currently supports Ableton Compressor Output Gain immediately before the after-effects node")
    params = {p["name"]: p for p in device["parameters"]}
    for key, value in (("Device On", 1), ("Makeup", 0), ("Dry/Wet", 1)):
        if key not in params or params[key]["value"] != value:
            raise ValueError("Compressor must be enabled, fully wet and have automatic makeup off")
    if any(not any(p["name"] == "Device On" and p["value"] == 1 for p in d["parameters"])
           for d in (devices[0], devices[-1])):
        raise ValueError("Both Analysis Nodes must be enabled")
    parameter = params.get("Output Gain")
    if (parameter is None or parameter["min"] != -36 or parameter["max"] != 36
            or parameter["is_quantized"] or not parameter["is_enabled"]
            or parameter["automation_state"] != 0):
        raise ValueError("Compressor Output Gain is unavailable, automated or has an unsupported scale")
    actions = [p for p in report["proposals"] if p["kind"] == "software_gain"]
    if len(actions) != 1:
        raise ValueError("No single supported gain proposal")
    gain = actions[0]["gain_db"]
    value = parameter["value"] + gain
    if not math.isfinite(gain) or not -12 <= gain < 0 or not -36 <= value <= 36:
        raise ValueError("Proposed gain is outside the supported attenuation range")
    return state, device, parameter, value


def availability(report):
    try:
        _, device, parameter, value = gain_target(report)
        return {"supported": True, "target": device["name"] + " → " + parameter["name"],
                "from_db": parameter["value"], "to_db": value,
                "note": "Rechecks the captured track and host parameter scale when you apply. Then capture again to verify audio."}
    except ValueError as exc:
        return {"supported": False, "reason": str(exc)}


def _check_current(client, state):
    snapshot = client.call("inspect_session")
    current = {"session_id": snapshot["session_id"], "track": choose_track(snapshot, state["track"]["id"])}
    if revision(current) != revision(state):
        raise ValueError("Ableton track or session changed since capture. Analyse the nodes again before applying.")


def apply_gain(client, report, *, request_id):
    state, _device, parameter, value = gain_target(report)
    if client.descriptor.get("pid") != report["provenance"]["host_pid"]:
        raise ValueError("Ableton host changed; capture again")
    args = {"session_id": state["session_id"], "parameter_id": parameter["id"],
            "expected_value": parameter["value"], "value": value, "normalized": False}
    _check_current(client, state)
    plan = client.call("set_parameter", args | {"dry_run": True})
    # Never assume that a normalized plug-in control is a dB scale.
    text = str(plan.get("requested_display", "")).replace("−", "-").replace(",", ".")
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*dB\s*", text, re.IGNORECASE)
    if match is None or abs(float(match[1]) - value) > .11:
        raise ValueError("Host gain conversion did not match the proposed dB value; nothing applied")
    _check_current(client, state)
    result = client.call("set_parameter", args, request_id=request_id)
    if not result.get("verified"):
        raise RuntimeError("Gain write was not verified. Inspect Ableton before another change.")
    return {"status": "applied", "target": "Compressor → Output Gain", "from_db": parameter["value"],
            "to_db": value, "parameter_readback_verified": True, "audio_verified": False,
            "note": "Host parameter readback verified. Capture nodes again to verify the resulting audio. Ableton Undo can restore the change."}
