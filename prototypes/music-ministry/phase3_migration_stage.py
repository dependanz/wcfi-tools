"""Synthetic-only, explicitly approved V19-to-SQLite draft staging.

This is NOT the authenticated production migration or an HTTP route. Only an
already server-verified admin subject may call the write operation; never accept
a browser-provided identity as verified. All source material must stay private.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Any

from phase3_migration_plan import inspect_v19_backup
from shared_store import InvalidRecord, MusicStore, NotAuthorized, VersionConflict, _json, _utc, validate_lineup, validate_song

ALLOWED_SOURCE_KEYS = frozenset({"wcfi-song-workshop-v1", "wcfi-music-hub-v2"})


def _no_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRecord("Duplicate storage JSON field")
        result[key] = value
    return result


def _canonical_bytes(envelope: dict) -> bytes:
    try:
        return json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise InvalidRecord("Invalid backup JSON values") from exc


def _validated_parts(envelope: object) -> tuple[list[dict], list[dict], str]:
    """Fail closed on any unmodeled browser state; return only validated records."""
    plan = inspect_v19_backup(envelope)
    if plan.record_count != 2 or plan.song_blocked or plan.lineup_blocked:
        raise InvalidRecord("Backup has incomplete or incompatible work; inspect preflight")
    if plan.unfinished_editor_drafts or plan.arrangement_entries:
        raise InvalidRecord("Unmigrated drafts or arrangements; staging refused")
    if any(not blocker.startswith("migration:") for blocker in plan.blockers):
        raise InvalidRecord("Backup needs manual review before staging")
    rows = {row["key"]: row["value"] for row in envelope["records"]}
    if set(rows) != ALLOWED_SOURCE_KEYS:
        raise InvalidRecord("Backup contains unmodeled local storage keys")
    try:
        workshop = json.loads(rows["wcfi-song-workshop-v1"], object_pairs_hook=_no_duplicate_fields)
        hub = json.loads(rows["wcfi-music-hub-v2"], object_pairs_hook=_no_duplicate_fields)
    except (ValueError, TypeError) as exc:
        raise InvalidRecord("Invalid local storage JSON") from exc
    if not isinstance(workshop, dict) or not isinstance(hub, dict):
        raise InvalidRecord("Invalid local storage objects")
    if workshop.get("arrangements", {}) != {} or workshop.get("deletedSongIds", []) != []:
        raise InvalidRecord("Unmodeled arrangements or deleted song markers")
    raw_songs = workshop.get("songs")
    raw_lineups = hub.get("lineups")
    if not isinstance(raw_songs, list) or not isinstance(raw_lineups, list):
        raise InvalidRecord("Missing song or lineup list")
    if any(not isinstance(x, dict) or x.get("demo") is True for x in raw_lineups):
        raise InvalidRecord("Demo or malformed lineups require separate review")
    draft = hub.get("draft", {})
    if not isinstance(draft, dict) or any(draft.get(k) for k in ("title", "date", "notes", "songs")):
        raise InvalidRecord("Incomplete personal lineup draft requires separate migration")
    songs = [validate_song(item) for item in raw_songs]
    lineups = [validate_lineup(item) for item in raw_lineups]
    song_ids = [item["id"] for item in songs]
    lineup_ids = [item["id"] for item in lineups]
    if len(set(song_ids)) != len(song_ids) or len(set(lineup_ids)) != len(lineup_ids):
        raise InvalidRecord("Duplicate source record identifiers")
    if any(entry["id"] not in set(song_ids) for item in lineups for entry in item["songs"]):
        raise InvalidRecord("Lineup references a song absent from this backup")
    return songs, lineups, hashlib.sha256(_canonical_bytes(envelope)).hexdigest()


def prepare_v19_draft_stage(envelope: object) -> dict:
    """Read-only preview. No contents, identifiers, lyrics or private notes returned."""
    songs, lineups, digest = _validated_parts(envelope)
    return {
        "source_sha256": digest,
        "songs_to_stage": len(songs),
        "lineups_to_stage": len(lineups),
        "undated_lineups": sum(item["date"] == "" for item in lineups),
        "target_state": "draft_only",
        "approved_or_published": 0,
    }


def stage_v19_drafts(
    store: MusicStore, verified_admin: str, envelope: object,
    *, reviewed_sha256: str, confirmed: bool = False,
) -> dict:
    """Atomic SQLite writes only after explicit confirmation and verified admin.

    No OAuth/test identity is created here. This method has NO HTTP route.
    No silent overwrite or approval. The browser backup remains authoritative.
    """
    if confirmed is not True:
        raise NotAuthorized("Explicit staging confirmation required")
    songs, lineups, digest = _validated_parts(envelope)
    if not isinstance(reviewed_sha256, str) or not hmac.compare_digest(reviewed_sha256, digest):
        raise VersionConflict("The inspected backup has changed; review it again")
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        store._actor(db, verified_admin, {"admin"})
        try:
            for item in songs:
                db.execute(
                    "INSERT INTO songs VALUES (?,?,?,?,?,?)",
                    (item["id"], _json(item), "draft", verified_admin, 1, _utc()),
                )
                store._audit(db, verified_admin, "v19_song_staged", item["id"])
            for item in lineups:
                db.execute(
                    "INSERT INTO lineups VALUES (?,?,?,?,?,?)",
                    (item["id"], _json(item), "draft", verified_admin, 1, _utc()),
                )
                store._audit(db, verified_admin, "v19_lineup_staged", item["id"])
        except sqlite3.IntegrityError as exc:
            raise VersionConflict("Existing shared record conflicts with local backup") from exc
    return {
        "source_sha256": digest,
        "songs_staged": len(songs),
        "lineups_staged": len(lineups),
        "target_state": "draft_only",
    }
