"""Synthetic-only Phase-3 tests. No browser, network, real accounts or Wix access."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_store import InvalidRecord, MusicStore, NotAuthenticated, NotAuthorized, VersionConflict


def chart(song_id='song-a'):
    return {'id': song_id, 'title': 'Synthetic original', 'key': 'G', 'category': 'Test',
            'sections': [{'label': 'Verse 1', 'lines': ['[G]Original synthetic words', '[D]Next line']}]}


def lineup(lineup_id='service-a', song_id='song-a'):
    return {'id': lineup_id, 'title': 'Synthetic Sunday', 'date': '2026-11-15',
            'songs': [{'id': song_id, 'key': 'A', 'arrangement': ['Verse 1']}], 'notes': 'Synthetic note'}


class SharedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'isolated-synthetic.sqlite'
        self.db = MusicStore(self.path)
        self.db.initialize()
        # Test-only initial administrator fixture. There is deliberately no
        # public bootstrap endpoint or self-service admin creation API.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO members VALUES(?,?,?,?)", ('admin-subject', 1, '["admin"]', 'fixture'))
        self.db.upsert_member('admin-subject', 'viewer', {'member'}, True)
        self.db.upsert_member('admin-subject', 'editor', {'song_editor'}, True)
        self.db.upsert_member('admin-subject', 'leader', {'worship_leader'}, True)
        self.db.upsert_member('admin-subject', 'unapproved', {'member'}, False)

    def test_all_data_access_rejects_anonymous_unapproved_and_unknown(self):
        for subject in ('', 'unknown', 'unapproved'):
            with self.subTest(subject=subject), self.assertRaises(NotAuthenticated):
                self.db.list_songs(subject)
            with self.assertRaises(NotAuthenticated):
                self.db.list_lineups(subject)
            with self.assertRaises(NotAuthenticated):
                self.db.create_song(subject, chart())

    def test_authorization_roles_and_drafts(self):
        with self.assertRaises(NotAuthorized):
            self.db.create_song('viewer', chart())
        self.assertEqual(self.db.create_song('editor', chart()), 1)
        self.assertEqual(self.db.list_songs('viewer'), [])
        self.assertEqual(self.db.list_songs('leader'), [])
        self.assertEqual(len(self.db.list_songs('editor')), 1)
        self.assertEqual(self.db.list_songs('editor')[0]['state'], 'draft')
        self.assertEqual(len(self.db.list_songs('admin-subject')), 1)
        with self.assertRaises(NotAuthorized):
            self.db.approve_song('editor', 'song-a')
        self.db.approve_song('admin-subject', 'song-a')
        self.assertEqual(len(self.db.list_songs('viewer')), 1)
        with self.assertRaises(NotAuthorized):
            self.db.update_song_draft('editor', chart(), expected_revision=2)

    def test_version_checks_and_separate_instance_persistence(self):
        self.db.create_song('editor', chart())
        other = MusicStore(self.path)
        self.assertEqual(other.list_songs('editor')[0]['revision'], 1)
        modified = chart()
        modified['title'] = 'Changed from device two'
        self.assertEqual(other.update_song_draft('editor', modified, 1), 2)
        with self.assertRaises(VersionConflict):
            self.db.update_song_draft('editor', chart(), 1)
        self.assertEqual(self.db.list_songs('editor')[0]['title'], 'Changed from device two')

    def test_import_v1_atomic_and_preserve_sections(self):
        envelope = {'format': 'wcfi-song-catalogue', 'version': 1,
                    'songs': [chart('song-a'), chart('song-b')]}
        with self.assertRaises(NotAuthorized):
            self.db.import_catalogue_v1('viewer', envelope)
        self.assertEqual(self.db.import_catalogue_v1('editor', envelope), 2)
        self.assertEqual(self.db.list_songs('editor')[0]['sections'], chart()['sections'])
        with self.assertRaises(VersionConflict):
            self.db.import_catalogue_v1('editor', envelope)
        with self.assertRaises(InvalidRecord):
            self.db.import_catalogue_v1('editor', {'format': 'wcfi-song-catalogue', 'version': 1,
                                                     'songs': [chart('song-c'), dict(chart('bad'), title='') ]})
        self.assertEqual(len(self.db.list_songs('editor')), 2)

    def test_lineup_publish_requires_approved_songs(self):
        self.db.create_song('editor', chart())
        with self.assertRaises(NotAuthorized):
            self.db.create_lineup('editor', lineup())
        with self.assertRaises(InvalidRecord):
            self.db.create_lineup('leader', lineup())
        self.db.approve_song('admin-subject', 'song-a')
        self.db.create_lineup('leader', lineup())
        self.assertEqual(self.db.list_lineups('viewer'), [])
        self.assertEqual(self.db.list_lineups('leader')[0]['state'], 'draft')
        self.db.publish_lineup('admin-subject', 'service-a')
        self.assertEqual(self.db.list_lineups('viewer')[0]['state'], 'published')
        with self.assertRaises(NotAuthorized):
            self.db.update_lineup_draft('leader', lineup(), 2)

    def test_revocation_blocks_old_member_and_editor_sessions(self):
        self.db.create_song('editor', chart())
        self.db.upsert_member('admin-subject', 'editor', {'song_editor'}, False)
        for method in (lambda: self.db.list_songs('editor'), lambda: self.db.create_song('editor', chart('song-new'))):
            with self.assertRaises(NotAuthenticated):
                method()

    def test_invalid_data_bounded_and_excludes_private_keys(self):
        with self.assertRaises(InvalidRecord):
            self.db.create_song('editor', dict(chart(), sections=[]))
        with self.assertRaises(InvalidRecord):
            self.db.create_song('editor', dict(chart(), bpm=True))
        special = dict(chart(), privateNotes='never copy this into shared records')
        self.db.create_song('editor', special)
        self.assertNotIn('privateNotes', self.db.list_songs('editor')[0])

    def test_no_real_network_or_credentials_in_module(self):
        source = (Path(__file__).parents[1] / 'shared_store.py').read_text()
        self.assertNotIn('http.server', source)
        self.assertNotIn('jwt.decode', source)
        self.assertNotIn('localhost:', source)
        self.assertTrue(self.path.is_file())


if __name__ == '__main__':
    unittest.main()