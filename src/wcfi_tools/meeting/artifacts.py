"""Render copy-able artifacts from the pipeline outputs."""

from __future__ import annotations

import re
from typing import Any


def render_atomic_markdown(summary: dict[str, Any], categories: list[str]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in categories}
    for item in summary.get("items", []):
        grouped.setdefault(item.get("category", "unresolved questions"), []).append(item)

    lines = ["# Atomic Summary", ""]
    for category in categories:
        items = grouped.get(category, [])
        if not items:
            continue
        lines += [f"## {category.title()}", ""]
        for item in items:
            details = [
                f"owner: {item.get('owner') or 'Not explicitly captured'}",
                f"due: {item.get('due_date') or 'N/A'}",
                f"status: {item.get('status') or 'N/A'}",
                f"confidence: {item.get('confidence')}",
            ]
            if item.get("uncertain"):
                details.append("uncertain")
            lines.append(f"- {item.get('claim', '').strip()} ({'; '.join(details)})")
            if item.get("notes"):
                lines.append(f"  Note: {item['notes']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITAL = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _strip_inline(text: str) -> str:
    text = _LINK.sub(lambda m: m.group(1), text)  # [label](url) -> label
    text = _BOLD.sub(r"\1", text)
    text = _ITAL.sub(r"\1", text)
    return text.strip()


def markdown_to_paste_block(markdown: str) -> str:
    """Flatten finalized minutes markdown into plain, section-by-section text for pasting
    into a styled template. Tables become ``cell | cell`` rows; emphasis/links are stripped."""
    out: list[str] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            title = _strip_inline(re.sub(r"^\d+\.\s*", "", heading.group(1)))
            out += ["", f"=== {title} ===", ""]
            continue
        if re.match(r"^\|?\s*:?-{2,}", line) or re.match(r"^\|(\s*:?-+:?\s*\|)+\s*$", line):
            continue  # table separator row
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append(" | ".join(_strip_inline(c) for c in cells))
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet:
            indent = " " * (len(bullet.group(1)) // 2 * 4)
            out.append(f"{indent}{_strip_inline(bullet.group(2))}")
            continue
        out.append(_strip_inline(line))
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n"
