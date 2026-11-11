"""Synthetic-only tests for explicitly confirmed atomic draft staging."""
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
from shared_store import InvalidRecord, MusicStore, NotAuthenticated, NotAuthorized, VersionConflict


def song(song_id="local-one"):
    return {
        "id": song_id, "title": "Synthetic hymn", "key": "G",
        "sections": [{"label": "Verse", "lines": ["[G]Synthetic song content"]}],
        "localChart": True,
    }


def lineup(lineup_id="local-service", song_id="local-one", date=""):
    return {
        "id": lineup_id, "title": "Synthetic planning draft", "date": date,
        "songs": [{"id": song_id, "key": "A"}], "notes": "Synthetic private note",
        "updated": 123, "demo": False,
    }


def backup(songs=None, lineups=None, *, arrangements=None, draft=None, extras=None):
    workshop = {
        "songs": [song()] if songs is None else songs,
        "arrangements": {} if arrangements is None else arrangements,
        "deletedSongIds": [],
    }
    hub = {
        "lineups": [lineup()] if lineups is None else lineups,
        "draft": {"id": None, "title": "", "date": "", "notes": "", "songs": []}
                 if draft is None else draft,
        "font": 21,
    }
    records = [
        {"key": "wcfi-song-workshop-v1", "value": json.dumps(workshop)},
        {"key": "wcfi-music-hub-v2", "value": json.dumps(hub)},
    ]
    if extras:
        records.extend(extras)
    return {
        "format": "wcfi-music-local-backup", "version": 1,
        "scope": "browser-local-only", "exportedAt": "2026-11-11T00:00:00.000Z",
        "records": records,
    }


class MigrationStageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MusicStore(Path(tmp.name) / "synthetic-stage.sqlite")
        self.store.initialize()
        with closing(sqlite3.connect(self.store.file)) as conn, conn:
            conn.execute("INSERT INTO members VALUES(?,?,?,?)", ("fixture-admin", 1, '["admin"]', "fixture"))
        self.store.upsert_member("fixture-admin", "fixture-viewer", {"member"}, True)
        self.store.upsert_member("fixture-admin", "fixture-editor", {"song_editor"}, True)
        self.store.upsert_member("fixture-admin", "fixture-leader", {"worship_leader"}, True)
        self.store.upsert_member("fixture-admin", "fixture-pending", {"member"}, False)

    def _stage(self, doc, *, principal="fixture-admin", confirmed=True, digest=None):
        prepared = prepare_v19_draft_stage(doc)
        return stage_v19_drafts(
            self.store, principal, doc,
            reviewed_sha256=prepared["source_sha256"] if digest is None else digest,
            confirmed=confirmed,
        )

    def test_preparation_read_only_summary_and_undated_draft(self):
        doc = backup()
        before = json.dumps(doc, sort_keys=True)
        result = prepare_v19_draft_stage(doc)
        self.assertEqual((result["songs_to_stage"], result["lineups_to_stage"]), (1, 1))
        self.assertEqual(result["undated_lineups"], 1)
        self.assertEqual(result["target_state"], "draft_only")
        self.assertEqual(result["approved_or_published"], 0)
        self.assertEqual(json.dumps(doc, sort_keys=True), before)
        self.assertEqual(self.store.list_songs("fixture-admin"), [])
        self.assertEqual(self.store.list_lineups("fixture-admin"), [])
        self.assertNotIn("Synthetic song content", str(result))
        self.assertNotIn("Synthetic private note", str(result))

    def test_atomic_stage_keeps_drafts_private_and_requires_date_to_publish(self):
        result = self._stage(backup())
        self.assertEqual((result["songs_staged"], result["lineups_staged"]), (1, 1))
        self.assertEqual(result["target_state"], "draft_only")
        saved_song = self.store.list_songs("fixture-admin")[0]
        saved_lineup = self.store.list_lineups("fixture-admin")[0]
        self.assertEqual(saved_song["state"], "draft")
        self.assertEqual(saved_song["sections"], song()["sections"])
        self.assertEqual((saved_lineup["date"], saved_lineup["state"]), ("", "draft"))
        self.assertEqual(saved_lineup["songs"][0]["key"], "A")
        self.assertEqual(saved_lineup["notes"], "Synthetic private note")
        self.assertEqual(self.store.list_songs("fixture-viewer"), [])
        self.assertEqual(self.store.list_lineups("fixture-viewer"), [])
        with self.assertRaises(InvalidRecord):
            self.store.publish_lineup("fixture-admin", "local-service")
        self.store.approve_song("fixture-admin", "local-one")
        with self.assertRaises(InvalidRecord):
            self.store.publish_lineup("fixture-admin", "local-service")
        revised = lineup(date="2026-11-15")
        self.assertEqual(self.store.update_lineup_draft("fixture-admin", revised, 1), 2)
        self.store.publish_lineup("fixture-admin", "local-service")
        published = self.store.list_lineups("fixture-viewer")[0]
        self.assertEqual((published["date"], published["state"]), ("2026-11-15", "published"))

    def test_explicit_approval_digest_and_roles_are_enforced(self):
        doc = backup()
        checked = prepare_v19_draft_stage(doc)["source_sha256"]
        with self.assertRaises(NotAuthorized):
            self._stage(doc, confirmed=False)
        with self.assertRaises(VersionConflict):
            self._stage(doc, digest="0" * 64)
        with self.assertRaises(NotAuthorized):
            self._stage(doc, principal="fixture-editor")
        with self.assertRaises(NotAuthenticated):
            self._stage(doc, principal="fixture-pending")
        self.assertEqual(self.store.list_songs("fixture-admin"), [])
        self.assertEqual(self.store.list_lineups("fixture-admin"), [])
        self.assertEqual(len(checked), 64)

    def test_conflicting_lineup_rolls_back_prior_song_insert(self):
        self.store.create_song("fixture-editor", song("other-song"))
        self.store.approve_song("fixture-admin", "other-song")
        self.store.create_lineup("fixture-leader", lineup("local-service", "other-song", "2026-11-15"))
        doc = backup()
        with self.assertRaises(VersionConflict):
            self._stage(doc)
        self.assertEqual([s["id"] for s in self.store.list_songs("fixture-admin")], ["other-song"])
        self.assertEqual([l["id"] for l in self.store.list_lineups("fixture-admin")], ["local-service"])

    def test_replay_of_staged_backup_fails_without_mutations(self):
        doc = backup()
        self._stage(doc)
        with self.assertRaises(VersionConflict):
            self._stage(doc)
        self.assertEqual(len(self.store.list_songs("fixture-admin")), 1)
        self.assertEqual(len(self.store.list_lineups("fixture-admin")), 1)

    def test_incomplete_browser_data_fails_closed(self):
        scenarios = [
            backup(arrangements={"local-service": {"local-one": {"flow": ["Verse"]}}}),
            backup(draft={"title": "Unfinished lineup", "songs": []}),
            backup(extras=[{"key": "wcfi-song-editor-draft-v1:local-one", "value": "{broken"}]),
            backup(extras=[{"key": "wcfi-song-editor-last", "value": '"local-one"'}]),
            backup(lineups=[lineup(song_id="demo-not-imported")]),
            backup(lineups=[lineup(date="2026-99-99")]),
        ]
        for doc in scenarios:
            with self.subTest(source_type=doc["records"][-1]["key"]):
                with self.assertRaises((InvalidRecord, ValueError, TypeError)):
                    prepare_v19_draft_stage(doc)
                self.assertEqual(self.store.list_songs("fixture-admin"), [])
                self.assertEqual(self.store.list_lineups("fixture-admin"), [])

    def test_no_http_route_or_external_account_changes(self):
        source = (Path(__file__).parents[1] / "phase3_migration_stage.py").read_text()
        self.assertNotIn("http.server", source)
        self.assertNotIn("wix.com", source)
        self.assertNotIn("requests.post(", source)
        self.assertNotIn("publish_lineup(", source)


if __name__ == "__main__":
    unittest.main()
