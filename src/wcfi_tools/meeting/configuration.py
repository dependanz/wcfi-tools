"""Versioned meeting configuration and metadata-only inventory; no provider calls."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_NAME = "meeting.wcfi.toml"
MATERIALS = ("recordings", "documents", "images", "videos")
EXTENSIONS = {
    "recordings": {".m4a", ".mp3", ".wav", ".mp4", ".mpeg", ".mpga", ".webm"},
    "documents": {".pdf", ".docx", ".txt", ".md"},
    "images": {".jpg", ".jpeg", ".png", ".webp"},
    "videos": {".mp4", ".webm"},
}
ROLES = ("agenda", "previous_minutes", "supporting_notes", "transcript", "reference")
PROTECTED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules"}
GENERATED_DIRS = {"_work", "_minutes_work", ".wcfi", ".wcfi-cache", ".wcfi-runs"}
GENERATED_FILES = {CONFIG_NAME, "SUMMARY.wcfi.md", "minutes.md", "paste-block.txt",
                   "source-index.md", "review-needed.md", ".wcfi-private"}
BASE_DEFAULTS = {
    "version": 1,
    "processing": {"transcription": {"chunk_minutes": 5},
                   "speakers": {"identify": False}},
    "output": {"directory": "summary", "formats": ["markdown", "plain_text"],
               "template": "meeting_minutes_pageless", "template_version": 2,
               "layout": "pageless", "include_source_index": True, "include_review_notes": True},
}


class ConfigError(ValueError):
    """An invalid config or unsafe meeting path."""


def _fail(field: str, message: str) -> None:
    raise ConfigError(f"{field}: {message}")


def _table(value: Any, field: str, keys: set[str]) -> None:
    if not isinstance(value, dict):
        _fail(field, "expected a table")
    unknown = set(value) - keys
    if unknown:
        remedy = ""
        if unknown & {"page_limit", "overflow"}:
            remedy = '; remove page_limit/overflow and use layout = "pageless"'
        _fail(field, f"unknown field(s): {', '.join(sorted(unknown))}{remedy}")


def _string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(field, "expected a nonempty string")


def _number(value: Any, field: str, *, positive: bool = False, integer: bool = False) -> None:
    types = (int,) if integer else (int, float)
    if type(value) not in types or not math.isfinite(value):
        _fail(field, "expected a finite number" if not integer else "expected an integer")
    if value < 0 or (positive and value == 0):
        _fail(field, "must be positive" if positive else "must be nonnegative")


def _boolean(value: Any, field: str) -> None:
    if type(value) is not bool:
        _fail(field, "expected true or false")


def input_name(value: Any, field: str = "path") -> str:
    _string(value, field)
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (PureWindowsPath(value).drive or normalized.startswith("/") or
            any(p in {"", ".", ".."} for p in parts) or
            any(c in value for c in '*?[]:\x00\r\n') or
            "$" in value or "%" in value or normalized.startswith("~")):
        _fail(field, "use a literal relative file path without '..', globs, URLs, or expansion")
    return normalized


def protected(parts: tuple[str, ...]) -> bool:
    return any(p.lower().startswith(".") or p.lower() in PROTECTED_DIRS for p in parts)


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Validate present values; missing settings are allowed in a partial config."""
    _table(data, "config", {"version", "meeting", "processing", "output", *MATERIALS, "excluded"})
    if type(data.get("version")) is not int or data["version"] != 1:
        _fail("version", "expected integer 1")
    meeting = data.get("meeting", {})
    _table(meeting, "meeting", {"date", "title", "time", "timezone", "location", "prepared_by"})
    for k, v in meeting.items():
        if k == "date":
            if type(v) is not date and v != "unknown":
                _fail("meeting.date", 'use a TOML local date YYYY-MM-DD or "unknown"')
        else:
            _string(v, f"meeting.{k}")
        if k == "time" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            _fail("meeting.time", "expected HH:MM in 24-hour format")
        if k == "timezone":
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError):
                _fail("meeting.timezone", "unknown IANA timezone (install tzdata if unavailable)")
    processing = data.get("processing", {})
    _table(processing, "processing",
           {"transcription", "summarization", "image_text", "visual_analysis", "speakers"})
    for kind, values in processing.items():
        keys = {"identify", "count"} if kind == "speakers" else {"provider", "model"}
        if kind == "transcription":
            keys |= {"chunk_minutes"}
        _table(values, f"processing.{kind}", keys)
        for key, value in values.items():
            field = f"processing.{kind}.{key}"
            if key == "identify":
                _boolean(value, field)
            elif key in {"count", "chunk_minutes"}:
                _number(value, field, positive=True, integer=key == "count")
            else:
                _string(value, field)
        if kind == "speakers" and values.get("identify") is False and "count" in values:
            _fail("processing.speakers.count", "omit count when identification is disabled")
    output = data.get("output", {})
    _table(output, "output", set(BASE_DEFAULTS["output"]))
    for key, value in output.items():
        if key == "formats":
            if (not isinstance(value, list) or not value or
                    any(x not in ("markdown", "plain_text") for x in value) or
                    len(set(value)) != len(value) or "markdown" not in value):
                _fail("output.formats", "use unique markdown/plain_text values including markdown")
        elif key == "template_version":
            _number(value, "output.template_version", positive=True, integer=True)
            if value not in (1, 2):
                _fail("output.template_version", "installed versions are 1 and 2")
        elif key.startswith("include_"):
            _boolean(value, f"output.{key}")
            if value is not True:
                _fail(f"output.{key}", "version 1 requires true")
        else:
            _string(value, f"output.{key}")
        if key == "layout" and value != "pageless":
            _fail("output.layout", 'version 1 requires "pageless"')
        if key == "template":
            if value == "meeting_minutes_one_page":
                _fail("output.template", "replace the draft id with meeting_minutes_pageless")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
                _fail("output.template", "expected an installed template identifier, not a path/URL")

    ids: set[str] = set()
    paths: dict[str, list[str]] = {}
    orders = []
    for kind in (*MATERIALS, "excluded"):
        entries = data.get(kind, [])
        if not isinstance(entries, list):
            _fail(kind, "expected an array of tables")
        for i, item in enumerate(entries):
            field = f"{kind}[{i}]"
            keys = {"path", "reason"} if kind == "excluded" else {"id", "path"}
            keys |= {"recordings": {"order", "start_seconds", "end_seconds", "overlap_group"},
                     "documents": {"role"}, "images": {"use"},
                     "videos": {"use", "frames_seconds"}}.get(kind, set())
            _table(item, field, keys)
            name = input_name(item.get("path"), f"{field}.path")
            previous = paths.setdefault(name, [])
            if previous and sorted([*previous, kind]) != ["recordings", "videos"]:
                _fail(field, "path is repeated or both included and excluded")
            previous.append(kind)
            if kind == "excluded":
                if "reason" in item:
                    _string(item["reason"], f"{field}.reason")
                continue
            identity = item.get("id")
            if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", identity):
                _fail(f"{field}.id", "expected a lowercase stable id")
            if identity in ids:
                _fail(f"{field}.id", "duplicate id")
            ids.add(identity)
            if protected(tuple(Path(name).parts)):
                _fail(f"{field}.path", "hidden, credential, and system sources are excluded")
            if Path(name).suffix.lower() not in EXTENSIONS[kind]:
                _fail(f"{field}.path", f"unsupported file type for {kind}")
            if kind == "recordings":
                _number(item.get("order"), f"{field}.order", positive=True, integer=True)
                orders.append(item["order"])
                for key in ("start_seconds", "end_seconds"):
                    if key in item:
                        _number(item[key], f"{field}.{key}")
                if "end_seconds" in item and item["end_seconds"] <= item.get("start_seconds", 0):
                    _fail(field, "end_seconds must be greater than start_seconds")
                if "overlap_group" in item:
                    _string(item["overlap_group"], f"{field}.overlap_group")
            if kind == "documents" and item.get("role") not in ROLES:
                _fail(f"{field}.role", f"choose one of {', '.join(ROLES)}")
            if kind == "images" and item.get("use") not in {"extract_text", "analyze_visual"}:
                _fail(f"{field}.use", "choose extract_text or analyze_visual")
            if kind == "videos":
                if item.get("use") != "analyze_visual":
                    _fail(f"{field}.use", "expected analyze_visual")
                frames = item.get("frames_seconds")
                if not isinstance(frames, list) or not frames:
                    _fail(f"{field}.frames_seconds", "explicit nonempty frame times required")
                for frame in frames:
                    _number(frame, f"{field}.frames_seconds")
                if sorted(set(frames)) != frames:
                    _fail(f"{field}.frames_seconds", "frame times must be ascending and unique")
    if sorted(orders) != list(range(1, len(orders) + 1)):
        _fail("recordings.order", "orders must be unique and consecutive starting at 1")
    return data


def load(root: Path) -> dict[str, Any]:
    path = root / CONFIG_NAME
    if not path.exists():
        return {"version": 1}
    if path.is_symlink() or path.resolve().parent != root.resolve():
        _fail(CONFIG_NAME, "config must be a local file directly in the meeting folder")
    try:
        if path.stat().st_size > 1024 * 1024:
            _fail(CONFIG_NAME, "config exceeds 1 MiB")
        return validate(tomllib.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def reusable(data: dict[str, Any]) -> dict[str, Any]:
    """Allowlist reusable settings: never meeting metadata, paths, credentials or approvals."""
    processing = copy.deepcopy(data.get("processing", {}))
    processing.get("speakers", {}).pop("count", None)
    return {
        "processing": processing,
        "output": {k: copy.deepcopy(v) for k, v in data.get("output", {}).items()
                   if k in BASE_DEFAULTS["output"] and k != "directory"},
    }


def resolve(config: dict[str, Any], *, setup: dict | None = None,
            last: dict | None = None, overrides: dict | None = None) -> tuple[dict, dict]:
    """Merge leaf values by presence, preserving false and recording their origins."""
    validate(config)
    data: dict = {}
    origins: dict[str, str] = {}

    def merge(dest: dict, src: dict, origin: str, prefix: str = "") -> None:
        for key, value in src.items():
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                target = dest.setdefault(key, {})
                # Models from a different provider must not leak across precedence layers.
                if ("provider" in value and "provider" in target and
                        target["provider"] != value["provider"]):
                    target.pop("model", None)
                    origins.pop(f"{field}.model", None)
                if key == "speakers" and value.get("identify") is False:
                    target.pop("count", None)
                merge(target, value, origin, field)
            else:
                dest[key] = copy.deepcopy(value)
                origins[field] = origin

    merge(data, BASE_DEFAULTS, "built-in default")
    merge(data, reusable(setup or {}), "setup defaults")
    merge(data, reusable(last or {}), "last successful summarization")
    merge(data, config, CONFIG_NAME)
    merge(data, overrides or {}, "command line")
    return validate(data), origins


def resolve_input(root: Path, name: str) -> Path:
    name = input_name(name)
    root = root.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        _fail(name, "input resolves outside the meeting folder")
    return path


def output_path(root: Path, data: dict) -> Path:
    name = data.get("output", {}).get("directory")
    _string(name, "output.directory")
    output = (root / name).resolve()
    if output == root.resolve() or root.resolve().is_relative_to(output):
        _fail("output.directory", "cannot be the meeting root or its ancestor")
    for parent in (output, *output.parents):
        if (parent / ".git").exists():
            _fail("output.directory", "private outputs cannot be inside a Git worktree")
    if output.exists() and not output.is_dir():
        _fail("output.directory", "must be a directory")
    for kind in MATERIALS:
        for entry in data.get(kind, []):
            source = resolve_input(root, entry["path"])
            if source == output or source.is_relative_to(output):
                _fail("output.directory", "must not contain any selected input")
    return output


@dataclass(frozen=True)
class InventoryItem:
    path: str
    kind: str
    status: str
    reason: str = ""


def inventory(root: Path, data: dict) -> list[InventoryItem]:
    """Walk safely by metadata, never following escaping links or hidden directories."""
    root = root.resolve()
    if not root.is_dir():
        _fail("meeting folder", "directory does not exist")
    output = output_path(root, data)
    selected: dict[str, list[str]] = {}
    physical: dict[Path, list[str]] = {}
    for kind in MATERIALS:
        for entry in data.get(kind, []):
            name = input_name(entry["path"])
            path = resolve_input(root, name)
            if not path.is_file():
                _fail(name, "selected input is missing or not a file")
            # os.access is a metadata check; actual reads/probes occur only after review.
            if not os.access(path, os.R_OK):
                _fail(name, "selected input is not readable")
            prior = physical.setdefault(path, [])
            if prior and sorted([*prior, kind]) != ["recordings", "videos"]:
                _fail(name, "multiple entries resolve to the same input")
            prior.append(kind)
            selected.setdefault(name, []).append(kind)
    excluded = {input_name(e["path"]): e.get("reason", "Explicitly excluded")
                for e in data.get("excluded", [])}
    for name in excluded:
        path = resolve_input(root, name)
        if path in physical:
            _fail(name, "an included file is also excluded through an alias")
    result: list[InventoryItem] = []
    seen: set[Path] = set()
    found: set[str] = set()

    def walk(folder: Path) -> None:
        resolved = folder.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            rel = path.relative_to(root).as_posix()
            found.add(rel)
            target = path.resolve()
            if not target.is_relative_to(root):
                result.append(InventoryItem(rel, "link", "builtin", "Link outside meeting folder"))
                if rel in selected:
                    _fail(rel, "selected link escapes the meeting folder")
            elif protected(tuple(path.relative_to(root).parts)):
                result.append(InventoryItem(rel, "protected", "builtin", "Hidden/system exclusion"))
            elif target == output or target.is_relative_to(output):
                result.append(InventoryItem(rel, "output", "builtin", "Generated output"))
            elif path.name in GENERATED_DIRS or (path.name in GENERATED_FILES and rel not in selected):
                result.append(InventoryItem(rel, "generated", "builtin", "WCFI config/cache/output"))
            elif path.is_dir():
                walk(path)
            elif path.is_file():
                kind = next((k for k, exts in EXTENSIONS.items() if path.suffix.lower() in exts),
                            "unsupported")
                status = ("selected" if rel in selected else "excluded" if rel in excluded
                          else "unsupported" if kind == "unsupported" else "unassigned")
                result.append(InventoryItem(rel, "+".join(selected.get(rel, [kind])),
                                            status, excluded.get(rel, "")))
            else:
                result.append(InventoryItem(rel, "special", "builtin", "Not a regular file"))
    walk(root)
    visible_selected = {x.path for x in result if x.status == "selected"}
    if set(selected) - visible_selected:
        _fail("materials", "selected sources are hidden under an excluded/generated directory")
    for name in excluded.keys() - found:
        result.append(InventoryItem(name, "missing", "excluded", "Excluded path is absent"))
    return result


def missing_fields(data: dict) -> list[str]:
    missing = []
    if "date" not in data.get("meeting", {}):
        missing.append("meeting.date")
    if not any(data.get(k) for k in MATERIALS):
        missing.append("materials (select at least one source)")
    needed = {"summarization"}
    if data.get("recordings"):
        needed.add("transcription")
    if any(x["use"] == "extract_text" for x in data.get("images", [])):
        needed.add("image_text")
    if data.get("videos") or any(x["use"] == "analyze_visual" for x in data.get("images", [])):
        needed.add("visual_analysis")
    for kind in sorted(needed):
        for key in ("provider", "model"):
            value = data.get("processing", {}).get(kind, {}).get(key)
            if not value or (key == "model" and value.startswith("REPLACE_")):
                missing.append(f"processing.{kind}.{key}")
    return missing


def dump(data: dict) -> str:
    validate(data)
    lines = ["version = 1", ""]

    def value(v: Any) -> str:
        if type(v) is date:
            return v.isoformat()
        return json.dumps(v, ensure_ascii=False, allow_nan=False)

    def table(name: str, entries: dict) -> None:
        scalars = {k: v for k, v in entries.items() if not isinstance(v, dict)}
        if scalars:
            lines.append(f"[{name}]")
            lines.extend(f"{k} = {value(v)}" for k, v in scalars.items())
            lines.append("")
        for k, v in entries.items():
            if isinstance(v, dict):
                table(f"{name}.{k}", v)

    for key in ("meeting", "processing", "output"):
        if data.get(key):
            table(key, data[key])
    for kind in (*MATERIALS, "excluded"):
        for item in data.get(kind, []):
            lines.append(f"[[{kind}]]")
            lines.extend(f"{k} = {value(input_name(v) if k == 'path' else v)}"
                         for k, v in item.items())
            lines.append("")
    return "\n".join(lines)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)
