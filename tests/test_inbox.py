"""Offline tests for the semantic inbox: store semantics, API guards, CLI wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.inbox.paths import prepare_data_dir
from wcfi_tools.inbox.server import create_app
from wcfi_tools.inbox.store import InboxError, InboxStore
from wcfi_tools.private_workspace import UnsafeOutputError

TOKEN = "test-token"
AUTH = {"X-Inbox-Token": TOKEN}


def _item(**overrides: object) -> dict:
    item = {"source": "gmail", "external_id": "t1", "title": "Reply to Edwin", "kind": "needs-you"}
    item.update(overrides)
    return item


@pytest.fixture
def store(tmp_path: Path):
    s = InboxStore(tmp_path / "inbox.sqlite3")
    yield s
    s.close()


@pytest.fixture
def api(store: InboxStore) -> TestClient:
    return TestClient(create_app(store, TOKEN, allowed_hosts={"testserver"}))


def test_repush_refreshes_content_but_keeps_the_human_decision(store: InboxStore) -> None:
    iid = store.upsert([_item()])["added"][0]
    store.decide(iid, status="settled", note="told him Sunday")
    assert store.upsert([_item(title="Reply to Pastor Edwin")])["updated"] == [iid]
    item = store.get(iid)
    assert item["title"] == "Reply to Pastor Edwin"
    assert (item["status"], item["note"]) == ("settled", "told him Sunday")
    assert store.upsert([_item(title="Reply to Pastor Edwin")])["unchanged"] == [iid]


def test_a_bad_item_rejects_the_whole_batch(store: InboxStore) -> None:
    with pytest.raises(InboxError):
        store.upsert([_item(external_id="ok"), _item(external_id="bad", kind="urgent")])
    assert store.list_items() == []


def test_api_needs_the_token_and_json(api: TestClient) -> None:
    assert api.get("/health").json()["app"] == "wcfi-inbox"
    assert api.get("/api/items").status_code == 401
    wrong_type = {**AUTH, "Content-Type": "text/plain"}
    assert api.post("/api/items", content="{}", headers=wrong_type).status_code == 415


def test_push_list_decide_round_trip(api: TestClient) -> None:
    pushed = api.post(
        "/api/items",
        json={"items": [_item(), _item(external_id="t2", kind="fyi", title="Choir Sept 20")]},
        headers=AUTH,
    )
    assert pushed.status_code == 200 and len(pushed.json()["added"]) == 2
    items = api.get("/api/items", headers=AUTH).json()["items"]
    target = next(i for i in items if i["external_id"] == "t1")
    decided = api.patch(
        f"/api/items/{target['id']}", json={"status": "relevant", "note": "draft"}, headers=AUTH
    )
    assert decided.json()["status"] == "relevant"
    still_open = api.get("/api/items", params={"status": "open"}, headers=AUTH).json()["items"]
    assert [i["external_id"] for i in still_open] == ["t2"]
    bad = api.patch(f"/api/items/{target['id']}", json={"status": "done"}, headers=AUTH)
    assert bad.status_code == 422


def test_a_foreign_host_header_is_refused(store: InboxStore) -> None:
    guarded = TestClient(create_app(store, TOKEN, allowed_hosts={"127.0.0.1:8787"}))
    assert guarded.get("/health", headers={"Host": "evil.example"}).status_code == 403


def test_memory_and_cursors_persist(api: TestClient) -> None:
    api.post("/api/memory", json={"text": "Not in the anniversary band", "scope": "wcfi"},
             headers=AUTH)
    api.put("/api/sources/messenger", json={"cursor": "2026-09-09T14:32", "swept": True},
            headers=AUTH)
    assert api.get("/api/memory", headers=AUTH).json()["memory"][0]["scope"] == "wcfi"
    source = api.get("/api/sources", headers=AUTH).json()["sources"][0]
    assert source["cursor"] == "2026-09-09T14:32" and source["last_sweep_at"]


def test_inbox_data_cannot_live_inside_git(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(UnsafeOutputError):
        prepare_data_dir(repository / "inbox")


def test_inbox_command_is_visible() -> None:
    result = CliRunner().invoke(app, ["inbox", "--help"])
    assert result.exit_code == 0
    assert "push" in result.output
