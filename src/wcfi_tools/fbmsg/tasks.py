"""Deterministic local task-candidate extraction from normalized messages."""

from __future__ import annotations

import re
from typing import Any

ACTION_RULES = {
    "request": re.compile(r"\b(?:please|can you|could you|would you)\b", re.IGNORECASE),
    "obligation": re.compile(
        r"\b(?:need(?:s)? to|must|should|have to|remember to|don'?t forget)\b",
        re.IGNORECASE,
    ),
    "action": re.compile(
        r"\b(?:prepare|bring|send|make|create|finish|follow up|check|call|contact|"
        r"post|upload|edit|review|schedule|book|confirm|organize|arrange)\b",
        re.IGNORECASE,
    ),
    "deadline": re.compile(r"\b(?:deadline|due|by (?:today|tomorrow|monday|tuesday|"
        r"wednesday|thursday|friday|saturday|sunday|\d{1,2}(?:/\d{1,2})?))\b", re.IGNORECASE),
}
DEADLINE_HINT = re.compile(
    r"\b(?:by|before|on|due)\s+(?:today|tomorrow|tonight|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\d{1,2}(?:[/-]\d{1,2})(?:[/-]\d{2,4})?)\b",
    re.IGNORECASE,
)


def task_candidate(
    message: dict[str, Any],
    *,
    terms: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return a review candidate; never claims that an inferred task is confirmed."""
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    title = str(message.get("conversation_title") or "")
    haystack = f"{title}\n{content}"
    requested_terms = [term.casefold() for term in terms or [] if term.strip()]
    if requested_terms and not any(term in haystack.casefold() for term in requested_terms):
        return None
    reasons = [name for name, pattern in ACTION_RULES.items() if pattern.search(content)]
    if not reasons:
        return None
    deadline = DEADLINE_HINT.search(content)
    excerpt = " ".join(content.split())
    if len(excerpt) > 700:
        excerpt = excerpt[:697] + "..."
    return {
        "schema": "wcfi.fbmsg.task_candidate.v1",
        "status": "needs_review",
        "message_id": message.get("message_id"),
        "conversation_id": message.get("conversation_id"),
        "conversation_title": message.get("conversation_title"),
        "sent_at": message.get("sent_at"),
        "sender": message.get("sender"),
        "owner": None,
        "deadline_hint": deadline.group(0) if deadline else None,
        "reasons": reasons,
        "excerpt": excerpt,
        "source_file": message.get("source_file"),
    }


def task_markdown(candidates: list[dict[str, Any]]) -> str:
    """Render a human-review queue without elevating candidates to facts."""
    lines = [
        "# Facebook Messenger task candidates",
        "",
        (
            "These are rule-based candidates, not confirmed commitments. Review ownership, "
            "deadline, context, and current relevance before acting."
        ),
        "",
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"## {index}. Needs review",
                "",
                f"- Sent: {candidate.get('sent_at') or 'unknown'}",
                f"- Conversation: {candidate.get('conversation_title') or candidate.get('conversation_id')}",
                f"- Sender: {candidate.get('sender') or 'unknown'}",
                f"- Signals: {', '.join(candidate.get('reasons', []))}",
                f"- Deadline hint: {candidate.get('deadline_hint') or 'none detected'}",
                f"- Source: {candidate.get('source_file')}",
                "",
                str(candidate.get("excerpt") or ""),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
