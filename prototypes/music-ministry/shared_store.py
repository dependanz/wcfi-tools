"""Offline Phase-3 domain prototype: SQLite persistence and server-side policy.

NOT a web server or an authentication provider. The subject passed to methods MUST
come from a server-verified identity adapter, never browser input. No real users,
Wix tokens, or production data may be used with this experimental module.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROLES = frozenset({"member", "song_editor", "worship_leader", "admin"})
ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,99}$")
KEY = re.compile(r"^[A-G](?:#|b)?m?$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SONG_FIELDS = ("id", "title", "artist", "key", "category", "bpm", "meter", "description", "sequence", "sections")


class NotAuthenticated(Exception):
    """No active approved membership (future HTTP mapping: 401)."""


class NotAuthorized(Exception):
    """Authenticated member lacks permission (future HTTP mapping: 403)."""


class InvalidRecord(ValueError):
    """Invalid incoming record (future HTTP mapping: 422)."""


class VersionConflict(Exception):
    """Optimistic-concurrency or unique-ID conflict (future HTTP mapping: 409)."""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _require_id(value: object) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise InvalidRecord("Expected stable lowercase identifier")
    return value


def validate_song(value: object) -> dict:
    if not isinstance(value, dict):
        raise InvalidRecord("Song must be an object")
    song = {k: value[k] for k in SONG_FIELDS if k in value}
    _require_id(song.get("id"))
    if not isinstance(song.get("title"), str) or not 1 <= len(song["title"].strip()) <= 150:
        raise InvalidRecord("Song title is required")
    if not isinstance(song.get("key"), str) or not KEY.fullmatch(song["key"]):
        raise InvalidRecord("Song key is required")
    for k in ("artist", "category", "description", "sequence", "meter"):
        if k in song and (not isinstance(song[k], str) or len(song[k]) > 2000):
            raise InvalidRecord(f"Invalid song {k}")
    if "bpm" in song and (not isinstance(song["bpm"], int) or isinstance(song["bpm"], bool) or not 20 <= song["bpm"] <= 300):
        raise InvalidRecord("Invalid BPM")
    sections = song.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 60:
        raise InvalidRecord("At least one section required")
    cleaned = []
    labels = set()
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("label"), str):
            raise InvalidRecord("Invalid section")
        label = section["label"].strip()
        lines = section.get("lines")
        if not label or len(label) > 100 or label.casefold() in labels:
            raise InvalidRecord("Duplicate or invalid section label")
        labels.add(label.casefold())
        if not isinstance(lines, list) or len(lines) > 100 or any(not isinstance(line, str) or len(line) > 1000 for line in lines):
            raise InvalidRecord("Invalid lyric/chord lines")
        cleaned.append({"label": label, "lines": list(lines)})
    song["title"] = song["title"].strip()
    song["sections"] = cleaned
    return song


def validate_lineup(value: object, *, require_date: bool = False) -> dict:
    if not isinstance(value, dict):
        raise InvalidRecord("Lineup must be an object")
    _require_id(value.get("id"))
    title = value.get("title")
    date = value.get("date", "")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 150:
        raise InvalidRecord("Lineup title required")
    # V19 permits undated personal drafts; published plans must have a real date.
    if not isinstance(date, str) or (require_date and not date):
        raise InvalidRecord("Lineup date required")
    if date:
        if not DAY.fullmatch(date):
            raise InvalidRecord("Lineup date required")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise InvalidRecord("Invalid calendar date") from exc
    items = value.get("songs")
    if not isinstance(items, list) or not 1 <= len(items) <= 60:
        raise InvalidRecord("Lineup needs songs")
    songs = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidRecord("Invalid lineup song")
        song_id = _require_id(item.get("id"))
        key = item.get("key")
        if key is not None and (not isinstance(key, str) or not KEY.fullmatch(key)):
            raise InvalidRecord("Invalid performance key")
        order = item.get("arrangement", [])
        if not isinstance(order, list) or len(order) > 100 or any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in order):
            raise InvalidRecord("Invalid arrangement sequence")
        songs.append({"id": song_id, "key": key, "arrangement": list(order)})
    note = value.get("notes", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise InvalidRecord("Invalid lineup notes")
    return {"id": value["id"], "title": title.strip(), "date": date, "notes": note, "songs": songs}


def validate_member_preferences(value: object) -> dict:
    """Narrow active preference projection. All additional browser state stays archived."""
    if not isinstance(value, dict) or set(value) != {"keys", "font", "lyrics"}:
        raise InvalidRecord("Invalid member preference fields")
    raw_keys = value["keys"]
    if not isinstance(raw_keys, dict) or len(raw_keys) > 1000:
        raise InvalidRecord("Invalid personal display keys")
    keys = {}
    for song_id, key in raw_keys.items():
        if (not isinstance(song_id, str) or not 1 <= len(song_id) <= 160 or
                any(ord(char) < 32 for char in song_id) or
                not isinstance(key, str) or not KEY.fullmatch(key)):
            raise InvalidRecord("Invalid personal display key")
        keys[song_id] = key
    font = value["font"]
    if type(font) is not int or not 18 <= font <= 28:
        raise InvalidRecord("Invalid personal chart font size")
    if type(value["lyrics"]) is not bool:
        raise InvalidRecord("Invalid personal lyrics display flag")
    return {"keys": keys, "font": font, "lyrics": value["lyrics"]}


def extract_v19_member_preferences(hub: object) -> dict:
    """Select only V19's active personal-display settings; don't import other keys."""
    if not isinstance(hub, dict):
        raise InvalidRecord("Invalid V19 browser hub state")
    return validate_member_preferences({
        "keys": hub.get("keys", {}),
        "font": hub.get("font", 21),
        "lyrics": hub.get("lyrics", False),
    })


class MusicStore:
    """Standalone SQLite domain store. No HTTP routes, cookies or OAuth flows."""

    def __init__(self, file: str | Path):
        self.file = str(file)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.file, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    def initialize(self) -> None:
        # Caller chooses an isolated local path. Never operate on Wix/site data.
        with self._connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS members (
                subject TEXT PRIMARY KEY,
                approved INTEGER NOT NULL CHECK(approved IN (0,1)),
                roles TEXT NOT NULL,
                updated_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS songs (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('draft','approved')),
                owner TEXT NOT NULL REFERENCES members(subject),
                version INTEGER NOT NULL CHECK(version>=1),
                updated_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS lineups (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('draft','published')),
                owner TEXT NOT NULL REFERENCES members(subject),
                version INTEGER NOT NULL CHECK(version>=1),
                updated_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS audit (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS migration_source_archives (
                source_sha256 TEXT PRIMARY KEY,
                canonical_backup BLOB NOT NULL,
                owner TEXT NOT NULL REFERENCES members(subject),
                created_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS member_preferences (
                subject TEXT PRIMARY KEY REFERENCES members(subject),
                payload TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version>=1),
                updated_at TEXT NOT NULL
              );
            """)

    @staticmethod
    def _actor(db: sqlite3.Connection, verified_subject: str, accepted_roles: set[str]) -> sqlite3.Row:
        # NEVER expose verified_subject as a caller-controlled HTTP parameter.
        row = db.execute("SELECT * FROM members WHERE subject=?", (verified_subject,)).fetchone()
        if row is None or row["approved"] != 1:
            raise NotAuthenticated("Approved member sign-in required")
        roles = set(json.loads(row["roles"]))
        if "admin" not in roles and not roles.intersection(accepted_roles):
            raise NotAuthorized("Insufficient application role")
        return row

    @staticmethod
    def _audit(db: sqlite3.Connection, actor: str, action: str, record: str) -> None:
        db.execute("INSERT INTO audit (actor,kind,record_id,at) VALUES (?,?,?,?)", (actor, action, record, _utc()))

    def upsert_member(self, verified_admin: str, subject: str, roles: set[str], approved: bool) -> None:
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 240 or not roles or not roles <= ROLES or not isinstance(approved, bool):
            raise InvalidRecord("Invalid membership details")
        with self._connection() as db:
            self._actor(db, verified_admin, {"admin"})
            db.execute("""INSERT INTO members(subject,approved,roles,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(subject) DO UPDATE SET approved=excluded.approved, roles=excluded.roles, updated_at=excluded.updated_at""",
                       (subject, int(approved), _json(sorted(roles)), _utc()))
            self._audit(db, verified_admin, "membership_set", subject)

    def get_member_preferences(self, verified_subject: str) -> dict:
        """Approved members can read their own preferences, never another member's."""
        with self._connection() as db:
            self._actor(db, verified_subject, {"member", "song_editor", "worship_leader"})
            row = db.execute(
                "SELECT payload,version FROM member_preferences WHERE subject=?",
                (verified_subject,),
            ).fetchone()
            if row is None:
                return {"keys": {}, "font": 21, "lyrics": False, "revision": 0}
            return {**json.loads(row["payload"]), "revision": row["version"]}

    def update_member_preferences(
        self, verified_subject: str, preferences: object, expected_revision: int,
    ) -> int:
        """Per-member optimistic concurrency; caller has no target-subject override."""
        clean = validate_member_preferences(preferences)
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidRecord("Invalid preferences revision")
        with self._connection() as db:
            self._actor(db, verified_subject, {"member", "song_editor", "worship_leader"})
            if expected_revision == 0:
                try:
                    db.execute(
                        "INSERT INTO member_preferences(subject,payload,version,updated_at) "
                        "VALUES(?,?,1,?)",
                        (verified_subject, _json(clean), _utc()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise VersionConflict("Preferences changed on another device") from exc
                revision = 1
            else:
                changed = db.execute(
                    "UPDATE member_preferences SET payload=?, version=version+1, "
                    "updated_at=? WHERE subject=? AND version=?",
                    (_json(clean), _utc(), verified_subject, expected_revision),
                )
                if changed.rowcount != 1:
                    raise VersionConflict("Preferences changed on another device")
                revision = expected_revision + 1
            self._audit(db, verified_subject, "member_preferences_updated", verified_subject)
            return revision

    def list_songs(self, verified_subject: str) -> list[dict]:
        with self._connection() as db:
            row = self._actor(db, verified_subject, {"member", "song_editor", "worship_leader"})
            roles = set(json.loads(row["roles"]))
            if "admin" in roles:
                records = db.execute("SELECT * FROM songs ORDER BY id").fetchall()
            else:
                records = db.execute("SELECT * FROM songs WHERE state='approved' OR (owner=? AND state='draft' AND ?=1) ORDER BY id",
                                     (verified_subject, int("song_editor" in roles))).fetchall()
            return [{**json.loads(r["payload"]), "revision": r["version"], "state": r["state"]} for r in records]

    def create_song(self, verified_subject: str, item: dict) -> int:
        with self._connection() as db:
            self._actor(db, verified_subject, {"song_editor"})
            song = validate_song(item)
            try:
                db.execute("INSERT INTO songs VALUES (?,?,?,?,?,?)",
                           (song["id"], _json(song), "draft", verified_subject, 1, _utc()))
            except sqlite3.IntegrityError as exc:
                raise VersionConflict("Song ID already exists") from exc
            self._audit(db, verified_subject, "song_created", song["id"])
        return 1

    def update_song_draft(self, verified_subject: str, item: dict, expected_revision: int) -> int:
        with self._connection() as db:
            actor = self._actor(db, verified_subject, {"song_editor"})
            song = validate_song(item)
            old = db.execute("SELECT * FROM songs WHERE id=?", (song["id"],)).fetchone()
            if old is None:
                raise InvalidRecord("Song not found")
            if old["owner"] != verified_subject and "admin" not in json.loads(actor["roles"]):
                raise NotAuthorized("Not the draft owner")
            if old["state"] != "draft":
                raise NotAuthorized("Approved songs require a new revision proposal")
            if type(expected_revision) is not int or old["version"] != expected_revision:
                raise VersionConflict("Song was edited on another device")
            revision = expected_revision + 1
            changed = db.execute("UPDATE songs SET payload=?,version=?,updated_at=? WHERE id=? AND version=? AND state='draft'",
                                 (_json(song), revision, _utc(), song["id"], expected_revision))
            if changed.rowcount != 1:
                raise VersionConflict("Song was edited on another device")
            self._audit(db, verified_subject, "song_draft_updated", song["id"])
            return revision

    def approve_song(self, verified_admin: str, song_id: str) -> None:
        song_id = _require_id(song_id)
        with self._connection() as db:
            self._actor(db, verified_admin, {"admin"})
            if not db.execute("SELECT 1 FROM songs WHERE id=?", (song_id,)).fetchone():
                raise InvalidRecord("Song not found")
            db.execute("UPDATE songs SET state='approved',version=version+1,updated_at=? WHERE id=?", (_utc(), song_id))
            self._audit(db, verified_admin, "song_approved", song_id)

    def import_catalogue_v1(self, verified_subject: str, envelope: object) -> int:
        with self._connection() as db:
            self._actor(db, verified_subject, {"song_editor"})
            if not isinstance(envelope, dict) or envelope.get("format") != "wcfi-song-catalogue" or type(envelope.get("version")) is not int or envelope["version"] != 1:
                raise InvalidRecord("Unsupported song catalogue")
            items = envelope.get("songs")
            if not isinstance(items, list) or len(items) > 500:
                raise InvalidRecord("Invalid catalogue size")
            songs = [validate_song(x) for x in items]
            if len({x["id"] for x in songs}) != len(songs):
                raise InvalidRecord("Duplicate IDs in catalogue")
            if songs:
                collision = db.execute("SELECT id FROM songs WHERE id IN ({})".format(",".join("?" * len(songs))),
                                       tuple(x["id"] for x in songs)).fetchone()
                if collision:
                    raise VersionConflict("Catalogue contains an existing song ID")
            for song in songs:
                db.execute("INSERT INTO songs VALUES(?,?,?,?,?,?)", (song["id"], _json(song), "draft", verified_subject, 1, _utc()))
                self._audit(db, verified_subject, "song_imported", song["id"])
        return len(songs)

    def create_lineup(self, verified_subject: str, item: dict) -> int:
        with self._connection() as db:
            self._actor(db, verified_subject, {"worship_leader"})
            lineup = validate_lineup(item)
            for entry in lineup["songs"]:
                if not db.execute("SELECT 1 FROM songs WHERE id=? AND state='approved'", (entry["id"],)).fetchone():
                    raise InvalidRecord("Lineup requires approved songs")
            try:
                db.execute("INSERT INTO lineups VALUES (?,?,?,?,?,?)",
                           (lineup["id"], _json(lineup), "draft", verified_subject, 1, _utc()))
            except sqlite3.IntegrityError as exc:
                raise VersionConflict("Lineup ID already exists") from exc
            self._audit(db, verified_subject, "lineup_created", lineup["id"])
        return 1

    def list_lineups(self, verified_subject: str) -> list[dict]:
        with self._connection() as db:
            row = self._actor(db, verified_subject, {"member", "song_editor", "worship_leader"})
            roles = set(json.loads(row["roles"]))
            if "admin" in roles:
                records = db.execute("SELECT * FROM lineups ORDER BY id").fetchall()
            else:
                records = db.execute("SELECT * FROM lineups WHERE state='published' OR (state='draft' AND owner=? AND ?=1) ORDER BY id",
                                     (verified_subject, int("worship_leader" in roles))).fetchall()
            return [{**json.loads(r["payload"]), "revision": r["version"], "state": r["state"]} for r in records]

    def update_lineup_draft(self, verified_subject: str, item: dict, expected_revision: int) -> int:
        with self._connection() as db:
            actor = self._actor(db, verified_subject, {"worship_leader"})
            lineup = validate_lineup(item)
            old = db.execute("SELECT * FROM lineups WHERE id=?", (lineup["id"],)).fetchone()
            if old is None:
                raise InvalidRecord("Lineup not found")
            if old["owner"] != verified_subject and "admin" not in json.loads(actor["roles"]):
                raise NotAuthorized("Not the draft owner")
            if old["state"] != "draft":
                raise NotAuthorized("Published lineups require a new revision proposal")
            if type(expected_revision) is not int or old["version"] != expected_revision:
                raise VersionConflict("Lineup was edited on another device")
            for entry in lineup["songs"]:
                if not db.execute("SELECT 1 FROM songs WHERE id=? AND state='approved'", (entry["id"],)).fetchone():
                    raise InvalidRecord("Lineup requires approved songs")
            changed = db.execute("UPDATE lineups SET payload=?,version=version+1,updated_at=? WHERE id=? AND version=? AND state='draft'",
                                 (_json(lineup), _utc(), lineup["id"], expected_revision))
            if changed.rowcount != 1:
                raise VersionConflict("Lineup was edited on another device")
            self._audit(db, verified_subject, "lineup_draft_updated", lineup["id"])
        return expected_revision + 1

    def publish_lineup(self, verified_admin: str, lineup_id: str) -> None:
        lineup_id = _require_id(lineup_id)
        with self._connection() as db:
            self._actor(db, verified_admin, {"admin"})
            record = db.execute("SELECT payload FROM lineups WHERE id=?", (lineup_id,)).fetchone()
            if not record:
                raise InvalidRecord("Lineup not found")
            lineup = validate_lineup(json.loads(record["payload"]), require_date=True)
            for entry in lineup["songs"]:
                song = db.execute("SELECT state FROM songs WHERE id=?", (entry["id"],)).fetchone()
                if not song or song["state"] != "approved":
                    raise InvalidRecord("Lineup contains a song not approved for members")
            db.execute("UPDATE lineups SET state='published',version=version+1,updated_at=? WHERE id=?", (_utc(), lineup_id))
            self._audit(db, verified_admin, "lineup_published", lineup_id)