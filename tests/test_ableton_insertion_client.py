"""Client/CLI boundaries for exact browser selection and guarded insertion rollback."""
import json
from copy import deepcopy

import pytest
from typer.testing import CliRunner

from wcfi_tools.ableton import client
from wcfi_tools.cli import app


class InsertionRPC:
    """No host, desktop, filesystem or network writes are available in this fake."""

    def __init__(self):
        self.calls = []
        self.status = {"session_id": "session-current", "bridge_revision": 6,
                       "operations": ["status", "inspect_session", "list_browser_items",
                                      "insert_device", "remove_inserted_device",
                                      "inspect_insertion_context", "probe_insertion_context"]}
        self.snapshot = {"session_id": "session-current", "tracks": [
            {"id": "track-red", "name": "RED", "kind": "track", "devices": [
                {"id": "device-before", "name": "Before", "chains": []},
                {"id": "device-rack", "name": "Rack", "chains": [
                    {"id": "chain-nested", "name": "Nested", "devices": []}]},
                {"id": "device-after", "name": "After", "chains": []}]},
            {"id": "track-kit", "name": "KIT", "kind": "track", "devices": []},
            {"id": "track-master", "name": "Master", "kind": "master", "devices": []}]}
        self.result = {"verified": True, "changed": True, "dry_run": False,
                       "insertion_id": "insertion-owned", "device_id": "device-created",
                       "actual_index": 1, "selection_restored": True}
        self.error = None

    def call(self, operation, arguments=None):
        self.calls.append((operation, deepcopy(arguments)))
        if operation == "status":
            return deepcopy(self.status)
        if operation == "inspect_session":
            return deepcopy(self.snapshot)
        if self.error:
            raise self.error
        if operation == "list_browser_items":
            return {"session_id": "session-current", "root": "audio_effects",
                    "parent_id": arguments["parent_id"], "items": [], "total": 0,
                    "next_offset": None}
        if operation == "inspect_insertion_context":
            return {"session_id": "session-current", "target_id": arguments["target_id"],
                    "selected_track_id": "track-master", "target_insert_mode": 0}
        assert operation in ("insert_device", "remove_inserted_device", "probe_insertion_context")
        return dict(self.result, dry_run=arguments["dry_run"])


@pytest.fixture
def rpc(monkeypatch):
    fake = InsertionRPC()
    monkeypatch.setattr(client, "connect", lambda **_kwargs: fake)
    return fake


def test_browse_uses_current_session_and_exact_opaque_parent(rpc):
    parent = "browser-folder_opaque-not-a-name"
    result = client.browse_devices(rpc, parent=parent, offset=100, limit=25)
    assert result["parent_id"] == parent
    assert rpc.calls == [("status", None), ("list_browser_items", {
        "session_id": "session-current", "root": "audio_effects", "parent_id": parent,
        "offset": 100, "limit": 25})]


def test_insert_uses_inspected_order_and_never_resolves_item_as_a_name(rpc):
    result = client.insert_device(rpc, "red", "browser-item-opaque", 1, dry_run=True)
    assert result["dry_run"]
    assert rpc.calls[-1] == ("insert_device", {"session_id": "session-current",
        "item_id": "browser-item-opaque", "target_id": "track-red", "index": 1,
        "expected_target_order": ["device-before", "device-rack", "device-after"],
        "dry_run": True})
    assert [name for name, _args in rpc.calls] == ["status", "inspect_session", "insert_device"]


def test_duplicate_track_names_fail_before_write_but_exact_id_works(rpc):
    rpc.snapshot["tracks"][1]["name"] = "RED"
    with pytest.raises(client.AbletonError, match="matched 2"):
        client.insert_device(rpc, "RED", "browser-item", 0)
    assert all(operation != "insert_device" for operation, _args in rpc.calls)
    client.insert_device(rpc, "track-kit", "browser-item", 0)
    assert rpc.calls[-1][1]["target_id"] == "track-kit"
    assert rpc.calls[-1][1]["expected_target_order"] == []


def test_nested_chain_selector_cannot_be_used_for_insertion(rpc):
    with pytest.raises(client.AbletonError, match="matched 0"):
        client.insert_device(rpc, "chain-nested", "browser-item", 0)
    assert all(operation != "insert_device" for operation, _args in rpc.calls)


@pytest.mark.parametrize("invoke", [
    lambda rpc: client.browse_devices(rpc),
    lambda rpc: client.insert_device(rpc, "Master", "browser-item", 0),
    lambda rpc: client.remove_inserted_device(rpc, "insertion-owned"),
    lambda rpc: client.inspect_insertion_context(rpc, "Master"),
    lambda rpc: client.probe_insertion_context(rpc, "Master"),
])
def test_old_bridge_explains_reload_without_attempting_unknown_mutation(rpc, invoke):
    rpc.status.update(bridge_revision=2, operations=["status", "inspect_session"])
    with pytest.raises(client.AbletonError) as exc:
        invoke(rpc)
    assert exc.value.code == "bridge_update_required"
    assert "save your set and restart Live" in str(exc.value)
    assert "bridge_revision 6 or later" in str(exc.value)
    assert rpc.calls == [("status", None)]


@pytest.mark.parametrize("revision", [3, 4, 5])
@pytest.mark.parametrize("invoke", [
    lambda rpc: client.insert_device(rpc, "Master", "browser-item", 0),
    lambda rpc: client.probe_insertion_context(rpc, "Master"),
])
def test_older_mutations_require_diagnostics_revision_even_with_operation(rpc, revision, invoke):
    rpc.status["bridge_revision"] = revision
    with pytest.raises(client.AbletonError) as exc:
        invoke(rpc)
    assert exc.value.code == "bridge_update_required"
    assert rpc.calls == [("status", None)]


def test_older_revision_retains_discovery_and_receipt_cleanup(rpc):
    rpc.status["bridge_revision"] = 3
    client.browse_devices(rpc)
    client.remove_inserted_device(rpc, "insertion-owned", dry_run=True)
    client.inspect_insertion_context(rpc, "Master")
    assert [operation for operation, _args in rpc.calls] == [
        "status", "list_browser_items", "status", "remove_inserted_device",
        "status", "inspect_session", "inspect_insertion_context"]


def test_remove_only_sends_receipt_and_session_not_arbitrary_device_selector(rpc):
    client.remove_inserted_device(rpc, "insertion-owned", dry_run=True)
    assert rpc.calls[-1] == ("remove_inserted_device", {"session_id": "session-current",
        "insertion_id": "insertion-owned", "dry_run": True})


def test_context_probe_guards_fresh_target_order_without_sending_load_request(rpc):
    client.probe_insertion_context(rpc, "RED", dry_run=True)
    assert rpc.calls == [("status", None), ("inspect_session", None),
        ("probe_insertion_context", {"session_id": "session-current", "target_id": "track-red",
         "expected_target_order": ["device-before", "device-rack", "device-after"],
         "dry_run": True})]


def test_context_read_uses_only_session_and_exact_target(rpc):
    client.inspect_insertion_context(rpc, "master")
    assert rpc.calls[-1] == ("inspect_insertion_context", {
        "session_id": "session-current", "target_id": "track-master"})


@pytest.mark.parametrize("arguments,operation,expected", [
    (["browse-devices", "--parent", "browser-folder", "--offset", "25", "--limit", "25"],
     "list_browser_items", {"parent_id": "browser-folder", "offset": 25, "limit": 25}),
    (["insert-device", "Master", "browser-item", "--index", "0", "--dry-run"],
     "insert_device", {"target_id": "track-master", "item_id": "browser-item", "index": 0,
                       "dry_run": True, "expected_target_order": []}),
    (["remove-inserted-device", "insertion-owned", "--dry-run"],
     "remove_inserted_device", {"insertion_id": "insertion-owned", "dry_run": True}),
    (["insertion-context", "Master"],
     "inspect_insertion_context", {"target_id": "track-master"}),
    (["check-insertion-context", "Master", "--dry-run"],
     "probe_insertion_context", {"target_id": "track-master", "expected_target_order": [],
                                 "dry_run": True}),
])
def test_cli_forwards_explicit_arguments(rpc, arguments, operation, expected):
    result = CliRunner().invoke(app, ["audio", "ableton", *arguments])
    assert result.exit_code == 0, result.output
    assert rpc.calls[-1][0] == operation
    assert expected.items() <= rpc.calls[-1][1].items()
    json.loads(result.stdout)


@pytest.mark.parametrize("arguments", [
    ["insert-device", "Master", "browser-item", "--index", "0"],
    ["remove-inserted-device", "insertion-owned"],
    ["check-insertion-context", "Master"],
])
def test_cli_unverified_readback_is_exit_two_and_is_not_retried(rpc, arguments):
    rpc.result.update(verified=False, selection_restored=False)
    result = CliRunner().invoke(app, ["audio", "ableton", *arguments])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["verified"] is False
    assert sum(operation in ("insert_device", "remove_inserted_device", "probe_insertion_context")
               for operation, _args in rpc.calls) == 1


def test_cli_uncertain_insertion_preserves_error_and_never_retries(rpc):
    rpc.error = client.AbletonError("Load outcome is unknown; inspect before another write",
                                   code="outcome_unknown", details={"request_id": "abc"})
    result = CliRunner().invoke(app, ["audio", "ableton", "insert-device", "Master",
                                     "browser-item", "--index", "0"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["details"] == {"request_id": "abc"}
    assert sum(operation == "insert_device" for operation, _args in rpc.calls) == 1


def test_existing_move_readback_also_keeps_exit_two(rpc, monkeypatch):
    monkeypatch.setattr(client, "move_device", lambda *_args, **_kwargs: {
        "verified": False, "dry_run": False, "actual_index": 0})
    result = CliRunner().invoke(app, ["audio", "ableton", "move-device", "RED", "device-rack",
                                     "--index", "1"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["verified"] is False


@pytest.mark.parametrize("arguments", [
    ["browse-devices", "--offset", "-1"],
    ["browse-devices", "--limit", "0"],
    ["browse-devices", "--limit", "101"],
    ["insert-device", "Master", "browser-item", "--index", "-1"],
])
def test_cli_rejects_invalid_pagination_and_index_before_connecting(rpc, arguments):
    result = CliRunner().invoke(app, ["audio", "ableton", *arguments])
    assert result.exit_code == 2
    assert not rpc.calls
