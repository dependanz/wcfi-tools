"""Local Wix media catalogs."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_catalog(
    descriptors: Iterable[dict[str, Any]],
    output: Path,
    *,
    force: bool = False,
) -> int:
    """Atomically write a Wix descriptor JSONL catalog."""
    if output.exists() and not force:
        raise FileExistsError(f"Catalog already exists: {output}. Use --force to replace it.")
    temp = output.with_name(f".{output.name}.tmp")
    count = 0
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            header = {
                "record_type": "catalog",
                "schema": "wcfi.wix.media.v1",
                "captured_at": datetime.now(UTC).isoformat(),
            }
            handle.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
            for descriptor in descriptors:
                record = {
                    "record_type": "file",
                    "schema": "wcfi.wix.media.v1",
                    "descriptor": descriptor,
                }
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        os.replace(temp, output)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return count


def summarize_catalog(path: Path) -> dict[str, Any]:
    """Read a local catalog and return non-content counts for safe console display."""
    media_types: Counter[str] = Counter()
    folders: Counter[str] = Counter()
    total = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from None
            if record.get("record_type") != "file":
                continue
            descriptor = record.get("descriptor", {})
            total += 1
            media_type = descriptor.get("mediaType") or descriptor.get("type") or "unknown"
            parent = descriptor.get("parentFolderId") or "root"
            media_types[str(media_type)] += 1
            folders[str(parent)] += 1
    return {
        "files": total,
        "media_types": dict(sorted(media_types.items())),
        "folder_count": len(folders),
    }
