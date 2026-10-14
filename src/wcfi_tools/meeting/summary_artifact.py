"""Strict version-1 summary artifact parsing. No network or embedded execution."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from markdown_it import MarkdownIt
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

SECTIONS = ("Metadata", "Attendance", "Decisions / Approvals", "Discussion & Updates",
            "Motions", "Action Items / Assignments", "Next Steps", "Adjournment")
TABLES = {"Metadata": ("Field", "Detail"),
          "Motions": ("Motion Text", "Vote Result", "Moved By", "Seconded By"),
          "Action Items / Assignments": ("Owner", "Task Description", "Due Date", "Status")}
TEMPLATE = "meeting_minutes_pageless"
MAX_BYTES = 2_000_000
MD = MarkdownIt("commonmark", {"html": True}).enable("table")


# Parse even unsafe link destinations so validation rejects them instead of treating
# them as harmless-looking literal Markdown. Rendering only uses validated tokens.
MD.validateLink = lambda _url: True


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class Artifact:
    path: Path
    metadata: dict
    body: str
    tokens: list
    sha256: str
    warnings: tuple[str, ...]


def _fail(message, line=None):
    raise ArtifactError((f"Line {line}: " if line else "") + message)


def _node(node, depth=0):
    if depth > 12:
        _fail("Metadata nesting exceeds 12 levels")
    if isinstance(node, MappingNode):
        result = {}
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                _fail("Metadata keys must be strings", key.start_mark.line + 2)
            if key.value in result:
                _fail(f"Duplicate metadata key: {key.value}", key.start_mark.line + 2)
            result[key.value] = _node(value, depth + 1)
        return result
    if isinstance(node, SequenceNode):
        return [_node(value, depth + 1) for value in node.value]
    if isinstance(node, ScalarNode):
        if node.tag not in {"tag:yaml.org,2002:str", "tag:yaml.org,2002:int",
                            "tag:yaml.org,2002:bool", "tag:yaml.org,2002:null"}:
            _fail("Unsupported metadata scalar; quote dates as strings", node.start_mark.line + 2)
        if node.tag.endswith(":str"):
            return node.value
        return yaml.safe_load(node.value)
    _fail("Unsupported metadata node")


def _keys(value, required, optional=(), field="metadata"):
    if not isinstance(value, dict):
        _fail(f"{field} must be a mapping")
    missing, unknown = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or unknown:
        _fail(f"{field}: missing {sorted(missing)}; unsupported {sorted(unknown)}")


def validate_metadata(meta):
    _keys(meta, {"wcfi_version", "kind", "title", "meeting_date", "presentation"}, {"evidence"})
    if type(meta["wcfi_version"]) is not int or meta["wcfi_version"] != 1:
        _fail("wcfi_version must be integer 1")
    if meta["kind"] != "meeting_summary":
        _fail("kind must be meeting_summary")
    for field in ("title", "meeting_date"):
        if not isinstance(meta[field], str) or not meta[field].strip():
            _fail(f"{field} must be a nonempty quoted string")
    if "\n" in meta["title"] or len(meta["title"]) > 250:
        _fail("title must be a single line of at most 250 characters")
    if meta["meeting_date"] != "unknown":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["meeting_date"]):
                raise ValueError()
            date.fromisoformat(meta["meeting_date"])
        except ValueError:
            _fail('meeting_date must be quoted YYYY-MM-DD or "unknown"')
    presentation = meta["presentation"]
    if isinstance(presentation, dict) and ({"page_limit", "overflow"} & presentation.keys()):
        _fail("Remove page_limit/overflow; choose meeting_minutes_pageless version 1 or 2, layout pageless")
    _keys(presentation, {"template", "template_version", "layout"}, field="presentation")
    if (presentation["template"] != TEMPLATE or type(presentation["template_version"]) is not int
            or presentation["template_version"] not in (1, 2) or presentation["layout"] != "pageless"):
        _fail("Supported presentation: meeting_minutes_pageless version 1 or 2, layout pageless")
    evidence = meta.get("evidence", {})
    _keys(evidence, set(), {"source_index", "review_notes"}, "evidence")
    for field, value in evidence.items():
        if not isinstance(value, str) or not value.strip():
            _fail(f"evidence.{field} must be a relative path")


def inline_text(token):
    return "".join(child.content if child.type in {"text", "code_inline"} else
                   "\n" if child.type in {"softbreak", "hardbreak"} else ""
                   for child in token.children or [])


def validate_body(body: str, *, title: str | None = None, line_offset: int = 0):
    if not body.strip():
        _fail("Markdown body is empty")
    tokens = MD.parse(body)
    permitted = {"heading_open", "heading_close", "paragraph_open", "paragraph_close", "inline",
                 "bullet_list_open", "bullet_list_close", "ordered_list_open", "ordered_list_close",
                 "list_item_open", "list_item_close", "table_open", "table_close", "thead_open", "thead_close",
                 "tbody_open", "tbody_close", "tr_open", "tr_close", "th_open", "th_close", "td_open", "td_close"}
    inlines = {"text", "code_inline", "softbreak", "hardbreak", "em_open", "em_close", "strong_open",
               "strong_close", "link_open", "link_close"}
    sections, content, table_counts = [], {}, {}
    current = None
    h1 = 0
    in_header = False
    for i, token in enumerate(tokens):
        line = (token.map[0] + 1 + line_offset) if token.map else None
        if token.type not in permitted:
            _fail(f"Unsupported Markdown {token.type}; use headings, paragraphs, lists or pipe tables", line)
        if token.type == "thead_open":
            in_header = True
        elif token.type == "thead_close":
            in_header = False
        if token.type == "heading_open":
            heading = inline_text(tokens[i + 1])
            if token.tag not in {"h1", "h2", "h3"}:
                _fail("Only headings 1-3 are supported", line)
            if token.tag == "h1":
                h1 += 1
                if h1 > 1 or sections or heading not in {"Board Meeting Minutes", title}:
                    _fail("Use one opening H1: Board Meeting Minutes, or the metadata title", line)
            elif token.tag == "h2":
                sections.append(heading)
                current = heading
                content[current] = False
            elif current is None:
                _fail("H3 must belong to a required section", line)
        if token.type == "inline":
            for child in token.children or []:
                if child.type not in inlines:
                    _fail(f"Unsupported inline Markdown {child.type}", line)
                if child.type == "link_open":
                    url = child.attrGet("href") or ""
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                        _fail("Links must be ordinary HTTPS URLs without credentials", line)
            if not in_header and current and i and tokens[i - 1].type != "heading_open" and inline_text(token).strip():
                content[current] = True
        if token.type == "table_open":
            if current not in TABLES:
                _fail("Tables belong only in Metadata, Motions, and Action Items / Assignments", line)
            table_counts[current] = table_counts.get(current, 0) + 1
            headers = []
            for part in tokens[i + 1:]:
                if part.type == "thead_close":
                    break
                if part.type == "inline":
                    headers.append(inline_text(part))
            if tuple(headers) != TABLES[current]:
                _fail(f"{current} table columns must be: {', '.join(TABLES[current])}", line)
            # markdown-it otherwise silently pads/truncates ragged pipe rows.
            if token.map:
                from markdown_it.rules_block.table import escapedSplit
                for number in range(*token.map):
                    row = body.splitlines()[number].strip()
                    cells = escapedSplit(row)
                    if row.startswith("|"):
                        cells = cells[1:]
                    if row.endswith("|") and not row.endswith("\\|"):
                        cells = cells[:-1]
                    if len(cells) != len(headers):
                        _fail("Ragged table row; keep the exact number of columns", number + 1 + line_offset)
    if tuple(sections) != SECTIONS:
        _fail("Required H2 sections, once each in order: " + "; ".join(SECTIONS))
    if any(not value for value in content.values()):
        _fail("Empty sections must explicitly say information was not captured")
    if any(table_counts.get(section) != 1 for section in TABLES):
        _fail("Metadata, Motions and Action Items / Assignments each require one table")
    return tokens


def parse_text(text: str, path: Path) -> Artifact:
    text = text.removeprefix("\ufeff")
    if len(text.encode("utf-8")) > MAX_BYTES or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        _fail("Artifact is too large or contains control characters")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        _fail("Start with a standalone --- YAML front-matter delimiter", 1)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        _fail("Missing closing --- front-matter delimiter")
    front = "".join(lines[1:end])
    if len(front) > 32_000:
        _fail("Metadata exceeds 32,000 characters")
    try:
        for token in yaml.scan(front):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                _fail("YAML tags, anchors and aliases are not allowed", token.start_mark.line + 2)
        meta = _node(yaml.compose(front, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        _fail("Invalid YAML metadata: " + str(exc))
    validate_metadata(meta)
    body = "".join(lines[end + 1:])
    tokens = validate_body(body, title=meta["title"], line_offset=end + 1)
    warnings = []
    base = path.resolve().parent
    for key, relative in meta.get("evidence", {}).items():
        relative = relative.replace("\\", "/")
        if Path(relative).is_absolute() or re.match(r"^[a-zA-Z]:", relative) or ":" in relative:
            _fail(f"evidence.{key} must be a relative local path")
        resolved = (base / relative).resolve()
        if not resolved.is_relative_to(base):
            _fail(f"evidence.{key} escapes the artifact directory")
        if not resolved.exists():
            warnings.append(f"Missing local evidence sidecar: {relative}; unresolved issues are unknown")
        elif not resolved.is_file():
            _fail(f"Evidence sidecar is not a regular file: {relative}")
    return Artifact(path.resolve(), meta, body, tokens, hashlib.sha256(text.encode()).hexdigest(), tuple(warnings))


def load(path: Path) -> Artifact:
    if not path.name.lower().endswith(".wcfi.md"):
        _fail("Select one explicit *.wcfi.md artifact")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        _fail("Artifact exceeds two million bytes")
    try:
        return parse_text(data.decode("utf-8-sig"), path)
    except UnicodeError as exc:
        raise ArtifactError("Artifact must be UTF-8") from exc
