"""Offline tests for the Wix and Facebook archive tooling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.fbmsg import build_index, decode_meta_text, scan_export
from wcfi_tools.private_workspace import (
    UnsafeOutputError,
    initialize_private_directory,
    require_private_output,
)
from wcfi_tools.wix import WixCredentials, WixMediaClient

runner = CliRunner()


def test_archive_commands_are_visible() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "wix" in result.output
    assert "fbmsg" in result.output


def test_private_outputs_cannot_enter_git(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(UnsafeOutputError):
        require_private_output(repository / "private" / "index")


def test_private_initializer_never_overwrites_an_existing_guard(tmp_path: Path) -> None:
    output = tmp_path / "private"
    output.mkdir()
    custom = output / ".gitignore"
    custom.write_text("*.tmp\n", encoding="utf-8")
    with pytest.raises(UnsafeOutputError, match="not overwritten"):
        initialize_private_directory(output)
    assert custom.read_text(encoding="utf-8") == "*.tmp\n"


def test_wix_client_paginates_read_only_and_never_exposes_token() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []
    pages = [
        {"files": [{"id": "one"}, {"id": "two"}]},
        {"files": [{"id": "three"}]},
    ]

    def transport(url: str, headers: dict[str, str], timeout: float) -> dict:
        calls.append((url, headers, timeout))
        return pages[len(calls) - 1]

    credentials = WixCredentials(access_token="top-secret", site_id="site-123")
    files = list(WixMediaClient(credentials, transport=transport).iter_files(page_size=2))
    assert [item["id"] for item in files] == ["one", "two", "three"]
    assert "paging.offset=0" in calls[0][0]
    assert "paging.offset=2" in calls[1][0]
    assert calls[0][1]["Authorization"] == "top-secret"
    assert calls[0][1]["wix-site-id"] == "site-123"
    assert "top-secret" not in calls[0][0]


def _facebook_export(root: Path) -> Path:
    conversation = root / "messages" / "inbox" / "wcfi_anniversary"
    conversation.mkdir(parents=True)
    attachment = conversation / "photo.jpg"
    attachment.write_bytes(b"not-an-image-but-present")
    payload = {
        "title": "WCFI Anniversary",
        "participants": [{"name": "Jos\u00c3\u00a9"}],
        "messages": [
            {
                "sender_name": "Jos\u00c3\u00a9",
                "timestamp_ms": 1_700_000_000_000,
                "content": "Please send the anniversary photos by Friday.",
                "photos": [
                    {
                        "uri": "messages/inbox/wcfi_anniversary/photo.jpg",
                        "creation_timestamp": 1_700_000_000,
                    }
                ],
            },
            {
                "sender_name": "Someone",
                "timestamp_ms": 1_700_000_100_000,
                "content": "This is just a general observation.",
            },
        ],
    }
    (conversation / "message_1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return root


def test_fbmsg_scan_and_private_index(tmp_path: Path) -> None:
    export = _facebook_export(tmp_path / "facebook-export")
    summary = scan_export(export)
    assert summary["conversations"] == 1
    assert summary["messages"] == 2
    assert summary["attachments"] == 1
    assert summary["missing_local_attachments"] == 0

    output = tmp_path / "private-index"
    inventory = build_index(export, output)
    assert inventory["task_candidates"] == 1
    assert (output / ".wcfi-private").is_file()
    assert (output / ".gitignore").read_text(encoding="utf-8").startswith("*")
    assert (output / "messages.jsonl").is_file()
    assert (output / "attachments.jsonl").is_file()
    tasks = (output / "task-candidates.md").read_text(encoding="utf-8")
    assert "Needs review" in tasks
    assert "Please send the anniversary photos by Friday." in tasks


def test_fbmsg_term_filter_and_meta_text_repair(tmp_path: Path) -> None:
    export = _facebook_export(tmp_path / "facebook-export")
    output = tmp_path / "private-index"
    inventory = build_index(export, output, terms=["unrelated"])
    assert inventory["task_candidates"] == 0
    assert decode_meta_text("Jos\u00c3\u00a9") == "Jos\u00e9"
