"""Parser for Facebook's Download Your Information JSON message export."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..private_workspace import contained_path

LOCAL_ATTACHMENT_FIELDS = {
    "photos": "photo",
    "videos": "video",
    "audio_files": "audio",
    "files": "file",
    "gifs": "gif",
    "sticker": "sticker",
}
MOJIBAKE_MARKERS = ("Ã", "Â", "â", "ð", "ï¿½")


def decode_meta_text(value: Any) -> Any:
    """Repair the common UTF-8-as-Latin-1 encoding in Meta JSON exports."""
    if not isinstance(value, str) or not any(marker in value for marker in MOJIBAKE_MARKERS):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    old_score = sum(value.count(marker) for marker in MOJIBAKE_MARKERS)
    new_score = sum(repaired.count(marker) for marker in MOJIBAKE_MARKERS)
    return repaired if new_score < old_score else value


def message_files(export_root: Path) -> list[Path]:
    """Find Messenger message_N.json files without assuming one export layout."""
    root = export_root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Facebook export folder does not exist: {root}")
    return sorted(path for path in root.rglob("message_*.json") if path.is_file())


def _attachment_records(raw: dict[str, Any], export_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for field, kind in LOCAL_ATTACHMENT_FIELDS.items():
        items = raw.get(field, [])
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            uri = decode_meta_text(item.get("uri"))
            record: dict[str, Any] = {"kind": kind, "uri": uri}
            if isinstance(uri, str) and uri:
                path, contained = contained_path(export_root, uri)
                record.update(
                    {
                        "path": str(path),
                        "contained": contained,
                        "exists": contained and path.is_file(),
                    }
                )
            else:
                record.update({"path": None, "contained": False, "exists": False})
            records.append(record)
    share = raw.get("share")
    if isinstance(share, dict):
        link = decode_meta_text(share.get("link"))
        if link:
            records.append(
                {
                    "kind": "shared_link",
                    "uri": link,
                    "path": None,
                    "contained": False,
                    "exists": False,
                }
            )
    return records


def _message_id(source: str, raw: dict[str, Any]) -> str:
    stable = "|".join(
        (
            source,
            str(raw.get("timestamp_ms", "")),
            str(raw.get("sender_name", "")),
            str(raw.get("content", "")),
        )
    )
    return hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()[:20]


def iter_messages(export_root: Path) -> Iterator[dict[str, Any]]:
    """Yield normalized message records while leaving the export untouched."""
    root = export_root.expanduser().resolve()
    for source_path in message_files(root):
        relative_source = source_path.relative_to(root).as_posix()
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {relative_source}: {exc}") from None
        if not isinstance(payload, dict):
            raise TypeError(f"Expected a JSON object in {relative_source}")
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            raise TypeError(f"Expected a messages list in {relative_source}")
        conversation_id = source_path.parent.relative_to(root).as_posix()
        title = decode_meta_text(payload.get("title", source_path.parent.name))
        participants = [
            decode_meta_text(item.get("name"))
            for item in payload.get("participants", [])
            if isinstance(item, dict) and item.get("name")
        ]
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            timestamp_ms = raw.get("timestamp_ms")
            sent_at = None
            if isinstance(timestamp_ms, (int, float)):
                try:
                    sent_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
                except (OverflowError, OSError, ValueError):
                    pass
            record = {
                "schema": "wcfi.fbmsg.message.v1",
                "message_id": _message_id(relative_source, raw),
                "conversation_id": conversation_id,
                "conversation_title": title,
                "participants": participants,
                "source_file": relative_source,
                "timestamp_ms": timestamp_ms,
                "sent_at": sent_at,
                "sender": decode_meta_text(raw.get("sender_name")),
                "content": decode_meta_text(raw.get("content")),
                "attachments": _attachment_records(raw, root),
            }
            yield record
