"""Private member preference mapping and isolated V19 staging tests."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase3_migration_stage import prepare_v19_draft_stage, stage_v19_drafts
from shared_store import (
    InvalidRecord,
    MusicStore,
    NotAuthenticated,
    VersionConflict,
    extract_v19_member_preferences,
    validate_member_preferences,
)


def _synthetic_backup():
    chart = {
        "id": "fixture-song", "title": "Original synthetic song", "key": "G",
        "sections": [{"label": "Verse", "lines": ["[G]Test-only words"]}],
        "localChart": True,
    }
    lineup = {
        "id": "fixture-plan", "title": "Synthetic Sunday", "date": "",
        "notes": "Private test note", "songs": [{"id": "fixture-song", "key": "A"}],
        "demo": False,
    }
    demo = {
        "id": "demo-sunday", "title": "Sunday worship", "date": "",
        "notes": "Bundled example", "songs": [], "demo": True,
    }
    rows = [
        {"key": "wcfi-song-workshop-v1",
         "value": json.dumps({"songs": [chart], "arrangements": {},
                              "deletedSongIds": []})},
        {"key": "wcfi-music-hub-v2",
         "value": json.dumps({"lineups": [demo, lineup], "draft": {
             "title": "", "date": "", "notes": "", "songs": []},
             "keys": {"fixture-song": "C", "external-snapshot": "F#m"},
             "font": 23, "lyrics": True})},
    ]
    return {
        "format": "wcfi-music-local-backup", "version": 1,
        "scope": "browser-local-only", "exportedAt": "2026-11-11T00:00:00.000Z",
        "records": rows,
    }


class MemberPreferencesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MusicStore(Path(temp.name) / "synthetic-only.sqlite")
        self.store.initialize()
        with closing(sqlite3.connect(self.store.file)) as conn, conn:
            conn.execute(
                "INSERT INTO members(subject,approved,roles,updated_at) VALUES(?,?,?,?)",
                ("synthetic-admin", 1, '["admin"]', "fixture"),
            )
        self.store.upsert_member("synthetic-admin", "synthetic-one", {"member"}, True)
        self.store.upsert_member("synthetic-admin", "synthetic-two", {"member"}, True)
        self.store.upsert_member("synthetic-admin", "synthetic-pending", {"member"}, False)

    def test_defaults_then_first_update_is_private(self):
        default = {"keys": {}, "font": 21, "lyrics": False, "revision": 0}
        self.assertEqual(self.store.get_member_preferences("synthetic-one"), default)
        prefs = {"keys": {"fixture-song": "Gm"}, "font": 25, "lyrics": True}
        self.assertEqual(self.store.update_member_preferences("synthetic-one", prefs, 0), 1)
        self.assertEqual(self.store.get_member_preferences("synthetic-one"), {
            **prefs, "revision": 1,
        })
        self.assertEqual(self.store.get_member_preferences("synthetic-two"), default)
        self.assertEqual(self.store.get_member_preferences("synthetic-admin"), default)

    def test_concurrent_edits_cannot_silently_overwrite(self):
        prefs = {"keys": {}, "font": 20, "lyrics": False}
        self.store.update_member_preferences("synthetic-one", prefs, 0)
        other = MusicStore(self.store.file)
        self.assertEqual(other.get_member_preferences("synthetic-one")["revision"], 1)
        self.assertEqual(other.update_member_preferences("synthetic-one", {
            **prefs, "font": 24,
        }, 1), 2)
        with self.assertRaises(VersionConflict):
            self.store.update_member_preferences("synthetic-one", prefs, 1)
        with self.assertRaises(VersionConflict):
            self.store.update_member_preferences("synthetic-one", prefs, 0)
        self.assertEqual(self.store.get_member_preferences("synthetic-one")["font"], 24)

    def test_invalid_values_cannot_be_normalized_silently(self):
        good = {"keys": {}, "font": 21, "lyrics": False}
        for invalid in (
            {**good, "font": True}, {**good, "font": 17},
            {**good, "lyrics": "true"}, {**good, "keys": {"id": "Hmaj7"}},
            {**good, "keys": {"id": None}}, {**good, "other": 1},
            {**good, "keys": {"a\nb": "G"}},
        ):
            with self.subTest(), self.assertRaises(InvalidRecord):
                validate_member_preferences(invalid)
        with self.assertRaises(InvalidRecord):
            self.store.update_member_preferences("synthetic-one", good, -1)
        self.assertEqual(self.store.get_member_preferences("synthetic-one")["revision"], 0)

    def test_revoked_member_cannot_read_or_change_preferences(self):
        self.store.upsert_member("synthetic-admin", "synthetic-one", {"member"}, False)
        with self.assertRaises(NotAuthenticated):
            self.store.get_member_preferences("synthetic-one")
        with self.assertRaises(NotAuthenticated):
            self.store.update_member_preferences(
                "synthetic-one", {"keys": {}, "font": 21, "lyrics": False}, 0,
            )

    def test_v19_mapping_preserves_external_display_key_without_claiming_song_approval(self):
        prefs = extract_v19_member_preferences({
            "font": 23, "lyrics": True,
            "keys": {"fixture-song": "C", "external-snapshot": "F#m"},
            "lineups": [{"id": "not-a-preference"}],
        })
        self.assertEqual(prefs, {
            "font": 23, "lyrics": True,
            "keys": {"fixture-song": "C", "external-snapshot": "F#m"},
        })

    def test_stage_maps_preferences_in_same_transaction_and_keeps_private(self):
        source = _synthetic_backup()
        preflight = prepare_v19_draft_stage(source)
        self.assertTrue(preflight["personal_preferences_ready"])
        self.assertEqual(preflight["personal_display_key_count"], 2)
        staged = stage_v19_drafts(
            self.store, "synthetic-admin", source,
            reviewed_sha256=preflight["source_sha256"], confirmed=True,
        )
        self.assertTrue(staged["personal_preferences_staged"])
        self.assertEqual(staged["personal_display_key_count"], 2)
        self.assertEqual(self.store.get_member_preferences("synthetic-admin"), {
            "keys": {"fixture-song": "C", "external-snapshot": "F#m"},
            "font": 23, "lyrics": True, "revision": 1,
        })
        self.assertEqual(self.store.get_member_preferences("synthetic-one"), {
            "keys": {}, "font": 21, "lyrics": False, "revision": 0,
        })
        self.assertEqual(self.store.list_songs("synthetic-one"), [])
        self.assertEqual(self.store.list_lineups("synthetic-one"), [])

    def test_existing_member_preferences_conflict_rolls_back_archive_and_songs(self):
        self.store.update_member_preferences(
            "synthetic-admin", {"keys": {}, "font": 22, "lyrics": False}, 0,
        )
        source = _synthetic_backup()
        preview = prepare_v19_draft_stage(source)
        with self.assertRaises(VersionConflict):
            stage_v19_drafts(
                self.store, "synthetic-admin", source,
                reviewed_sha256=preview["source_sha256"], confirmed=True,
            )
        with self.store._connection() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM migration_source_archives"
            ).fetchone()
            songs = db.execute("SELECT COUNT(*) FROM songs").fetchone()
            lineups = db.execute("SELECT COUNT(*) FROM lineups").fetchone()
        self.assertEqual((row[0], songs[0], lineups[0]), (0, 0, 0))
        self.assertEqual(self.store.get_member_preferences("synthetic-admin")["font"], 22)

    def test_bad_preferences_fail_before_any_staging(self):
        source = _synthetic_backup()
        data = json.loads(source["records"][1]["value"])
        data["font"] = "giant"
        source["records"][1]["value"] = json.dumps(data)
        with self.assertRaises(InvalidRecord):
            prepare_v19_draft_stage(source)
        self.assertEqual(self.store.list_songs("synthetic-admin"), [])


if __name__ == "__main__":
    unittest.main()
