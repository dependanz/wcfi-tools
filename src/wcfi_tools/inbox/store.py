"""SQLite-backed store for the semantic inbox.

Agents *push* items: things the human needs to know, distilled from Gmail, Messenger and so on.
The human *decides* each one (relevant / settled / not relevant) and can add a note. Pushing the
same ``(source, external_id)`` again refreshes the content but never overwrites a decision or a
note, so sweeps can be re-run safely. ``memory`` holds durable notes the agent keeps about the
human's preferences; ``sources`` remembers where each source's last sweep left off.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

STATUSES = ("relevant", "settled", "notrelevant")
KINDS = ("needs-you", "fyi", "other")
CONTENT_FIELDS = (
    "kind", "title", "detail", "quote", "thread", "people", "link", "occurred_at", "due",
)
_TEXT_LIMITS = {
    "source": 60, "external_id": 300, "title": 300, "detail": 6000, "quote": 3000,
    "thread": 200, "link": 2000, "occurred_at": 40, "due": 10,
}
_UNSET: Any = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    thread TEXT NOT NULL DEFAULT '',
    people TEXT NOT NULL DEFAULT '[]',
    link TEXT NOT NULL DEFAULT '',
    occurred_at TEXT,
    due TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT,
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    seq INTEGER NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS items_seq ON items (seq);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'general',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    cursor TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    last_sweep_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class InboxError(ValueError):
    """Raised when pushed or edited data doesn't fit the inbox schema."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def item_id(source: str, external_id: str) -> str:
    """Stable id for an item, derived from where it came from."""
    digest = hashlib.sha1(f"{source}\x1f{external_id}".encode(), usedforsecurity=False)
    return digest.hexdigest()[:16]


def _text(raw: dict, key: str, *, required: bool = False) -> str:
    value = raw.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InboxError(f"'{key}' must be a string")
    value = value.strip()
    if required and not value:
        raise InboxError(f"'{key}' is required")
    return value[: _TEXT_LIMITS.get(key, 6000)]


def normalize_item(raw: Any) -> dict[str, Any]:
    """Validate one pushed item and return it in storage shape."""
    if not isinstance(raw, dict):
        raise InboxError("each item must be a JSON object")
    required = ("source", "external_id", "title")
    item: dict[str, Any] = {
        key: _text(raw, key, required=key in required)
        for key in ("source", "external_id", "title", "detail", "quote", "thread", "link")
    }
    kind = raw.get("kind") or "other"
    if kind not in KINDS:
        raise InboxError(f"'kind' must be one of: {', '.join(KINDS)}")
    people = raw.get("people") or []
    if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
        raise InboxError("'people' must be a list of names")
    occurred = _text(raw, "occurred_at") or None
    if occurred:
        try:
            datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        except ValueError:
            raise InboxError(f"'occurred_at' must be ISO 8601, got {occurred!r}") from None
    due = _text(raw, "due") or None
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            raise InboxError(f"'due' must be a date like 2026-09-13, got {due!r}") from None
    names = [p.strip()[:80] for p in people if p.strip()][:30]
    item.update(kind=kind, people=json.dumps(names), occurred_at=occurred, due=due)
    return item


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["people"] = json.loads(out.get("people") or "[]")
    return out


class InboxStore:
    """Thread-safe wrapper around one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- change counter (drives live updates) ------------------------------------------------

    @property
    def seq(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
        return int(row[0]) if row else 0

    def _bump(self) -> int:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES ('seq', '1') "
            "ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
        )
        return self.seq

    # -- items --------------------------------------------------------------------------------

    def upsert(self, raw_items: list[Any]) -> dict[str, Any]:
        """Add or refresh items. Validates the whole batch first; all or nothing."""
        items = [normalize_item(raw) for raw in raw_items]
        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        now = _now()
        sets = ", ".join(f"{field} = ?" for field in CONTENT_FIELDS)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    iid = item_id(item["source"], item["external_id"])
                    content = tuple(item[field] for field in CONTENT_FIELDS)
                    row = self._db.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
                    if row is None:
                        self._db.execute(
                            "INSERT INTO items (id, source, external_id, kind, title, detail,"
                            " quote, thread, people, link, occurred_at, due, received_at,"
                            " updated_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (iid, item["source"], item["external_id"], *content, now, now,
                             self._bump()),
                        )
                        added.append(iid)
                    elif tuple(row[field] for field in CONTENT_FIELDS) != content:
                        self._db.execute(
                            f"UPDATE items SET {sets}, updated_at = ?, seq = ? WHERE id = ?",
                            (*content, now, self._bump(), iid),
                        )
                        updated.append(iid)
                    else:
                        unchanged.append(iid)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"added": added, "updated": updated, "unchanged": unchanged, "seq": self.seq}

    def get(self, iid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
        return _as_dict(row) if row else None

    def list_items(
        self,
        *,
        status: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        since: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Newest first. ``status`` may be ``open`` (undecided) or one of STATUSES."""
        clauses: list[str] = []
        args: list[Any] = []
        if status == "open":
            clauses.append("status IS NULL")
        elif status:
            if status not in STATUSES:
                raise InboxError(f"'status' must be open or one of: {', '.join(STATUSES)}")
            clauses.append("status = ?")
            args.append(status)
        if source:
            clauses.append("source = ?")
            args.append(source)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if since is not None:
            clauses.append("seq > ?")
            args.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM items {where} "
            "ORDER BY COALESCE(occurred_at, received_at) DESC, seq DESC LIMIT ?"
        )
        with self._lock:
            rows = self._db.execute(sql, (*args, max(1, min(limit, 5000)))).fetchall()
        return [_as_dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, kind, COUNT(*) FROM items GROUP BY status, kind"
            ).fetchall()
        out = {"total": 0, "open": 0, "needs_you_open": 0, **{s: 0 for s in STATUSES}}
        for status, kind, count in rows:
            out["total"] += count
            out[status or "open"] += count
            if status is None and kind == "needs-you":
                out["needs_you_open"] += count
        return out

    def decide(self, iid: str, *, status: Any = _UNSET, note: Any = _UNSET) -> dict[str, Any]:
        """Record the human's decision and/or note. ``status=None`` clears the decision."""
        if status is not _UNSET and status is not None and status not in STATUSES:
            raise InboxError(f"'status' must be one of: {', '.join(STATUSES)} (or null)")
        if note is not _UNSET and not isinstance(note, str):
            raise InboxError("'note' must be a string")
        with self._lock:
            if self._db.execute("SELECT 1 FROM items WHERE id = ?", (iid,)).fetchone() is None:
                raise KeyError(iid)
            sets: list[str] = []
            args: list[Any] = []
            if status is not _UNSET:
                sets += ["status = ?", "decided_at = ?"]
                args += [status, _now() if status else None]
            if note is not _UNSET:
                sets.append("note = ?")
                args.append(note[:8000])
            if sets:
                sets.append("seq = ?")
                args.append(self._bump())
                self._db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", (*args, iid))
            return self.get(iid)  # type: ignore[return-value]

    # -- agent memory -------------------------------------------------------------------------

    def memory_add(self, text: str, scope: str = "general") -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise InboxError("memory text is empty")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO memory (scope, text, created_at) VALUES (?, ?, ?)",
                ((scope or "general").strip()[:60], text[:2000], _now()),
            )
            self._bump()
            row = self._db.execute("SELECT * FROM memory WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def memory_list(self, scope: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if scope:
                rows = self._db.execute(
                    "SELECT * FROM memory WHERE scope = ? ORDER BY id", (scope,)
                ).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM memory ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def memory_delete(self, mid: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memory WHERE id = ?", (mid,))
            if cur.rowcount:
                self._bump()
        return cur.rowcount > 0

    # -- sweep cursors ------------------------------------------------------------------------

    def sources_list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def source_set(
        self,
        name: str,
        *,
        cursor: str | None = None,
        notes: str | None = None,
        swept: bool = False,
    ) -> dict[str, Any]:
        """Update a source's cursor/notes; ``None`` leaves a field as it was."""
        name = (name or "").strip()[:60]
        if not name:
            raise InboxError("source name is empty")
        with self._lock:
            self._db.execute("INSERT INTO sources (name) VALUES (?) ON CONFLICT DO NOTHING", (name,))
            if cursor is not None:
                self._db.execute("UPDATE sources SET cursor = ? WHERE name = ?", (cursor[:500], name))
            if notes is not None:
                self._db.execute("UPDATE sources SET notes = ? WHERE name = ?", (notes[:2000], name))
            if swept:
                self._db.execute(
                    "UPDATE sources SET last_sweep_at = ? WHERE name = ?", (_now(), name)
                )
            row = self._db.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
        return dict(row)
