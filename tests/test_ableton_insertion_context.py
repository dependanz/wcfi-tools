"""Host-independent checks for read-only insertion context diagnostics."""
import ast
import json
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace

from wcfi_tools.ableton.remote import insertion_context as context


class Mode(IntEnum):
    default = 0
    selected_left = 1
    selected_right = 2


class ReadOnlyView:
    def __init__(self, mode, device=None):
        object.__setattr__(self, "device_insert_mode", mode)
        object.__setattr__(self, "selected_device", device)

    def __setattr__(self, name, value):
        raise AssertionError("Diagnostics attempted a view mutation")


def test_native_enum_evidence_is_json_safe_and_preserves_values():
    result = context.mode_info(ReadOnlyView(Mode.selected_right))
    assert result["first"]["int"] == 2
    assert result["first"] == result["second"]
    assert result["equal"] is True
    assert result["native_values"]["selected_right"]["equals_first"] is True
    assert result["native_values"]["default"]["equals_first"] is False
    assert result["readback_kind"] == "native_enum"
    assert result["exact_mode_available"] is True
    assert result["errors"] == []
    json.dumps(result, allow_nan=False)


def test_boolean_mode_is_explicitly_lossy_without_spurious_enum_errors():
    for mode in (False, True):
        result = context.mode_info(ReadOnlyView(mode))
        assert result["readback_kind"] == "boolean"
        assert result["exact_mode_available"] is False
        assert result["first"]["type"] == "builtins.bool"
        assert result["first"]["int"] == int(mode)
        assert result["equal"] is True
        assert result["native_values"] == {}
        assert result["errors"] == []


def test_plain_numeric_modes_keep_exact_readback_without_enum_lookup():
    for mode in (0, 1, 2):
        result = context.mode_info(ReadOnlyView(mode))
        assert result["readback_kind"] == "integer"
        assert result["exact_mode_available"] is True
        assert result["first"]["int"] == mode
        assert result["native_values"] == {}
        assert result["errors"] == []
    assert context.mode_info(ReadOnlyView(9))["exact_mode_available"] is False


def test_mixed_boolean_and_integer_readbacks_are_not_exact():
    class MixedView:
        def __init__(self):
            self.values = iter((True, 1))

        @property
        def device_insert_mode(self):
            return next(self.values)

    result = context.mode_info(MixedView())
    assert result["equal"] is True
    assert result["readback_kind"] == "mixed"
    assert result["exact_mode_available"] is False


def test_repeated_reads_reveal_changing_getter():
    class ChangingView:
        def __init__(self):
            self.values = iter((Mode.default, Mode.selected_right))

        @property
        def device_insert_mode(self):
            return next(self.values)

    result = context.mode_info(ChangingView())
    assert result["first"]["int"] == 0
    assert result["second"]["int"] == 2
    assert result["equal"] is False
    assert result["exact_mode_available"] is False


def test_same_integer_but_native_inequality_is_not_hidden():
    class UnequalInt(int):
        def __eq__(self, other):
            return False

    result = context.mode_info(ReadOnlyView(UnequalInt(2)))
    assert result["first"]["int"] == result["second"]["int"] == 2
    assert result["equal"] is False
    assert result["exact_mode_available"] is False


def test_failed_getters_are_explicit_bounded_and_json_safe():
    class FailedView:
        @property
        def device_insert_mode(self):
            raise RuntimeError("x" * 2000)

    result = context.mode_info(FailedView())
    assert result["first"] is None and result["second"] is None
    assert result["equal"] is None
    assert len(result["errors"]) == 2
    assert all(len(error) <= context.MAX_TEXT for error in result["errors"])
    json.dumps(result)


def test_repr_int_and_equality_errors_remain_diagnostic_data():
    class BrokenValue:
        def __repr__(self):
            raise RuntimeError("repr failed")

        def __int__(self):
            raise RuntimeError("int failed")

        def __eq__(self, other):
            raise RuntimeError("equality failed")

    result = context.mode_info(ReadOnlyView(BrokenValue()))
    assert result["first"]["repr"] is None
    assert result["first"]["int"] is None
    assert len(result["first"]["errors"]) == 2
    assert result["equal"] is None
    assert any("equality failed" in error for error in result["errors"])
    json.dumps(result)


def make_adapter():
    old_device, new_device, chain = object(), object(), object()
    original = SimpleNamespace(name="Original", view=ReadOnlyView(Mode.default, old_device))
    target = SimpleNamespace(name="Target", view=ReadOnlyView(Mode.selected_right, new_device))
    registry = SimpleNamespace(current={
        "track_original": ("track", original), "track_target": ("track", target),
        "device_original": ("device", old_device), "device_target": ("device", new_device),
        "chain_known": ("chain", chain),
    })
    adapter = SimpleNamespace(registry=registry, song=SimpleNamespace(view=SimpleNamespace(
        selected_track=original, selected_chain=chain)))
    return adapter, target, original


def test_snapshot_resolves_existing_ids_and_does_not_change_registry_or_views():
    adapter, target, original = make_adapter()
    before = list(adapter.registry.current.items())
    result = context.context_snapshot(adapter, target, original)
    assert result["selected_track_id"] == "track_original"
    assert result["selected_track_name"] == "Original"
    assert result["selected_device_id"] == "device_original"
    assert result["selected_chain_id"] == "chain_known"
    assert result["target"]["mode"]["first"]["int"] == 2
    assert result["original_track"]["mode"]["first"]["int"] == 0
    assert adapter.song.view.selected_track is original
    assert list(adapter.registry.current.items()) == before
    assert result["errors"] == []
    json.dumps(result, allow_nan=False)


def test_unknown_objects_keep_null_ids_without_registration():
    adapter, target, _ = make_adapter()
    adapter.registry.current.clear()
    result = context.context_snapshot(adapter, target)
    assert result["selected_track_id"] is None
    assert result["selected_device_id"] is None
    assert result["selected_chain_id"] is None
    assert result["target"]["id"] is None
    assert result["original_track"]["name"] == "Original"
    assert adapter.registry.current == {}


def test_registry_equality_failure_does_not_prevent_later_identity_match():
    class BadEquality:
        def __eq__(self, other):
            raise RuntimeError("deleted handle")

    adapter, target, _ = make_adapter()
    adapter.registry.current = {
        "track_deleted": ("track", BadEquality()), **adapter.registry.current,
    }
    result = context.context_snapshot(adapter, target)
    assert result["target"]["id"] == "track_target"
    assert any("deleted handle" in error for error in result["target"]["errors"])


def test_registry_scan_is_bounded_and_reports_limit(monkeypatch):
    adapter, target, _ = make_adapter()
    monkeypatch.setattr(context, "MAX_REGISTRY_SCAN", 1)
    result = context.context_snapshot(adapter, target)
    assert result["target"]["id"] is None
    assert any("scan limit" in error for error in result["target"]["errors"])


def test_missing_selection_getters_produce_a_partial_snapshot():
    result = context.context_snapshot(SimpleNamespace(), None)
    assert result["selected_track_id"] is None
    assert result["target"] is None
    assert result["errors"]
    assert len(result["errors"]) <= context.MAX_ERRORS
    json.dumps(result)


def test_helper_is_standalone_python_37_syntax():
    path = Path(context.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 7))
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
