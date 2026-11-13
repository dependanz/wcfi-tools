"""Read-only, bounded diagnostics for Live's insertion view context (Python 3.7).

These helpers perform getters and comparisons only. They never refresh or add to
the bridge registry, select an object, set a mode, or load a browser item.
"""

MAX_TEXT = 240
MAX_ERRORS = 8
MAX_REGISTRY_SCAN = 16384
_MISSING = object()


def _text(value):
    try:
        return str(value)[:MAX_TEXT]
    except Exception:  # noqa: BLE001 - diagnostics must survive broken host text.
        return "<unprintable>"


def _error(exc):
    return (type(exc).__name__ + ": " + _text(exc))[:MAX_TEXT]


def _note(errors, label, exc):
    if len(errors) < MAX_ERRORS:
        errors.append((label + ": " + _error(exc))[:MAX_TEXT])


def _get(obj, name, errors, label):
    try:
        return getattr(obj, name)
    except Exception as exc:  # noqa: BLE001 - invalid Live handles can raise.
        _note(errors, label, exc)
        return _MISSING


def _value_info(value):
    errors = []
    result = {"type": _text(type(value).__module__ + "." + type(value).__name__),
              "repr": None, "int": None}
    try:
        result["repr"] = repr(value)[:MAX_TEXT]
    except Exception as exc:  # noqa: BLE001 - keep conversion failures inspectable.
        _note(errors, "repr", exc)
    try:
        number = int(value)
        if not -(2 ** 63) <= number < 2 ** 63:
            raise ValueError("Integer is outside the bounded diagnostic range")
        result["int"] = number
    except Exception as exc:  # noqa: BLE001 - a non-enum value is diagnostic data.
        _note(errors, "int", exc)
    if errors:
        result["errors"] = errors
    return result


def _equal(first, second, errors, label):
    if first is _MISSING or second is _MISSING:
        return None
    try:
        return bool(first == second)
    except Exception as exc:  # noqa: BLE001 - equality itself may be a host call.
        _note(errors, label, exc)
        return None


def mode_info(view):
    """Read the mode twice, retaining native and integer comparison evidence."""
    errors = []
    first = _get(view, "device_insert_mode", errors, "first getter")
    second = _get(view, "device_insert_mode", errors, "second getter")
    result = {
        "first": None if first is _MISSING else _value_info(first),
        "second": None if second is _MISSING else _value_info(second),
        "equal": _equal(first, second, errors, "repeated getter equality"),
        "native_values": {},
        "readback_kind": "unavailable",
        "exact_mode_available": False,
    }
    available = first if first is not _MISSING else second
    if available is not _MISSING:
        boolean = type(available) is bool
        result["readback_kind"] = (
            "boolean" if boolean else "integer" if isinstance(available, int) else "other")
        # A bool cannot distinguish all three insertion modes. It is expected
        # on the observed Live 11 host, not three missing-enum-attribute errors.
        # A plain integer also has no native enum attributes to interrogate.
        if not boolean and type(available) is not int:
            for name in ("default", "selected_left", "selected_right"):
                native = _get(type(available), name, errors, "native enum " + name)
                if native is not _MISSING:
                    data = _value_info(native)
                    data["equals_first"] = _equal(first, native, errors, name + " first equality")
                    data["equals_second"] = _equal(second, native, errors, name + " second equality")
                    result["native_values"][name] = data
            if len(result["native_values"]) == 3:
                result["readback_kind"] = "native_enum"
        if first is not _MISSING and second is not _MISSING:
            if type(first) is not type(second):
                result["readback_kind"] = "mixed"
            result["exact_mode_available"] = (
                type(first) is type(second) and type(first) is not bool
                and isinstance(first, int) and isinstance(second, int)
                and result["equal"] is True
                and result["first"]["int"] in (0, 1, 2)
                and result["second"]["int"] == result["first"]["int"])
    result["errors"] = errors
    return result


def _identifier(adapter, obj, kind, errors, label):
    """Resolve existing IDs only; equality errors cannot mutate the registry."""
    if obj is None or obj is _MISSING:
        return None
    try:
        entries = adapter.registry.current.items()
        for index, (identifier, entry) in enumerate(entries):
            if index >= MAX_REGISTRY_SCAN:
                raise ValueError("Registry diagnostic scan limit reached")
            entry_kind, candidate = entry
            if entry_kind != kind:
                continue
            try:
                if candidate is obj or bool(candidate == obj):
                    return _text(identifier)
            except Exception as exc:  # noqa: BLE001 - skip invalid stale handles.
                _note(errors, label + " equality", exc)
    except Exception as exc:  # noqa: BLE001 - the snapshot remains useful in part.
        _note(errors, label + " registry", exc)
    return None


def _track_info(adapter, track):
    if track is None or track is _MISSING:
        return None
    errors = []
    identifier = _identifier(adapter, track, "track", errors, "track")
    name = _get(track, "name", errors, "track name")
    view = _get(track, "view", errors, "track view")
    device = _get(view, "selected_device", errors, "selected device")
    return {
        "id": identifier,
        "name": None if name is _MISSING else _text(name),
        "selected_device_id": _identifier(adapter, device, "device", errors, "device"),
        "mode": mode_info(view),
        "errors": errors,
    }


def context_snapshot(adapter, target, original_track=None):
    """Inspect current selection and two track contexts without registering IDs.

    ``original_track`` is the saved pre-operation selection when tracing cleanup;
    omitting it inspects the currently selected track. Unknown IDs remain null.
    """
    errors = []
    song = _get(adapter, "song", errors, "song")
    view = _get(song, "view", errors, "song view")
    selected = _get(view, "selected_track", errors, "selected track")
    selected_view = _get(selected, "view", errors, "selected track view")
    selected_device = _get(selected_view, "selected_device", errors, "selected device")
    selected_chain = _get(view, "selected_chain", errors, "selected chain")
    selected_name = _get(selected, "name", errors, "selected track name")
    if original_track is None:
        original_track = selected
    return {
        "selected_track_id": _identifier(adapter, selected, "track", errors, "selected track"),
        "selected_track_name": None if selected_name is _MISSING else _text(selected_name),
        "selected_device_id": _identifier(
            adapter, selected_device, "device", errors, "selected device"),
        "selected_chain_id": _identifier(
            adapter, selected_chain, "chain", errors, "selected chain"),
        "target": _track_info(adapter, target),
        "original_track": _track_info(adapter, original_track),
        "errors": errors,
    }
