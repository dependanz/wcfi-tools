"""Synthetic-only private source-archive tests. No real user records."""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase3_migration_stage import (
    prepare_v19_draft_stage,
    read_v19_source_archive,
    stage_v19_drafts,
)
from shared_store import InvalidRecord, MusicStore, NotAuthorized, VersionConflict


def test_backup():
    workshop = {
        "songs": [{
            "id": "fixture-song",
            "title": "Synthetic test hymn",
            "key": "G",
            "sections": [{"label": "Verse", "lines": ["[G]Original fixture words"]}],
            "localChart": True,
            "extraEditorLayout": {"panel": "large"},
        }],
        "arrangements": {},
        "deletedSongIds": [],
        "browserOnlyKey": {"keepThis": True},
    }
    hub = {
        "lineups": [
            {"id": "demo-sunday", "demo": True, "title": "Sunday worship",
             "date": "", "notes": "Never publish this synthetic demo",
             "songs": [{"id": "not-exported-demo", "key": "G"}]},
            {"id": "fixture-lineup", "demo": False, "title": "Private test plan",
             "date": "", "notes": "Private rehearsal note",
             "songs": [{"id": "fixture-song", "key": "A"}],
             "updated": 1730000000},
        ],
        "draft": {"id": None, "title": "", "date": "", "notes": "", "songs": []},
        "font": 23,
        "keys": {"fixture-song": "C"},
        "lyrics": True,
    }
    return {
        "format": "wcfi-music-local-backup",
        "version": 1,
        "scope": "browser-local-only",
        "exportedAt": "2026-11-11T12:00:00.000Z",
        "records": [
            {"key": "wcfi-song-workshop-v1", "value": json.dumps(workshop)},
            {"key": "wcfi-music-hub-v2", "value": json.dumps(hub)},
        ],
    }


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MusicStore(Path(temp.name) / "fixture-only.sqlite")
        self.store.initialize()
        with closing(sqlite3.connect(self.store.file)) as db, db:
            db.execute(
                "INSERT INTO members(subject,approved,roles,updated_at) VALUES(?,?,?,?)",
                ("fixture-admin", 1, '["admin"]', "synthetic"),
            )
        self.store.upsert_member("fixture-admin", "fixture-member", {"member"}, True)
        self.store.upsert_member("fixture-admin", "fixture-pending", {"member"}, False)

    def _stage(self, doc):
        digest = prepare_v19_draft_stage(doc)["source_sha256"]
        result = stage_v19_drafts(
            self.store, "fixture-admin", doc,
            reviewed_sha256=digest, confirmed=True,
        )
        return digest, result

    def test_exact_logical_backup_and_raw_storage_strings_are_recoverable(self):
        doc = test_backup()
        unchanged = copy.deepcopy(doc)
        digest, result = self._stage(doc)
        self.assertTrue(result["source_backup_archived"])
        self.assertTrue(result["personal_preferences_staged"])
        self.assertEqual(result["personal_display_key_count"], 1)
        self.assertEqual(self.store.get_member_preferences("fixture-admin"), {
            "keys": {"fixture-song": "C"},
            "font": 23, "lyrics": True, "revision": 1,
        })
        self.assertEqual(self.store.get_member_preferences("fixture-member"), {
            "keys": {}, "font": 21, "lyrics": False, "revision": 0,
        })
        self.assertEqual(doc, unchanged)
        restored = read_v19_source_archive(self.store, "fixture-admin", digest)
        self.assertEqual(restored, unchanged)
        self.assertEqual(restored["records"], doc["records"])
        hub = json.loads(restored["records"][1]["value"])
        self.assertEqual(hub["font"], 23)
        self.assertTrue(hub["lyrics"])
        self.assertEqual(hub["lineups"][0]["id"], "demo-sunday")
        self.assertEqual(hub["lineups"][1]["updated"], 1730000000)
        self.assertEqual(hub["lineups"][1]["notes"], "Private rehearsal note")
        self.assertEqual(self.store.list_lineups("fixture-admin")[0]["state"], "draft")
        self.assertEqual(self.store.list_lineups("fixture-member"), [])

    def test_only_admin_can_read_private_archive(self):
        digest, _ = self._stage(test_backup())
        with self.assertRaises(NotAuthorized):
            read_v19_source_archive(self.store, "fixture-member", digest)
        with self.assertRaises(InvalidRecord):
            read_v19_source_archive(self.store, "fixture-admin", "0" * 64)

    def test_archive_insert_is_rolled_back_when_lineup_collides(self):
        doc = test_backup()
        self.store.create_song("fixture-admin", {
            "id": "prior-song",
            "title": "Prior synthetic chart",
            "key": "G",
            "sections": [{"label": "Verse", "lines": ["Test-only words"]}],
        })
        # Create directly in fixture SQLite: application logic rightly refuses
        # lineups referring to unapproved chart records.
        with self.store._connection() as db:
            db.execute(
                "INSERT INTO lineups VALUES(?,?,?,?,?,?)",
                ("fixture-lineup", '{"id":"fixture-lineup"}', "draft",
                 "fixture-admin", 1, "fixture"),
            )
        digest = prepare_v19_draft_stage(doc)["source_sha256"]
        with self.assertRaises(VersionConflict):
            stage_v19_drafts(
                self.store, "fixture-admin", doc,
                reviewed_sha256=digest, confirmed=True,
            )
        with self.store._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM migration_source_archives").fetchone()[0]
            inserted = db.execute("SELECT COUNT(*) FROM songs WHERE id='fixture-song'").fetchone()[0]
        self.assertEqual((count, inserted), (0, 0))

    def test_tampering_with_archived_bytes_is_detected(self):
        digest, _ = self._stage(test_backup())
        with self.store._connection() as db:
            db.execute(
                "UPDATE migration_source_archives SET canonical_backup=? "
                "WHERE source_sha256=?",
                (sqlite3.Binary(b'{"corrupt":true}'), digest),
            )
        with self.assertRaises(InvalidRecord):
            read_v19_source_archive(self.store, "fixture-admin", digest)

    def test_replaying_backup_does_not_overwrite_source_archive(self):
        doc = test_backup()
        digest, _ = self._stage(doc)
        with self.assertRaises(VersionConflict):
            self._stage(doc)
        self.assertEqual(read_v19_source_archive(self.store, "fixture-admin", digest), doc)
        with self.store._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM migration_source_archives").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
