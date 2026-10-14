"""Semantic inbox: agents push things you need to know; you triage them in a local web page.

The flow:

0. ``wcfi inbox up`` makes sure the local receiver server is running (starts it if not).
1. An agent sweeps a source (Gmail, Messenger, ...) and pushes distilled items to the server.
2. You work through the inbox in the browser as it fills, marking each item and adding notes.
3. The agent reads your decisions back (``wcfi inbox list --json``) and drafts replies.

Everything is stored locally, outside any Git worktree.
"""

from __future__ import annotations

from .client import InboxClient, InboxClientError
from .store import KINDS, STATUSES, InboxError, InboxStore

__all__ = [
    "KINDS",
    "STATUSES",
    "InboxClient",
    "InboxClientError",
    "InboxError",
    "InboxStore",
]
