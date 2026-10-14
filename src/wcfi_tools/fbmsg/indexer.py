"""Build guarded local indexes from a Facebook Messenger JSON export."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..private_workspace import initialize_private_directory
from .parser import iter_messages, message_files
from .tasks import task_candidate, task_markdown

OUTPUT_NAMES = (
    "inventory.json",
    "messages.jsonl",
    "attachments.jsonl",
    "task-candidates.jsonl",
    "task-candidates.md",
)


class FbmsgExportError(ValueError):
    """A malformed, missing, or unsafe Facebook export."""


def _write_json_atomic(path: Path, value: Any) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def scan_export(export_root: Path) -> dict[str, Any]:
    """Return aggregate counts without displaying message content or names."""
    root = export_root.expanduser().resolve()
    files = message_files(root)
    if not files:
        raise FbmsgExportError(f"No message_*.json files found under {root}")
    conversations: set[str] = set()
    message_count = 0
    attachment_count = 0
    missing_attachments = 0
    kinds: Counter[str] = Counter()
    for message in iter_messages(root):
        message_count += 1
        conversations.add(str(message["conversation_id"]))
        for attachment in message["attachments"]:
            attachment_count += 1
            kinds[str(attachment["kind"])] += 1
            if attachment.get("path") and not attachment.get("exists"):
                missing_attachments += 1
    return {
        "message_files": len(files),
        "conversations": len(conversations),
        "messages": message_count,
        "attachments": attachment_count,
        "missing_local_attachments": missing_attachments,
        "attachment_kinds": dict(sorted(kinds.items())),
    }


def build_index(
    export_root: Path,
    output_dir: Path,
    *,
    force: bool = False,
    terms: list[str] | None = None,
) -> dict[str, Any]:
    """Create local private indexes; never copies, sends, edits, or deletes messages."""
    root = export_root.expanduser().resolve()
    files = message_files(root)
    if not files:
        raise FbmsgExportError(f"No message_*.json files found under {root}")
    private_dir = initialize_private_directory(output_dir)
    existing = [private_dir / name for name in OUTPUT_NAMES if (private_dir / name).exists()]
    if existing and not force:
        raise FileExistsError(
            f"Index already exists in {private_dir}. Use --force to replace known index files."
        )

    messages_path = private_dir / "messages.jsonl"
    attachments_path = private_dir / "attachments.jsonl"
    tasks_path = private_dir / "task-candidates.jsonl"
    temporary = {
        messages_path: messages_path.with_name(".messages.jsonl.tmp"),
        attachments_path: attachments_path.with_name(".attachments.jsonl.tmp"),
        tasks_path: tasks_path.with_name(".task-candidates.jsonl.tmp"),
    }
    conversations: set[str] = set()
    candidates: list[dict[str, Any]] = []
    message_count = 0
    attachment_count = 0
    missing_attachments = 0
    kinds: Counter[str] = Counter()
    try:
        with (
            temporary[messages_path].open("w", encoding="utf-8", newline="\n") as messages_out,
            temporary[attachments_path].open("w", encoding="utf-8", newline="\n") as attachments_out,
            temporary[tasks_path].open("w", encoding="utf-8", newline="\n") as tasks_out,
        ):
            for message in iter_messages(root):
                message_count += 1
                conversations.add(str(message["conversation_id"]))
                messages_out.write(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")
                for attachment in message["attachments"]:
                    attachment_record = {
                        "schema": "wcfi.fbmsg.attachment.v1",
                        "message_id": message["message_id"],
                        "conversation_id": message["conversation_id"],
                        "sent_at": message["sent_at"],
                        **attachment,
                    }
                    attachments_out.write(
                        json.dumps(attachment_record, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                    attachment_count += 1
                    kinds[str(attachment["kind"])] += 1
                    if attachment.get("path") and not attachment.get("exists"):
                        missing_attachments += 1
                candidate = task_candidate(message, terms=terms)
                if candidate is not None:
                    candidates.append(candidate)
                    tasks_out.write(
                        json.dumps(candidate, ensure_ascii=False, sort_keys=True) + "\n"
                    )
        for final, temp in temporary.items():
            os.replace(temp, final)
    except Exception:
        for temp in temporary.values():
            temp.unlink(missing_ok=True)
        raise

    inventory = {
        "schema": "wcfi.fbmsg.inventory.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "export_root": str(root),
        "message_files": len(files),
        "conversations": len(conversations),
        "messages": message_count,
        "attachments": attachment_count,
        "missing_local_attachments": missing_attachments,
        "attachment_kinds": dict(sorted(kinds.items())),
        "task_candidates": len(candidates),
        "task_filter_terms": terms or [],
        "privacy": "Local-only raw index. Do not commit or send externally without explicit approval.",
    }
    _write_json_atomic(private_dir / "inventory.json", inventory)
    (private_dir / "task-candidates.md").write_text(task_markdown(candidates), encoding="utf-8")
    return inventory
