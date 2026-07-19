"""Prompts and JSON schemas for atomic extraction, consolidation, and minutes rendering."""

from __future__ import annotations

from typing import Any

ATOMIC_CATEGORIES = [
    "metadata",
    "attendance",
    "approvals",
    "decisions",
    "motions",
    "discussion updates",
    "action items",
    "next steps",
    "adjournment",
    "unresolved questions",
]


def transcription_prompt() -> str:
    return (
        "This is a WCFI church board meeting. Speech may code-switch between Filipino/Tagalog and "
        "English, including Taglish. Preserve the language as spoken; do not translate unless the "
        "speaker clearly translated it. Use punctuation and paragraph breaks. Pay close attention to "
        "church-board terms such as motions, approvals, proxy voting, formal members, financial "
        "reports, budgets, building pledges, ministries, action items, pastors, and board member names."
    )


def atomic_extraction_system_prompt() -> str:
    return (
        "You extract atomic factual meeting notes from WCFI church board meeting transcripts. "
        "The meeting may be in English, Filipino/Tagalog, and Taglish. Return only facts supported "
        "by the transcript chunk. Create small standalone facts suitable for board minutes. "
        "Do not invent names, motions, votes, owners, due dates, or attendance. When a 'Speaker:' "
        "is given for the chunk, that is the identified speaker — attribute the chunk's facts, "
        "motions, and action-item owners to that person by name. Otherwise use 'Person' or "
        "'Not explicitly captured' when the transcript does not clearly identify a detail. "
        "Set uncertain=true when the transcript is garbled or attribution is weak."
    )


def atomic_consolidation_system_prompt() -> str:
    return (
        "You consolidate WCFI board meeting atomic notes. Deduplicate repeated facts, preserve "
        "important decisions, approvals, motions, action items, next steps, and unresolved questions. "
        "Do not invent details. Keep uncertainty explicit."
    )


def minutes_system_prompt() -> str:
    return (
        "You write concise, formal church board meeting minutes. Follow the requested Markdown "
        "structure exactly. Keep the language practical and conservative. Use 'Not explicitly captured' "
        "rather than guessing. Preserve action items and approvals clearly."
    )


def atomic_summary_schema() -> dict[str, Any]:
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "category", "claim", "owner", "due_date", "status",
            "source_chunk", "confidence", "uncertain", "notes",
        ],
        "properties": {
            "category": {"type": "string", "enum": ATOMIC_CATEGORIES},
            "claim": {"type": "string"},
            "owner": {"type": "string"},
            "due_date": {"type": "string"},
            "status": {"type": "string"},
            "source_chunk": {"type": "string"},
            "confidence": {"type": "number"},
            "uncertain": {"type": "boolean"},
            "notes": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": item_schema}},
    }


def minutes_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["markdown"],
        "properties": {"markdown": {"type": "string"}},
    }
