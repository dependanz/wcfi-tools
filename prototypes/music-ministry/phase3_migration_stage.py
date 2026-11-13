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

from phase3_migration_plan import inspect_v19_backup, is_known_v19_demo_lineup
from shared_store import (
    InvalidRecord,
    MusicStore,
    NotAuthorized,
    VersionConflict,
    _json,
    _utc,
    extract_v19_member_preferences,
    validate_lineup,
    validate_song,
)

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


def _validated_parts(envelope: object) -> tuple[list[dict], list[dict], str, int]:
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
    if any(not isinstance(x, dict) for x in raw_lineups):
        raise InvalidRecord("Malformed lineup entries require separate review")
    demos = [item for item in raw_lineups if item.get("demo") is True]
    if len(demos) > 1 or any(not is_known_v19_demo_lineup(item) for item in demos):
        raise InvalidRecord("Unrecognized or duplicate demo lineup requires review")
    real_lineups = [item for item in raw_lineups if item.get("demo") is not True]
    draft = hub.get("draft", {})
    if not isinstance(draft, dict) or any(draft.get(k) for k in ("title", "date", "notes", "songs")):
        raise InvalidRecord("Incomplete personal lineup draft requires separate migration")
    songs = [validate_song(item) for item in raw_songs]
    lineups = [validate_lineup(item) for item in real_lineups]
    song_ids = [item["id"] for item in songs]
    lineup_ids = [item["id"] for item in lineups]
    if len(set(song_ids)) != len(song_ids) or len(set(lineup_ids)) != len(lineup_ids):
        raise InvalidRecord("Duplicate source record identifiers")
    if any(entry["id"] not in set(song_ids) for item in lineups for entry in item["songs"]):
        raise InvalidRecord("Lineup references a song absent from this backup")
    return songs, lineups, hashlib.sha256(_canonical_bytes(envelope)).hexdigest(), len(demos)


def _v19_personal_preferences(envelope: dict) -> dict:
    """Validate personal display settings before any SQLite transaction begins."""
    rows = {row["key"]: row["value"] for row in envelope["records"]}
    try:
        hub = json.loads(rows["wcfi-music-hub-v2"], object_pairs_hook=_no_duplicate_fields)
    except (ValueError, TypeError) as exc:
        raise InvalidRecord("Invalid browser preferences source") from exc
    return extract_v19_member_preferences(hub)


def prepare_v19_draft_stage(envelope: object) -> dict:
    """Read-only preview. No contents, identifiers, lyrics or private notes returned."""
    songs, lineups, digest, excluded_demos = _validated_parts(envelope)
    preferences = _v19_personal_preferences(envelope)
    return {
        "source_sha256": digest,
        "songs_to_stage": len(songs),
        "lineups_to_stage": len(lineups),
        "undated_lineups": sum(item["date"] == "" for item in lineups),
        "demo_lineups_excluded": excluded_demos,
        "target_state": "draft_only",
        "approved_or_published": 0,
        "personal_preferences_ready": True,
        "personal_display_key_count": len(preferences["keys"]),
    }


def read_v19_source_archive(
    store: MusicStore, verified_admin: str, source_sha256: str,
) -> dict:
    """Internal admin-only archival readback; NEVER expose through a member API.

    This is a byte-faithful reconstruction of the parsed JSON backup (including
    verbatim localStorage strings), not an exact original downloaded file copy.
    """
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise InvalidRecord("Invalid source fingerprint")
    with store._connection() as db:
        store._actor(db, verified_admin, {"admin"})
        row = db.execute(
            "SELECT canonical_backup FROM migration_source_archives "
            "WHERE source_sha256=?",
            (source_sha256,),
        ).fetchone()
        if row is None:
            raise InvalidRecord("Source archive not found")
        content = bytes(row["canonical_backup"])
    observed_digest = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(observed_digest, source_sha256):
        raise InvalidRecord("Source archive integrity mismatch")
    try:
        restored = json.loads(content.decode("utf-8"), object_pairs_hook=_no_duplicate_fields)
    except (ValueError, UnicodeError) as exc:
        raise InvalidRecord("Source archive is not valid JSON") from exc
    if not isinstance(restored, dict):
        raise InvalidRecord("Source archive must be an object")
    return restored


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
    songs, lineups, digest, excluded_demos = _validated_parts(envelope)
    preferences = _v19_personal_preferences(envelope)
    if not isinstance(reviewed_sha256, str) or not hmac.compare_digest(reviewed_sha256, digest):
        raise VersionConflict("The inspected backup has changed; review it again")
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        store._actor(db, verified_admin, {"admin"})
        try:
            # Archive the ENTIRE validated backup in the very same transaction
            # as projected draft rows, including the demo excluded from staging
            # and all raw browser-owned values. No public API exposes this BLOB.
            canonical_backup = _canonical_bytes(envelope)
            db.execute(
                "INSERT INTO migration_source_archives "
                "(source_sha256,canonical_backup,owner,created_at) VALUES(?,?,?,?)",
                (digest, sqlite3.Binary(canonical_backup), verified_admin, _utc()),
            )
            db.execute(
                "INSERT INTO member_preferences(subject,payload,version,updated_at) "
                "VALUES(?,?,1,?)",
                (verified_admin, _json(preferences), _utc()),
            )
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
        "demo_lineups_excluded": excluded_demos,
        "source_backup_archived": True,
        "personal_preferences_staged": True,
        "personal_display_key_count": len(preferences["keys"]),
        "target_state": "draft_only",
    }
