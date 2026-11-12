"""Synthetic-only tests of disposable V19 import rehearsal."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase3_migration_rehearsal import (
    rehearse_v19_backup_file,
    rehearse_v19_envelope,
)
from shared_store import InvalidRecord


def _song(song_id="fixture-hymn"):
    return {
        "id": song_id, "title": "Synthetic original", "key": "G",
        "sections": [{"label": "Verse", "lines": ["[G]Test-only melody", "[D]Second line"]}],
        "localChart": True,
    }


def _lineup(lineup_id="fixture-service", *, date="", song_id="fixture-hymn"):
    return {
        "id": lineup_id, "title": "Unannounced fixture", "date": date,
        "notes": "PRIVATE fixture note must not appear in logs",
        "songs": [{"id": song_id, "key": "A"}],
        "demo": False, "updated": 123,
    }


def _demo():
    return {
        "id": "demo-sunday", "demo": True, "title": "Sunday worship",
        "date": "", "notes": "Bundled example", "songs": [
            {"id": "fixture-bundled-demo", "key": "G"},
        ],
        "updated": 0,
    }


def backup(*, extra=None, lineups=None, arrangements=None):
    workshop = {
        "songs": [_song()], "arrangements": {} if arrangements is None else arrangements,
        "deletedSongIds": [],
    }
    hub = {
        "lineups": [_demo(), _lineup()] if lineups is None else lineups,
        "draft": {"id": None, "title": "", "date": "", "notes": "", "songs": []},
        "keys": {"fixture-hymn": "G"}, "font": 21,
    }
    records = [
        {"key": "wcfi-song-workshop-v1", "value": json.dumps(workshop)},
        {"key": "wcfi-music-hub-v2", "value": json.dumps(hub)},
    ]
    if extra:
        records.extend(extra)
    return {
        "format": "wcfi-music-local-backup", "version": 1,
        "scope": "browser-local-only", "exportedAt": "2026-11-11T12:00:00.000Z",
        "records": records,
    }


class DisposableRehearsalTests(unittest.TestCase):
    def test_real_draft_round_trip_no_member_publication(self):
        doc = backup()
        original = copy.deepcopy(doc)
        result = rehearse_v19_envelope(doc)
        self.assertEqual(doc, original)
        self.assertEqual(result["songs_staged_and_verified"], 1)
        self.assertEqual(result["lineups_staged_and_verified"], 1)
        self.assertEqual(result["undated_draft_lineups"], 1)
        self.assertEqual(result["demo_lineups_excluded"], 1)
        self.assertEqual(result["published_records"], 0)
        self.assertEqual(result["member_visibility"], "none")
        self.assertTrue(result["normalized_domain_records_match"])
        self.assertTrue(result["complete_browser_backup_archived_and_verified"])
        self.assertTrue(result["archive_contains_excluded_demo_and_raw_storage"])
        self.assertTrue(result["temporary_database_removed"])
        self.assertFalse(result["lossless_production_migration"])
        self.assertFalse(result["browser_specific_fields_migrated"])
        self.assertFalse(result["wix_access"])
        self.assertNotIn("PRIVATE fixture note", str(result))
        self.assertNotIn("Test-only melody", str(result))
        self.assertNotIn("Unannounced fixture", str(result))

    def test_local_file_is_unchanged_and_no_output_file_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "private-fixture-backup.json"
            data = b"\xef\xbb\xbf" + json.dumps(backup(), ensure_ascii=False).encode("utf-8")
            source.write_bytes(data)
            before = set(folder.iterdir())
            expected_hash = hashlib.sha256(data).hexdigest()
            result = rehearse_v19_backup_file(source)
            self.assertEqual(set(folder.iterdir()), before)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(result["original_file_sha256"], expected_hash)
            self.assertTrue(result["original_backup_unchanged"])
            self.assertTrue(result["temporary_database_removed"])

    def test_unsupported_arrangements_cause_no_rehearsal(self):
        doc = backup(arrangements={"fixture-service": {"fixture-hymn": {"flow": ["Verse"]}}})
        with self.assertRaises(InvalidRecord):
            rehearse_v19_envelope(doc)

    def test_unknown_demo_and_unresolved_refs_fail_closed(self):
        for doc in (
            backup(lineups=[{"id": "unknown-demo", "demo": True, "title": "Other",
                             "date": "", "notes": "", "songs": []}, _lineup()]),
            backup(lineups=[_demo(), _lineup(song_id="not-exported")]),
        ):
            with self.subTest(), self.assertRaises(InvalidRecord):
                rehearse_v19_envelope(doc)

    def test_unfinished_editor_draft_is_not_discarded(self):
        doc = backup(extra=[{
            "key": "wcfi-song-editor-draft-v1:fixture-hymn", "value": '{"incomplete":',
        }])
        with self.assertRaises(InvalidRecord):
            rehearse_v19_envelope(doc)

    def test_duplicate_json_fields_are_rejected_from_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "duplicate.json"
            path.write_text(
                '{"format":"wcfi-music-local-backup","format":"wcfi-music-local-backup"}',
                encoding="utf-8",
            )
            with self.assertRaises(InvalidRecord):
                rehearse_v19_backup_file(path)

    def test_rehearsal_never_opens_an_http_listener(self):
        src = (Path(__file__).parents[1] / "phase3_migration_rehearsal.py").read_text()
        self.assertNotIn("http.server", src)
        self.assertNotIn("create_server(", src)
        self.assertNotIn("requests.post(", src)
        self.assertNotIn("wix.com", src)


if __name__ == "__main__":
    unittest.main()
