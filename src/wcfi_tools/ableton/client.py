"""CLI/app client for the local Remote Script. Never launches or focuses Live."""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from platformdirs import user_data_path


class AbletonError(RuntimeError):
    def __init__(self, message, *, code="unavailable", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def state_directory():
    override = os.environ.get("WCFI_ABLETON_HOME")
    return Path(override) if override else user_data_path("wcfi", appauthor=False) / "ableton-bridge"


def _descriptor(path):
    info = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(info, dict) or info.get("protocol") != 1 or type(info.get("port")) is not int
            or not 1 <= info["port"] <= 65535 or type(info.get("pid")) is not int
            or not isinstance(info.get("token"), str) or not 32 <= len(info["token"]) <= 128
            or not isinstance(info.get("instance_id"), str) or len(info["instance_id"]) != 32):
        raise ValueError("Invalid Ableton bridge descriptor")
    return info


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _fp, _code, _message, _headers, _url):
        return None  # Never forward the local capability to a redirected endpoint.


class AbletonClient:
    def __init__(self, descriptor, *, discovery_folder=None):
        self.descriptor = descriptor
        self.discovery_folder = Path(discovery_folder).resolve() if discovery_folder is not None else None

    def call(self, operation, arguments=None, *, request_id=None, timeout=7):
        identifier = request_id or uuid.uuid4().hex
        body = json.dumps({"id": identifier, "operation": operation,
                           "arguments": arguments or {}}, allow_nan=False).encode()
        info = self.descriptor
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/v1/rpc",
            data=body, headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + info["token"]}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                data = json.loads(response.read(16 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(65536)).get("error", {})
            except ValueError:
                error = {}
            raise AbletonError(error.get("message", "Local bridge rejected the request"),
                               code=error.get("code", "http_error")) from None
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise AbletonError("Bridge connection failed. A write may have started; inspect before retrying.",
                code="connection_failed", details={"request_id": identifier}) from exc
        if data.get("id") != identifier:
            raise AbletonError("Invalid bridge response ID", code="protocol_error")
        if not data.get("ok"):
            error = data.get("error", {})
            raise AbletonError(error.get("message", "Ableton operation failed"),
                code=error.get("code", "host_error"), details=dict(error.get("details", {}),
                                                                  request_id=identifier))
        return data["result"]


def connect(*, instance=None, folder=None):
    active = []
    discovery_folder = Path(folder) if folder is not None else state_directory()
    for path in sorted(discovery_folder.glob("bridge-*.json")):
        try:
            descriptor = _descriptor(path)
            if instance and not descriptor["instance_id"].startswith(instance):
                continue
            client = AbletonClient(descriptor, discovery_folder=discovery_folder)
            status = client.call("status", timeout=1.5)
            if status.get("session_id") == descriptor["instance_id"]:
                active.append(client)
        except (OSError, ValueError, AbletonError):
            continue
    if len(active) > 1:
        raise AbletonError("Multiple Live bridges are active; specify --instance with one of: " +
            ", ".join(client.descriptor["instance_id"] for client in active), code="ambiguous")
    if not active:
        raise AbletonError("Ableton bridge is not connected. Run 'wcfi audio ableton install', then select "
                           "WCFI_Bridge in Live Preferences > Link/Tempo/MIDI > Control Surface "
                           "(Input and Output: None). No Play command is needed.")
    return active[0]


def reload_bridge(client, expected_bundle_id=None, *, dry_run=False,
                  discard_receipts=False, timeout=20):
    """Reload one installed bundle and reconcile its receipt without retrying a write.

    Discovery is restricted to this client's Live process. Only an exact reload
    receipt on a new coherent session establishes success; an unrelated restart
    or another bridge never counts. On success the supplied client adopts the
    fresh descriptor. An unknown outcome keeps the old client untouched.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise AbletonError("Reload timeout must be a positive finite number", code="invalid_argument")
    if type(dry_run) is not bool or type(discard_receipts) is not bool:
        raise AbletonError("Reload flags must be booleans", code="invalid_argument")
    deadline = time.monotonic() + timeout
    old = dict(client.descriptor)
    status = client.call("status", timeout=min(7., timeout))
    if not isinstance(status, dict):
        raise AbletonError("Invalid bridge status before reload", code="protocol_error")
    if status.get("session_id") != old["instance_id"]:
        raise AbletonError("Bridge session changed before reload; reconnect and inspect first",
                           code="stale_session")
    capability = status.get("reload", {})
    if ("reload_bridge" not in status.get("operations", ())
            or not isinstance(capability, dict) or capability.get("supported") is not True
            or capability.get("bootstrap_current") is not True):
        raise AbletonError("This running bridge needs the reload bootstrap. Run 'wcfi audio ableton "
            "install', then save your set and restart Live once to activate it. Subsequent supported "
            "bundle updates can use 'wcfi audio ableton reload'. No Play command is needed.",
            code="bootstrap_update_required", details={"reload": capability,
                "bridge_revision": status.get("bridge_revision")})
    bundle = expected_bundle_id if expected_bundle_id is not None else capability.get("installed_bundle_id")
    if not isinstance(bundle, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle):
        raise AbletonError("Expected an installed bundle SHA-256 ID from status or install",
                           code="invalid_argument")
    identifier = uuid.uuid4().hex
    arguments = {"session_id": old["instance_id"], "expected_bundle_id": bundle,
                 "reload_id": identifier, "dry_run": dry_run,
                 "discard_receipts": discard_receipts}
    details = {"reload_id": identifier, "request_id": identifier,
               "old_session_id": old["instance_id"], "bundle_id": bundle, "pid": old["pid"]}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AbletonError("Reload deadline expired before sending any mutation",
                           code="reload_not_sent", details=details)
    acknowledgement = None
    try:
        # Exactly one mutation attempt. A lost acknowledgement is reconciled below.
        acknowledgement = client.call("reload_bridge", arguments, request_id=identifier,
                                      timeout=min(7., remaining))
    except AbletonError as exc:
        uncertain = {"connection_failed", "outcome_unknown", "protocol_error", "http_error",
                     "disconnected", "reloading"}
        if dry_run or exc.code not in uncertain:
            raise
        details["acknowledgement_error"] = {"code": exc.code, "message": str(exc)}
    if dry_run:
        if (not isinstance(acknowledgement, dict) or acknowledgement.get("verified") is not True
                or acknowledgement.get("dry_run") is not True
                or acknowledgement.get("changed") is not False):
            raise AbletonError("Invalid reload dry-run result", code="protocol_error", details=details)
        return acknowledgement
    if acknowledgement is not None and (
            not isinstance(acknowledgement, dict) or acknowledgement.get("reload_id") != identifier
            or acknowledgement.get("old_session_id") != old["instance_id"]
            or acknowledgement.get("bundle_id") != bundle
            or acknowledgement.get("state") != "accepted"
            or acknowledgement.get("dry_run") is not False):
        details["acknowledgement_error"] = {"code": "protocol_error",
            "message": "Reload acknowledgement did not match the submitted request"}
    folder = getattr(client, "discovery_folder", None) or state_directory()
    while time.monotonic() < deadline:
        for path in sorted(Path(folder).glob("bridge-*.json")):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                descriptor = _descriptor(path)
                if descriptor["pid"] != old["pid"]:
                    continue
                candidate = AbletonClient(descriptor, discovery_folder=folder)
                observed = candidate.call("status", timeout=min(1., remaining))
            except (OSError, ValueError, AbletonError):
                # Stale descriptors, shutdown and startup intervals are expected.
                continue
            if not isinstance(observed, dict) or observed.get("session_id") != descriptor["instance_id"]:
                continue
            reload_status = observed.get("reload", {})
            if not isinstance(reload_status, dict):
                continue
            receipt = reload_status.get("last_reload")
            if (not isinstance(receipt, dict) or receipt.get("reload_id") != identifier
                    or receipt.get("old_session_id") != old["instance_id"]
                    or receipt.get("bundle_id") != bundle):
                continue
            if receipt.get("state") == "failed" and descriptor["instance_id"] == old["instance_id"]:
                raise AbletonError("Bridge reload failed; the previous runtime remains active",
                    code="reload_failed", details={**details, "failure": receipt.get("error")})
            if (receipt.get("state") != "complete" or descriptor["instance_id"] == old["instance_id"]
                    or reload_status.get("active_bundle_id") != bundle
                    or reload_status.get("supported") is not True
                    or reload_status.get("bootstrap_current") is not True
                    or "reload_bridge" not in observed.get("operations", ())):
                continue
            client.descriptor = descriptor
            client.discovery_folder = Path(folder)
            return {**details, "session_id": descriptor["instance_id"],
                    "bridge_revision": observed.get("bridge_revision"), "state": "complete",
                    "verified": True, "changed": True, "dry_run": False,
                    "discard_receipts": discard_receipts,
                    "acknowledgement_received": acknowledgement is not None
                        and "acknowledgement_error" not in details}
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.1, remaining))
    raise AbletonError("Reload outcome is unknown after the deadline. Inspect status and its "
        "last_reload receipt before retrying; no second reload was sent. Request ID: " + identifier,
        code="reload_outcome_unknown", details=details)


def choose(items, selector, label):
    matches = [item for item in items if item["id"] == selector]
    if not matches:
        matches = [item for item in items if item["name"].casefold() == selector.casefold()]
    if len(matches) != 1:
        raise AbletonError(f"{label} selector {selector!r} matched {len(matches)} objects; "
                           "use an exact name or an ID from inspect", code="ambiguous_target")
    return matches[0]


def choose_track(snapshot, selector):
    if selector.casefold() == "master":
        return next(track for track in snapshot["tracks"] if track["kind"] == "master")
    return choose(snapshot["tracks"], selector, "Track")


def devices(track):
    for device in track["devices"]:
        yield device
        for chain in device["chains"]:
            yield from devices(chain)


def containers(snapshot):
    def descend(container):
        yield container
        for device in container["devices"]:
            for chain in device["chains"]:
                yield from descend(chain)
    for track in snapshot["tracks"]:
        yield from descend(track)


def set_fader(client, track, db, *, relative=False, dry_run=False):
    snapshot = client.call("inspect_session")
    target = choose_track(snapshot, track)
    return client.call("set_fader", {"session_id": snapshot["session_id"], "track_id": target["id"],
        "db": db, "expected_value": target["volume"]["value"], "relative": relative,
        "dry_run": dry_run})


def set_parameter(client, track, device, parameter, value, *, normalized=False, dry_run=False):
    snapshot = client.call("inspect_session")
    target = choose(list(devices(choose_track(snapshot, track))), device, "Device")
    control = choose(target["parameters"], parameter, "Parameter")
    return client.call("set_parameter", {"session_id": snapshot["session_id"],
        "parameter_id": control["id"], "expected_value": control["value"], "value": value,
        "normalized": normalized, "dry_run": dry_run})


def inspect_eq8_state(client, track, device):
    """Inspect fixed native EQ Eight properties without changing the host."""
    snapshot = client.call("inspect_session")
    target = choose(list(devices(choose_track(snapshot, track))), device, "Device")
    return client.call("inspect_eq8_state", {"session_id": snapshot["session_id"],
                                            "device_id": target["id"]})


def set_eq8_mode(client, track, device, mode, *, dry_run=False):
    """Set one native mode once; do not retry an uncertain host mutation."""
    if type(mode) is not int or mode not in (0, 1, 2) or type(dry_run) is not bool:
        raise AbletonError("EQ Eight mode must be 0, 1 or 2 and dry_run a boolean",
                           code="invalid_argument")
    snapshot = client.call("inspect_session")
    target = choose(list(devices(choose_track(snapshot, track))), device, "Device")
    state = client.call("inspect_eq8_state", {"session_id": snapshot["session_id"],
                                             "device_id": target["id"]})
    if (state.get("supported") is not True or type(state.get("global_mode")) is not int
            or state["global_mode"] not in (0, 1, 2)):
        raise AbletonError("EQ Eight native mode is unavailable or ambiguous", code="unsupported",
                           details=state)
    return client.call("set_eq8_mode", {"session_id": snapshot["session_id"],
        "device_id": target["id"], "expected_mode": state["global_mode"],
        "mode": mode, "dry_run": dry_run})


def move_device(client, track, device, index, *, to_track=None, to_chain=None, dry_run=False):
    snapshot = client.call("inspect_session")
    target_device = choose(list(devices(choose_track(snapshot, track))), device, "Device")
    all_containers = list(containers(snapshot))
    source = next(item for item in all_containers if any(
        candidate["id"] == target_device["id"] for candidate in item["devices"]))
    if to_track and to_chain:
        raise AbletonError("Choose --to-track or --to-chain, not both", code="invalid_argument")
    if to_chain:
        destination = choose(all_containers, to_chain, "Destination chain")
    elif to_track:
        destination = choose_track(snapshot, to_track)
    else:
        destination = source
    return client.call("move_device", {"session_id": snapshot["session_id"],
        "device_id": target_device["id"], "target_id": destination["id"], "index": index,
        "expected_source_order": [item["id"] for item in source["devices"]],
        "expected_target_order": [item["id"] for item in destination["devices"]],
        "dry_run": dry_run})


def _require_operation(client, operation):
    status = client.call("status")
    revision = status.get("bridge_revision")
    insertion_update = operation in ("insert_device", "probe_insertion_context") and (
        type(revision) is not int or revision < 6)
    if operation not in status.get("operations", ()) or insertion_update:
        raise AbletonError(
            "The running bridge needs an update for this operation. Run 'wcfi audio ableton "
            "install', save your set and restart Live; check bridge_revision 6 or later in status. "
            "Do not press Play.", code="bridge_update_required",
            details={"operation": operation, "bridge_revision": revision})
    return status


def browse_devices(client, *, parent=None, offset=0, limit=100):
    """Read one page of Audio Effects browser items; IDs are session-local."""
    status = _require_operation(client, "list_browser_items")
    return client.call("list_browser_items", {"session_id": status["session_id"],
        "root": "audio_effects", "parent_id": parent, "offset": offset, "limit": limit})


def insert_device(client, track, item_id, index, *, dry_run=False):
    """Insert an exact browsed Audio Effect into a top-level track device chain."""
    _require_operation(client, "insert_device")
    snapshot = client.call("inspect_session")
    target = choose_track(snapshot, track)
    return client.call("insert_device", {"session_id": snapshot["session_id"],
        "item_id": item_id, "target_id": target["id"], "index": index,
        "expected_target_order": [item["id"] for item in target["devices"]],
        "dry_run": dry_run})


def remove_inserted_device(client, insertion_id, *, dry_run=False):
    """Revert this bridge's own insertion using its receipt, never a device name."""
    status = _require_operation(client, "remove_inserted_device")
    return client.call("remove_inserted_device", {"session_id": status["session_id"],
        "insertion_id": insertion_id, "dry_run": dry_run})


def inspect_insertion_context(client, track):
    """Read Live's insertion context without changing selection or loading a device."""
    _require_operation(client, "inspect_insertion_context")
    snapshot = client.call("inspect_session")
    target = choose_track(snapshot, track)
    return client.call("inspect_insertion_context", {"session_id": snapshot["session_id"],
        "target_id": target["id"]})


def probe_insertion_context(client, track, *, dry_run=False):
    """Check selection/mode setup and restoration without loading any device."""
    _require_operation(client, "probe_insertion_context")
    snapshot = client.call("inspect_session")
    target = choose_track(snapshot, track)
    return client.call("probe_insertion_context", {"session_id": snapshot["session_id"],
        "target_id": target["id"],
        "expected_target_order": [item["id"] for item in target["devices"]],
        "dry_run": dry_run})
