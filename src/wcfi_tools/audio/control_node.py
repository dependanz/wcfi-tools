"""Owned, single-parameter configuration of WCFI Control Node through Live.

The plugin owns the audio-rate loop. This adapter only inspects and configures
host parameters; it never loads devices or changes transport, routing or faders.
No retries, implicit rebinding, or batch rollback occur after an uncertain write.
"""
from __future__ import annotations

from copy import deepcopy
import math
import uuid


PARAMETERS = {
    "auto": ("Auto Enable", 0., 1.),
    "trim-db": ("Trim dB", -24., 24.),
    "target-rms": ("Target RMS", -36., -12.),
    "max-boost": ("Max Boost", 0., 24.),
    "max-cut": ("Max Cut", 0., 24.),
    "activity-threshold": ("Activity Threshold", -90., -24.),
}
RED_PARAMETERS = {**PARAMETERS,
    "auto": ("Calibrate & Hold", 0., 1.),
    "trim-db": ("Trim dB", -6., 6.),
    "max-boost": ("Max Boost", 0., 6.),
    "max-cut": ("Max Cut", 0., 6.),
}
PROFILES = {"WCFI Control Node": ("legacy", PARAMETERS),
            "WCFI Control Node RED": ("red-single-mic", RED_PARAMETERS)}
SCHEMA = "wcfi-control-node-binding-v1"
METERS = {
    "input_rms_dbfs": ("Input RMS", -161., 48.),
    "input_peak_dbfs": ("Input Peak", -161., 48.),
    "output_rms_dbfs": ("Output RMS", -161., 48.),
    "output_peak_dbfs": ("Output Peak", -161., 48.),
    "auto_gain_db": ("Auto Gain", -25., 24.),
    "applied_gain_db": ("Applied Gain", -193., 24.),
    "guard_reduction_db": ("Guard Reduction", -1., 192.),
    "state_code": ("Activity State", -1., 3.),
    "activity_code": ("Activity Gate", -1., 1.),
    "sequence": ("Telemetry Sequence", 0., 65535.),
}


class ControlNodeError(RuntimeError):
    pass


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ControlNodeError("Expected a finite number")
    return float(value)


def _one(items, selector, kind):
    if not isinstance(selector, str) or not selector or not isinstance(items, list):
        raise ControlNodeError(f"Invalid {kind} selector or inventory")
    if any(not isinstance(item, dict) for item in items):
        raise ControlNodeError(f"Invalid {kind} inventory")
    exact = [item for item in items if item.get("id") == selector]
    matches = exact or [item for item in items if isinstance(item.get("name"), str)
                       and item["name"].casefold() == selector.casefold()]
    if len(matches) != 1:
        raise ControlNodeError(f"Expected one {kind} matching {selector!r}; found {len(matches)}")
    return matches[0]


def _profile(device_name):
    if not isinstance(device_name, str) or device_name not in PROFILES:
        raise ControlNodeError("Unrecognized Control Node identity; do not infer parameter ranges")
    return PROFILES[device_name]


def _telemetry(device, profile="legacy"):
    """Decode read-only host meters; a single snapshot cannot prove freshness."""
    values = {}
    parameters = device.get("parameters", [])
    if not any(p.get("name") == "Telemetry Sequence" for p in parameters):
        return {"available": False, "delivery": "unavailable",
                "reason": "The host parameter snapshot does not expose telemetry; check the native editor"}
    waiting = False
    for key, (name, low, high) in METERS.items():
        if key == "state_code" and profile == "red-single-mic":
            high = 4.
        parameter = _one(parameters, name, "read-only meter")
        minimum, maximum, raw = (_number(parameter.get(k)) for k in ("min", "max", "value"))
        if not minimum < maximum or not minimum <= raw <= maximum:
            raise ControlNodeError("Invalid meter range")
        normalized = (raw - minimum) / (maximum - minimum)
        waiting |= normalized <= 0
        value = low + normalized * (high - low)
        values[key] = round(value) if key in ("state_code", "activity_code", "sequence") else value
    states = ({-1: "waiting_for_telemetry", 0: "manual", 1: "waiting_for_activity",
               2: "collecting", 3: "ramping", 4: "holding"} if profile == "red-single-mic" else
              {-1: "waiting", 0: "manual", 1: "hold", 2: "qualifying", 3: "active"})
    state = states[values["state_code"]]
    return {"available": True, "ready": not waiting, "delivery": "unverified_single_snapshot",
            "state": state, "values": values,
            "note": "Read-only host observations; not sample-aligned with the Analysis Nodes"}


def compare_telemetry(first, second):
    """Compare two bindings without treating moving meter values as owned controls."""
    for key in ("session_id", "track_id", "device_id", "device_name", "profile", "device_order", "analysis_node_ids"):
        if first.get(key) != second.get(key):
            raise ControlNodeError("Node identity changed between meter inspections")
    result = deepcopy(second["telemetry"])
    a, b = first.get("telemetry", {}), second["telemetry"]
    if a.get("ready") and b.get("ready"):
        result["delivery"] = ("advanced_between_inspections" if a["values"]["sequence"] != b["values"]["sequence"]
                              else "not_confirmed_between_inspections")
    return result


def bind(snapshot, track, device="WCFI Control Node"):
    """Bind a named profile and its distinct parameter names; no host mutations."""
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("session_id"), str)
            or not snapshot["session_id"]):
        raise ControlNodeError("Missing session identity")
    selected_track = _one(snapshot.get("tracks", []), track, "track")
    devices = selected_track.get("devices", [])
    selected = _one(devices, device, "top-level device")
    profile, definitions = _profile(selected.get("name"))
    index = devices.index(selected)
    if not 0 < index < len(devices) - 1:
        raise ControlNodeError("Control Node must be between its two Analysis Nodes")
    if profile == "red-single-mic":
        before = [item for item in devices[:index] if item.get("name") == "WCFI Analysis Node"]
        after = [item for item in devices[index + 1:] if item.get("name") == "WCFI Analysis Node"]
        if len(before) != 1 or len(after) != 1:
            raise ControlNodeError("RED Control Node requires exactly one Analysis Node on each side")
        neighbors = [before[0], after[0]]
    else:
        neighbors = [devices[index - 1], devices[index + 1]]
        if any(item.get("name") != "WCFI Analysis Node" for item in neighbors):
            raise ControlNodeError("Immediate neighbors must be WCFI Analysis Nodes")
    controls = {}
    for setting, (name, low, high) in definitions.items():
        control = _one(selected.get("parameters", []), name, "Control Node parameter")
        minimum, maximum, raw = (_number(control.get(key)) for key in ("min", "max", "value"))
        if not minimum < maximum or not minimum <= raw <= maximum:
            raise ControlNodeError("Invalid host parameter range")
        if (control.get("is_enabled") is not True or type(control.get("automation_state")) is not int
                or control["automation_state"] != 0):
            raise ControlNodeError(f"{name} is disabled or automated")
        controls[setting] = {"id": control["id"], "name": name, "value": raw,
            "min": minimum, "max": maximum, "display": control.get("display"),
            "plain_value": low + (raw - minimum) / (maximum - minimum) * (high - low)}
    ids = [selected_track.get("id")] + [item.get("id") for item in devices]
    ids += [item["id"] for item in controls.values()]
    if any(not isinstance(identifier, str) or not identifier for identifier in ids):
        raise ControlNodeError("Missing host object identity")
    if len(ids) != len(set(ids)):
        raise ControlNodeError("Duplicate host object identities")
    return {"schema": SCHEMA, "profile": profile, "session_id": snapshot["session_id"],
            "track_id": selected_track["id"], "track_name": selected_track["name"],
            "device_id": selected["id"], "device_name": selected["name"],
            "device_order": [item["id"] for item in devices],
            "analysis_node_ids": [item["id"] for item in neighbors], "parameters": controls,
            "telemetry": _telemetry(selected, profile),
            "placement_note": "Host chain order only; labels/stages require verification. RED's bracket may contain existing processing; full device order is owned."}


def _compare(owned, fresh):
    if owned.get("schema") != SCHEMA:
        raise ControlNodeError("Unsupported binding schema")
    for key in ("session_id", "track_id", "device_id", "device_name", "device_order", "analysis_node_ids"):
        if owned.get(key) != fresh[key]:
            raise ControlNodeError(f"Binding changed: {key}")
    expected_profile = _profile(owned.get("device_name"))[0]
    if owned.get("profile", expected_profile) != expected_profile or fresh.get("profile") != expected_profile:
        raise ControlNodeError("Control Node policy identity changed")
    if set(owned.get("parameters", {})) != set(PARAMETERS):
        raise ControlNodeError("Incomplete owned parameter binding")
    for setting in PARAMETERS:
        previous, current = owned["parameters"][setting], fresh["parameters"][setting]
        if any(previous.get(key) != current[key] for key in ("id", "min", "max", "name")):
            raise ControlNodeError(f"Parameter identity/range changed: {setting}")
        if abs(_number(previous.get("value")) - current["value"]) > 1e-6:
            raise ControlNodeError(f"Manual override or other writer changed {setting}")


def configure_one(client, owned, setting, value, *, apply=False, journal=None):
    """Inspect and issue one CAS command against the ORIGINAL owned value.

    Dry-run is the default. An apply requires a synchronous durable journal.
    The caller must stop using the binding on any exception, particularly an
    uncertain outcome. Other parameters/topology cannot be atomically leased
    by the current bridge; readback detects a concurrent change after the fact.
    """
    if type(apply) is not bool or setting not in PARAMETERS:
        raise ControlNodeError("Invalid setting or apply flag")
    value = _number(value)
    if not isinstance(owned, dict) or owned.get("schema") != SCHEMA:
        raise ControlNodeError("Invalid owned binding")
    profile, definitions = _profile(owned.get("device_name"))
    if owned.get("profile", profile) != profile:
        raise ControlNodeError("Invalid policy profile")
    name, low, high = definitions[setting]
    if not low <= value <= high or (setting == "auto" and value not in (0., 1.)):
        raise ControlNodeError(f"{setting} must be within {low}..{high}")
    if apply and not callable(journal):
        raise ControlNodeError("Apply requires a durable journal sink")
    if not isinstance(owned, dict) or owned.get("schema") != SCHEMA:
        raise ControlNodeError("Invalid owned binding")
    original = deepcopy(owned)
    fresh = bind(client.call("inspect_session"), original["track_id"], original["device_id"])
    _compare(original, fresh)
    control = original["parameters"][setting]
    normalized = (value - low) / (high - low)
    expected_raw = control["min"] + normalized * (control["max"] - control["min"])
    arguments = {"session_id": original["session_id"], "parameter_id": control["id"],
                 "expected_value": control["value"], "value": normalized,
                 "normalized": True, "dry_run": not apply}
    request_id = uuid.uuid4().hex
    event = {"event": "write_pending" if apply else "dry_run_pending", "request_id": request_id,
             "setting": setting, "plain_value": value, "arguments": arguments,
             "binding": original}
    if journal is not None:
        journal(deepcopy(event))
    try:
        result = client.call("set_parameter", arguments, request_id=request_id)
        before = result.get("before", {})
        if (result.get("dry_run") is not (not apply) or before.get("id") != control["id"]
                or abs(_number(before.get("value")) - control["value"]) > 1e-6
                or abs(_number(result.get("requested_value")) - expected_raw) > 1e-5):
            raise ControlNodeError("Parameter receipt did not match the submitted command")
        following = deepcopy(original)
        if apply:
            after = result.get("after", {})
            if (result.get("verified") is not True or after.get("id") != control["id"]
                    or abs(_number(after.get("value")) - expected_raw) > 1e-5):
                raise ControlNodeError("Write was not verified; stop before any further commands")
            following["parameters"][setting].update(value=after["value"], display=after.get("display"),
                                                     plain_value=value)
            readback = bind(client.call("inspect_session"), original["track_id"], original["device_id"])
            _compare(following, readback)
            following = readback
        outcome = {"event": "confirmed" if apply else "dry_run", "request_id": request_id,
                   "setting": setting, "plain_value": value, "receipt": result,
                   "next_binding": following, "audio_quality_validated": False,
                   "no_retry": True}
        if journal is not None:
            journal(deepcopy(outcome))
        return outcome
    except Exception as exc:
        if journal is not None:
            journal({"event": "stop_inspect_before_further_commands", "request_id": request_id,
                     "error": type(exc).__name__ + ": " + str(exc),
                     "write_may_have_applied": apply})
        raise
