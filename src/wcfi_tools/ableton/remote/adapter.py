"""Live operations. Every method must run on Live's main thread (Python 3.7+).

This module deliberately has no transport setters, arbitrary attribute writes,
eval, clip launching, save, or routing mutations. Device insertion temporarily
targets a track through Live's API and restores its view state.
"""
import math
import re
import time
import uuid


class BridgeError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BridgeError("invalid_argument", "Expected a finite number")
    return float(value)


def readable(obj, name, default=None):
    try:
        return getattr(obj, name)
    except (AttributeError, RuntimeError):
        return default


def display(parameter, value=None):
    try:
        return str(parameter.str_for_value(parameter.value if value is None else value))
    except (AttributeError, RuntimeError):
        return str(parameter) if value is None else None


def display_db(parameter, value=None):
    text = display(parameter, value)
    if text is None:
        raise BridgeError("unsupported", "Live does not expose this fader's dB conversion")
    text = text.strip().lower().replace("\u2212", "-").replace(",", ".")
    if text in ("-inf", "-inf db", "-\u221e", "-\u221e db"):
        return -math.inf
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*db", text)
    if not match:
        raise BridgeError("unsupported", "Cannot interpret Live's fader display: " + text)
    return float(match.group(1))


class Registry:
    """Opaque IDs survive scans and moves, but never resolve deleted objects.

    Live supplies hashable object wrappers. Unhashable test/host objects use a
    conservative equality fallback. Never identify targets by selection/index.
    """
    def __init__(self):
        self.buckets = {}
        self.current = {}
        self.next_buckets = {}

    def start(self):
        self.current = {}
        self.next_buckets = {}

    def add(self, obj, kind):
        if len(self.current) >= 16384:
            raise BridgeError("session_too_large", "Session exceeds the bridge object limit")
        try:
            key = (kind, hash(obj))
        except TypeError:
            key = (kind, None)
        candidates = self.next_buckets.get(key, []) + self.buckets.get(key, [])
        identifier = None
        for previous, previous_id in candidates:
            try:
                if previous is obj or previous == obj:
                    identifier = previous_id
                    break
            except RuntimeError:
                pass
        if identifier is None:
            identifier = kind + "_" + uuid.uuid4().hex
        if identifier not in self.current:
            self.next_buckets.setdefault(key, []).append((obj, identifier))
            self.current[identifier] = (kind, obj)
        return identifier

    def finish(self):
        self.buckets = self.next_buckets

    def get(self, identifier, kind):
        entry = self.current.get(identifier)
        if entry is None or entry[0] != kind:
            raise BridgeError("stale_target", "Target no longer exists; inspect the session again")
        return entry[1]


class LiveAdapter:
    MAX_OBJECTS = 16384

    def __init__(self, song, application=None):
        self.song = song
        self.application = application
        self.session_id = uuid.uuid4().hex
        self.registry = Registry()
        self.locations = {}
        self.containers = {}
        from .browser import DeviceBrowser

        self.device_browser = DeviceBrowser(self)
        self.eq8_last_mutation = None
        # Explicit operation table: extending the bridge is a code change, not
        # permission for a remote caller to invoke arbitrary Live methods.
        self.operations = {
            "status": self.status,
            "inspect_session": self.inspect_session,
            "set_fader": self.set_fader,
            "set_parameter": self.set_parameter,
            "inspect_eq8_state": self.inspect_eq8_state,
            "set_eq8_mode": self.set_eq8_mode,
            "move_device": self.move_device,
            "list_browser_items": self.device_browser.list_items,
            "inspect_insertion_context": self.device_browser.inspect_context,
            "probe_insertion_context": self.device_browser.probe_context,
            "insert_device": self.device_browser.insert,
            "remove_inserted_device": self.device_browser.remove,
        }

    def execute(self, operation, arguments):
        handler = self.operations.get(operation)
        if handler is None:
            raise BridgeError("unknown_operation", "Unsupported bridge operation")
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_argument", "arguments must be an object")
        if (self.device_browser.uncertain and operation in
                ("set_fader", "set_parameter", "set_eq8_mode", "move_device", "insert_device", "probe_insertion_context")):
            raise BridgeError("insertion_unresolved", "Inspect the unresolved device load before further writes; remove its guarded receipt if available or restart the bridge after recovery")
        for name in ("normalized", "relative", "dry_run"):
            if name in arguments and type(arguments[name]) is not bool:
                raise BridgeError("invalid_argument", name + " must be a boolean")
        return handler(**arguments)

    def status(self):
        version = None
        if self.application is not None:
            try:
                version = ".".join(str(getattr(self.application, name)()) for name in (
                    "get_major_version", "get_minor_version", "get_bugfix_version"))
            except AttributeError:
                pass
        return {"session_id": self.session_id, "live_version": version, "bridge_revision": 8,
                "operations": sorted(self.operations), "transport_control": False,
                "eq8_last_mutation": self.eq8_last_mutation,
                "capabilities": {"move_device": callable(readable(self.song, "move_device")),
                                 "parameter_write": True, "fader_write": True,
                                 **self.device_browser.capabilities()}}

    def _parameter(self, parameter):
        if len(self.registry.current) >= self.MAX_OBJECTS:
            raise BridgeError("session_too_large", "Session exceeds the bridge object limit")
        state = readable(parameter, "automation_state")
        try:
            state = int(state) if state is not None else None
        except (TypeError, ValueError):
            state = None
        items = tuple(readable(parameter, "value_items", ()) or ())
        return {"id": self.registry.add(parameter, "parameter"), "name": str(parameter.name),
                "value": float(parameter.value), "min": float(parameter.min),
                "max": float(parameter.max), "display": display(parameter),
                "is_enabled": bool(readable(parameter, "is_enabled", False)),
                "is_quantized": bool(readable(parameter, "is_quantized", False)),
                "automation_state": state, "value_items": [str(item) for item in items[:256]],
                "value_items_count": len(items)}

    def _chain(self, container, kind, depth=0, ancestors=()):
        if depth > 12:
            raise BridgeError("session_too_large", "Rack nesting exceeds 12 levels")
        chain_id = self.registry.add(container, kind)
        device_objects = tuple(container.devices)
        device_ids = [self.registry.add(device, "device") for device in device_objects]
        self.containers[chain_id] = {"object": container, "device_ids": device_ids,
                                     "ancestors": ancestors}
        result = []
        for index, (device, device_id) in enumerate(zip(device_objects, device_ids)):
            self.locations[device_id] = (chain_id, index)
            children = []
            if readable(device, "can_have_chains", False):
                for chain in tuple(readable(device, "chains", ())):
                    child_id, devices = self._chain(chain, "chain", depth + 1,
                                                    ancestors + (device_id,))
                    children.append({"id": child_id, "name": str(chain.name), "devices": devices})
            description = {"id": device_id, "name": str(device.name), "index": index,
                           "class_name": str(readable(device, "class_name", "")),
                           "type": str(readable(device, "type", "")),
                           "parameters": [self._parameter(p) for p in tuple(device.parameters)],
                           "chains": children}
            if description["class_name"] == "Eq8":
                description["native_state"] = self._eq8_state(device)
            result.append(description)
        return chain_id, result

    def inspect_session(self):
        self.registry.start()
        self.locations = {}
        self.containers = {}
        tracks = []
        groups = (("track", tuple(self.song.tracks)),
                  ("return", tuple(self.song.return_tracks)),
                  ("master", (self.song.master_track,)))
        for kind, members in groups:
            for index, track in enumerate(members):
                track_id, devices = self._chain(track, "track")
                mixer = track.mixer_device
                routing = {}
                for name in ("input_routing_type", "input_routing_channel",
                             "output_routing_type", "output_routing_channel"):
                    route = readable(track, name)
                    routing[name] = str(readable(route, "display_name", route)) if route else None
                group = readable(track, "group_track")
                tracks.append({"id": track_id, "name": str(track.name), "kind": kind,
                               "index": index, "is_group": bool(readable(track, "is_foldable", False)),
                               "group_id": self.registry.add(group, "track") if group else None,
                               "mute": bool(readable(track, "mute", False)),
                               "solo": bool(readable(track, "solo", False)),
                               "routing": routing, "devices": devices,
                               "volume": self._parameter(mixer.volume),
                               "pan": self._parameter(mixer.panning),
                               "sends": [self._parameter(p) for p in tuple(mixer.sends)]})
        self.registry.finish()
        status = self.status()
        # Mutation receipts are diagnostic history, not current audio state.
        status.pop("eq8_last_mutation", None)
        return dict(status, name=str(readable(self.song, "name", "")),
                    file_path=str(readable(self.song, "file_path", "")),
                    transport={"is_playing": bool(self.song.is_playing),
                               "record_mode": bool(readable(self.song, "record_mode", False))},
                    inspected_at=time.time(), tracks=tracks)

    def _refresh(self, session_id):
        if session_id != self.session_id:
            raise BridgeError("stale_session", "Bridge reconnected; inspect the session again")
        self.inspect_session()

    def _writable(self, parameter, expected_value):
        if not readable(parameter, "is_enabled", False):
            raise BridgeError("parameter_disabled", "Live reports that this parameter is disabled")
        # Unknown automation state fails closed; never silently override automation.
        if readable(parameter, "automation_state") != 0:
            raise BridgeError("automation_active", "Parameter is automated or its automation state is unknown")
        before = float(parameter.value)
        if abs(before - number(expected_value)) > 1e-6:
            raise BridgeError("conflict", "Parameter changed since inspection", {"actual_value": before})
        return before

    @staticmethod
    def _eq8_mode(value):
        # Live's integer enum is accepted; lossy bools, floats and objects with
        # only an __int__ conversion are not proof of a three-way mode.
        if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
            raise BridgeError("invalid_argument", "EQ Eight mode must be an integer: 0 Stereo, 1 L/R, 2 M/S")
        return int(value)

    def _eq8_state(self, device):
        state = {"global_mode": None, "edit_mode": None, "oversample": None,
                 "supported": False, "errors": {}}
        for name in ("global_mode", "edit_mode", "oversample"):
            try:
                value = getattr(device, name)
                if name == "global_mode":
                    value = self._eq8_mode(value)
                elif not isinstance(value, (bool, int)) or value not in (0, 1):
                    raise ValueError("Expected a boolean native property")
                else:
                    value = bool(value)
                state[name] = value
            except Exception as exc:  # noqa: BLE001 - a failing host getter must not hide the session.
                # Inspection must remain available on a host without this API.
                state["errors"][name] = type(exc).__name__ + ": " + str(exc)[:240]
        state["supported"] = not state["errors"]
        return state

    def _eq8_target(self, session_id, device_id):
        self._refresh(session_id)
        device = self.registry.get(device_id, "device")
        if readable(device, "class_name") != "Eq8":
            raise BridgeError("invalid_target", "Native mode access is restricted to EQ Eight")
        return device

    def inspect_eq8_state(self, session_id, device_id):
        device = self._eq8_target(session_id, device_id)
        return dict(self._eq8_state(device), device_id=device_id, class_name="Eq8")

    def set_eq8_mode(self, session_id, device_id, expected_mode, mode, dry_run=False):
        expected_mode, mode = self._eq8_mode(expected_mode), self._eq8_mode(mode)
        if type(dry_run) is not bool:
            raise BridgeError("invalid_argument", "dry_run must be a boolean")
        if self.device_browser.uncertain:
            raise BridgeError("insertion_unresolved", "Reconcile the unresolved insertion before changing EQ Eight")
        device = self._eq8_target(session_id, device_id)
        if (readable(self.song, "is_playing") is not False
                or readable(self.song, "record_mode") is not False):
            raise BridgeError("transport_active", "EQ Eight mode changes require stopped Play and Record")
        before = self._eq8_state(device)
        if not before["supported"]:
            raise BridgeError("unsupported", "EQ Eight native state cannot be verified", before)
        if before["global_mode"] != expected_mode:
            raise BridgeError("conflict", "EQ Eight mode changed since inspection", before)
        result = {"device_id": device_id, "before": before, "requested_mode": mode,
                  "dry_run": dry_run, "changed": False, "verified": True}
        if dry_run or mode == expected_mode:
            return result
        outcome = dict(result, state="started", verified=False, changed=None)
        self.eq8_last_mutation = outcome
        error = None
        opened = False
        try:
            self.song.begin_undo_step()
            opened = True
            device.global_mode = mode
        except Exception as exc:  # noqa: BLE001 - record the outcome of every host write failure.
            error = type(exc).__name__ + ": " + str(exc)[:240]
        finally:
            if opened:
                try:
                    self.song.end_undo_step()
                except Exception as exc:  # noqa: BLE001 - a write may have applied before undo cleanup fails.
                    error = error or type(exc).__name__ + ": " + str(exc)[:240]
        after = self._eq8_state(device)
        outcome.update(after=after,
            changed=(after["global_mode"] != expected_mode) if after["global_mode"] is not None else None,
            verified=bool(after["supported"] and after["global_mode"] == mode))
        if error is not None or not outcome["verified"]:
            outcome.update(state="uncertain" if not after["supported"] else "failed",
                           error=error or "Requested mode did not read back")
            raise BridgeError("eq8_mode_unverified", "EQ Eight mode write was not cleanly verified; inspect before another change", dict(outcome))
        outcome["state"] = "complete"
        return dict(outcome)

    def _write(self, parameter, value, before, dry_run):
        plan = {"before": self._parameter(parameter), "requested_value": value,
                "requested_display": display(parameter, value), "dry_run": bool(dry_run)}
        if dry_run:
            return plan
        self.song.begin_undo_step()
        try:
            parameter.value = value
        finally:
            self.song.end_undo_step()
        after = self._parameter(parameter)
        plan.update(after=after, changed=abs(after["value"] - before) > 1e-9,
                    verified=abs(after["value"] - value) <= 1e-5)
        return plan

    def set_parameter(self, session_id, parameter_id, value, expected_value,
                      normalized=False, dry_run=False):
        self._refresh(session_id)
        parameter = self.registry.get(parameter_id, "parameter")
        # Only plug-in/device parameters; mixer access is deliberately separate.
        device_parameters = set()
        for kind, obj in list(self.registry.current.values()):
            if kind == "device":
                device_parameters.update(self.registry.add(p, "parameter") for p in obj.parameters)
        if parameter_id not in device_parameters:
            raise BridgeError("invalid_target", "Use set_fader for track volume")
        before = self._writable(parameter, expected_value)
        value = number(value)
        lower, upper = float(parameter.min), float(parameter.max)
        if normalized:
            if not 0 <= value <= 1:
                raise BridgeError("out_of_range", "Normalized value must be between 0 and 1")
            value = lower + value * (upper - lower)
            if readable(parameter, "is_quantized", False):
                value = math.floor(value + 0.5)
        if not lower <= value <= upper:
            raise BridgeError("out_of_range", "Value is outside Live's parameter range")
        if readable(parameter, "is_quantized", False) and value != int(value):
            raise BridgeError("invalid_argument", "Quantized parameters require an integer value")
        return self._write(parameter, value, before, dry_run)

    def set_fader(self, session_id, track_id, db, expected_value, relative=False, dry_run=False,
                  raw_value=None):
        self._refresh(session_id)
        parameter = self.registry.get(track_id, "track").mixer_device.volume
        before = self._writable(parameter, expected_value)
        if raw_value is not None:
            if db is not None or relative:
                raise BridgeError("invalid_argument", "Choose dB or an exact host value, not both")
            value = number(raw_value)
            if not float(parameter.min) <= value <= float(parameter.max):
                raise BridgeError("out_of_range", "Host fader value is outside Live's range")
            # Exact restoration from a saved inspection must not round-trip
            # through a displayed dB string (which discards precision).
            return self._write(parameter, value, before, dry_run)
        target = number(db)
        if relative:
            current = display_db(parameter)
            if not math.isfinite(current):
                raise BridgeError("invalid_argument", "Cannot apply a relative change to a silent fader")
            target += current
        low, high = float(parameter.min), float(parameter.max)
        maximum = display_db(parameter, high)
        if not -120 <= target <= maximum:
            raise BridgeError("out_of_range", "Requested dB is outside the supported fader range")
        # Fader values are not linear dB. Ask Live's own formatter, rather than
        # guessing that a normalized value or amplitude ratio is the correct law.
        for _ in range(40):
            middle = (low + high) / 2
            if display_db(parameter, middle) < target:
                low = middle
            else:
                high = middle
        value = high
        if abs(display_db(parameter, value) - target) > 0.11:
            raise BridgeError("unsupported", "Live's displayed fader value cannot represent the requested dB")
        result = self._write(parameter, value, before, dry_run)
        result["requested_db"] = target
        if not dry_run:
            actual_db = display_db(parameter)
            result["actual_db"] = actual_db if math.isfinite(actual_db) else None
            result["verified"] = result["verified"] and abs(actual_db - target) <= .11
        return result

    def move_device(self, session_id, device_id, target_id, index,
                    expected_source_order, expected_target_order, dry_run=False):
        self._refresh(session_id)
        mover = readable(self.song, "move_device")
        if not callable(mover):
            raise BridgeError("unsupported", "This Live version does not expose move_device")
        device = self.registry.get(device_id, "device")
        target = self.containers.get(target_id)
        if target is None:
            raise BridgeError("stale_target", "Destination no longer exists")
        source_id, source_index = self.locations[device_id]
        source = self.containers[source_id]
        if (source["device_ids"] != expected_source_order or
                target["device_ids"] != expected_target_order):
            raise BridgeError("conflict", "Device order changed since inspection")
        if device_id in target["ancestors"]:
            raise BridgeError("invalid_target", "Cannot move a rack into its own descendant")
        last = len(target["device_ids"]) - (1 if target_id == source_id else 0)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= last:
            raise BridgeError("out_of_range", "Index must be a valid zero-based final device position")
        before = {"source_id": source_id, "index": source_index,
                  "source_order": list(source["device_ids"]),
                  "target_order": list(target["device_ids"])}
        result = {"before": before, "target_id": target_id, "requested_index": index,
                  "dry_run": bool(dry_run)}
        # Live 11 counts insertion positions in the ORIGINAL target list. When
        # moving forward within the same chain, removing the old occurrence
        # shifts the insertion point left. Our API uses final chain positions.
        host_index = index + (1 if source_id == target_id and source_index < index else 0)
        result["host_insertion_index"] = host_index
        if dry_run:
            return result
        if source_id != target_id or source_index != index:
            self.song.begin_undo_step()
            try:
                mover(device, target["object"], host_index)
            finally:
                self.song.end_undo_step()
        self.inspect_session()
        actual = self.locations.get(device_id)
        result.update(actual_target_id=actual[0] if actual else None,
                      actual_index=actual[1] if actual else None,
                      verified=actual == (target_id, index),
                      changed=actual != (source_id, source_index))
        # Live may choose the nearest valid position. Report actual placement;
        # never claim exact success based solely on the host method returning.
        return result
