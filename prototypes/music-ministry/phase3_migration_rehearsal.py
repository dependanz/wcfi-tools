"""Disposable V19 migration rehearsal for an isolated local machine.

This is NOT a production migration or a Wix login. The only SQLite database
exists inside a newly created TemporaryDirectory and is removed after verifying
the staged private drafts. All identities are synthetic fixture-only accounts.
Never logs charts, lyrics, lineup names, notes, user IDs or the raw backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from phase3_migration_plan import MAX_BYTES
from phase3_migration_stage import (
    _no_duplicate_fields,
    _v19_personal_preferences,
    _validated_parts,
    prepare_v19_draft_stage,
    read_v19_source_archive,
    stage_v19_drafts,
)
from shared_store import MusicStore

_FIXTURE_ADMIN = "fixture-scratch-admin"
_FIXTURE_MEMBER = "fixture-scratch-member"


class RehearsalFailed(RuntimeError):
    """A verification failed; no existing database is altered."""


def _verify_rows(store: MusicStore, songs: list[dict], lineups: list[dict], preferences: dict) -> None:
    """Confirm actual staged records equal the domain's expected projection."""
    stored_songs = store.list_songs(_FIXTURE_ADMIN)
    stored_lineups = store.list_lineups(_FIXTURE_ADMIN)

    def compare(rows: list[dict], expected: list[dict]) -> None:
        if len(rows) != len(expected):
            raise RehearsalFailed("Round-trip record count mismatch")
        by_id = {row["id"]: row for row in expected}
        for row in rows:
            if row.get("state") != "draft" or row.get("revision") != 1:
                raise RehearsalFailed("A staged record was not a private first revision")
            cleaned = {key: value for key, value in row.items() if key not in {"state", "revision"}}
            if cleaned != by_id.get(row["id"]):
                raise RehearsalFailed("Normalized record did not round-trip exactly")

    compare(stored_songs, songs)
    compare(stored_lineups, lineups)
    if store.list_songs(_FIXTURE_MEMBER) or store.list_lineups(_FIXTURE_MEMBER):
        raise RehearsalFailed("A private staged record was exposed to a regular member")
    actual_preferences = store.get_member_preferences(_FIXTURE_ADMIN)
    if actual_preferences != {**preferences, "revision": 1}:
        raise RehearsalFailed("Personal preferences did not round-trip correctly")
    if store.get_member_preferences(_FIXTURE_MEMBER) != {
        "keys": {}, "font": 21, "lyrics": False, "revision": 0,
    }:
        raise RehearsalFailed("Private preferences leaked into another member account")
    with store._connection() as db:
        private_songs = db.execute(
            "SELECT COUNT(*) FROM songs WHERE state='draft' AND owner=?", (_FIXTURE_ADMIN,)
        ).fetchone()[0]
        private_lineups = db.execute(
            "SELECT COUNT(*) FROM lineups WHERE state='draft' AND owner=?", (_FIXTURE_ADMIN,)
        ).fetchone()[0]
        audit_events = db.execute(
            "SELECT COUNT(*) FROM audit WHERE kind IN ('v19_song_staged','v19_lineup_staged')"
        ).fetchone()[0]
    if (private_songs, private_lineups, audit_events) != (
        len(songs), len(lineups), len(songs) + len(lineups)
    ):
        raise RehearsalFailed("Stored privacy, ownership or audit invariant failed")


def rehearse_v19_envelope(envelope: dict) -> dict:
    """Stage only into temporary SQLite and return metadata-only verification."""
    prepared = prepare_v19_draft_stage(envelope)
    expected_songs, expected_lineups, digest, demos = _validated_parts(envelope)
    expected_preferences = _v19_personal_preferences(envelope)
    if digest != prepared["source_sha256"]:
        raise RehearsalFailed("Preflight fingerprint disagreement")

    with tempfile.TemporaryDirectory(prefix="wcfi-music-rehearsal-") as temporary:
        db_file = Path(temporary) / "synthetic-only.sqlite"
        store = MusicStore(db_file)
        store.initialize()
        # Fixture-only bootstrap into an isolated, disposable database.
        with closing(sqlite3.connect(db_file)) as conn, conn:
            conn.execute(
                "INSERT INTO members(subject,approved,roles,updated_at) VALUES(?,?,?,?)",
                (_FIXTURE_ADMIN, 1, '["admin"]', "rehearsal-fixture"),
            )
        store.upsert_member(_FIXTURE_ADMIN, _FIXTURE_MEMBER, {"member"}, True)
        staged = stage_v19_drafts(
            store, _FIXTURE_ADMIN, envelope,
            reviewed_sha256=digest, confirmed=True,
        )
        _verify_rows(store, expected_songs, expected_lineups, expected_preferences)
        # Verify that the complete original logical backup is recoverable,
        # not just normalized songs/lineups. This includes private browser
        # preferences, raw storage strings and the excluded demo lineup.
        archived = read_v19_source_archive(store, _FIXTURE_ADMIN, digest)
        if archived != envelope:
            raise RehearsalFailed("Full browser source archive round-trip mismatch")
        with store._connection() as db:
            archived_row = db.execute(
                "SELECT COUNT(*), owner FROM migration_source_archives "
                "WHERE source_sha256=?",
                (digest,),
            ).fetchone()
            if archived_row[0] != 1 or archived_row[1] != _FIXTURE_ADMIN:
                raise RehearsalFailed("Source archive count or ownership mismatch")
        if staged["songs_staged"] != len(expected_songs) or (
            staged["lineups_staged"] != len(expected_lineups)
        ) or staged["demo_lineups_excluded"] != demos:
            raise RehearsalFailed("Stage results disagree with the verified source")
    if db_file.exists():
        raise RehearsalFailed("Temporary rehearsal database was not removed")

    return {
        "source_sha256": digest,
        "songs_staged_and_verified": len(expected_songs),
        "lineups_staged_and_verified": len(expected_lineups),
        "undated_draft_lineups": prepared["undated_lineups"],
        "demo_lineups_excluded": demos,
        "member_visibility": "none",
        "published_records": 0,
        "normalized_domain_records_match": True,
        "personal_preferences_migrated_and_verified": True,
        "personal_display_keys_migrated": len(expected_preferences["keys"]),
        "complete_browser_backup_archived_and_verified": True,
        "archive_contains_excluded_demo_and_raw_storage": True,
        "browser_specific_fields_migrated": False,
        "lossless_production_migration": False,
        "temporary_database_removed": True,
        "wix_access": False,
    }


def rehearse_v19_backup_file(file: str | Path) -> dict:
    """Read a private file locally. No write to the backup or existing SQLite."""
    file = Path(file)
    with file.open("rb") as stream:
        original = stream.read(MAX_BYTES + 1)
    if len(original) > MAX_BYTES:
        raise ValueError("Backup exceeds the size limit")
    before_hash = hashlib.sha256(original).hexdigest()
    envelope = json.loads(
        original.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_fields
    )
    report = rehearse_v19_envelope(envelope)

    # Detect concurrent mutation of the source file; the rehearsal never writes.
    with file.open("rb") as stream:
        after_hash = hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest()
    if before_hash != after_hash:
        raise RehearsalFailed("Backup changed during rehearsal; results discarded")

    report["original_file_sha256"] = before_hash
    report["original_backup_unchanged"] = True
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True,
                        help="Local V19 JSON backup (never uploaded)")
    args = parser.parse_args()
    try:
        result = rehearse_v19_backup_file(args.backup)
    except (OSError, ValueError, TypeError, RehearsalFailed) as exc:
        parser.exit(1, "Rehearsal failed or was blocked: " + str(exc) + "\n")
    print(json.dumps(result, indent=2))
