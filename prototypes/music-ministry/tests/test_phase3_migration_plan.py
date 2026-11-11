"""Synthetic-only, read-only V19 migration compatibility tests."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phase3_migration_plan import inspect_v19_backup


def backup(workshop=None, hub=None, extra=None):
    records = [
        {'key': 'wcfi-song-workshop-v1', 'value': json.dumps(workshop if workshop is not None else {'songs': [], 'arrangements': {}})},
        {'key': 'wcfi-music-hub-v2', 'value': json.dumps(hub if hub is not None else {'lineups': []})},
    ]
    if extra:
        records.extend(extra)
    return {'format':'wcfi-music-local-backup','version':1,'scope':'browser-local-only',
            'exportedAt':'2026-11-11T12:00:00.000Z','records': records}


def song(song_id='local-test'):
    return {'id':song_id,'title':'Synthetic','key':'G','sections':[{'label':'Verse', 'lines':['[G]Synthetic words']}], 'privateNotes':'not included in plan'}


class MigrationInspectionTests(unittest.TestCase):
    def test_empty_workshop_reports_no_autonomous_migration(self):
        p = inspect_v19_backup(backup())
        self.assertEqual(p.song_candidates,0)
        self.assertFalse(p.can_auto_import)
        self.assertIn('migration:atomic_import_not_implemented',p.blockers)

    def test_valid_song_counts_and_no_private_content_in_report(self):
        p = inspect_v19_backup(backup({'songs':[song()],'arrangements':{}},{'lineups':[]}))
        self.assertEqual((p.song_candidates,p.song_ready,p.song_blocked),(1,1,0))
        self.assertNotIn('Synthetic words',str(p.summary()))
        self.assertNotIn('not included in plan',str(p.summary()))

    def test_undated_lineup_is_ready_for_draft_staging_but_not_publication(self):
        l={'id':'lineup-test','title':'Test','date':'','songs':[{'id':'local-test','key':'G'}]}
        p=inspect_v19_backup(backup({'songs':[song()],'arrangements':{}},{'lineups':[l]}))
        self.assertEqual((p.lineup_candidates,p.lineup_ready,p.lineup_blocked),(1,1,0))
        self.assertEqual(p.undated_draft_lineups,1)
        self.assertIn('lineups:publication_date_required',p.publication_holds)
        self.assertNotIn('lineups:incompatible_lineup',p.blockers)
        self.assertFalse(p.can_auto_import)

    def test_invalid_calendar_date_still_blocks_lineup_staging(self):
        invalid = {'id':'lineup-test','title':'Fixture','date':'2026-99-99',
                   'songs':[{'id':'local-test','key':'G'}]}
        p = inspect_v19_backup(backup({'songs':[song()], 'arrangements':{}},
                                     {'lineups':[invalid]}))
        self.assertEqual((p.lineup_ready,p.lineup_blocked), (0,1))
        self.assertIn('lineups:invalid_date', p.blockers)
        self.assertIn('lineups:incompatible_lineup', p.blockers)

    def test_missing_date_and_unresolved_reference_are_both_reported(self):
        item = {'id':'lineup-test','title':'Fixture','date':'',
                'songs':[{'id':'demo-unavailable','key':'G'}]}
        p = inspect_v19_backup(backup({'songs':[song()],'arrangements':{}},
                                     {'lineups':[item]}))
        self.assertIn('lineups:unresolved_song_reference', p.blockers)
        self.assertNotIn('lineups:incompatible_lineup', p.blockers)
        self.assertEqual((p.lineup_ready, p.lineup_blocked), (0,1))

    def test_performance_key_issue_uses_only_category_without_private_song_id(self):
        item = {'id':'lineup-test','title':'Secret title','date':'2026-11-15',
                'songs':[{'id':'local-test','key':'Hmaj7'}], 'notes':'SECRET NOTE'}
        p = inspect_v19_backup(backup({'songs':[song()],'arrangements':{}},
                                     {'lineups':[item]}))
        self.assertIn('lineups:invalid_performance_key', p.blockers)
        report = str(p.summary())
        for secret in ('Secret title', 'SECRET NOTE', 'Hmaj7', 'local-test'):
            self.assertNotIn(secret, report)

    def test_known_demo_is_excluded_and_counted(self):
        demo={'id':'demo-sunday','demo':True,'title':'Sunday worship',
              'date':'','notes':'example','songs':[{'id':'demo-only','key':'G'}]}
        valid={'id':'lineup-test','title':'Test','date':'',
               'songs':[{'id':'local-test','key':'G'}]}
        p=inspect_v19_backup(backup({'songs':[song()],'arrangements':{}},
                                    {'lineups':[demo,valid]}))
        self.assertEqual((p.lineup_candidates,p.lineup_ready,p.lineup_blocked),(1,1,0))
        self.assertEqual(p.demo_lineups_excluded,1)
        self.assertEqual(p.undated_draft_lineups,1)
        self.assertFalse(p.can_auto_import)

    def test_unrecognized_demo_is_not_silently_ignored(self):
        item={'id':'unknown-demo','demo':True,'title':'Maybe personal',
              'date':'','notes':'keep','songs':[]}
        p=inspect_v19_backup(backup(hub={'lineups':[item]}))
        self.assertEqual((p.lineup_candidates,p.lineup_ready,p.lineup_blocked),(1,0,1))
        self.assertEqual(p.demo_lineups_excluded,0)
        self.assertIn('lineups:unrecognized_demo',p.blockers)

    def test_external_reference_and_arrangement_are_explicitly_blocked(self):
        l={'id':'lineup-test','title':'Test','date':'2026-11-15','songs':[{'id':'not-present','key':'G'}]}
        p=inspect_v19_backup(backup({'songs':[song()],'arrangements':{'lineup-test':{'local-test':{'key':'G','flow':['Verse']}}}},{'lineups':[l]}))
        self.assertIn('lineups:unresolved_song_reference',p.blockers)
        self.assertIn('arrangements:shared_schema_not_yet_supported',p.blockers)

    def test_unfinished_draft_raw_invalid_json_is_counted_not_discarded(self):
        p=inspect_v19_backup(backup(extra=[{'key':'wcfi-song-editor-draft-v1:local-test','value':'{"broken":'}]))
        self.assertEqual(p.unfinished_editor_drafts,1)
        self.assertIn('drafts:unfinished_editor_state_requires_separate_migration',p.blockers)

    def test_duplicate_and_foreign_storage_keys_fail_before_report(self):
        doc=backup();doc['records'].append(doc['records'][0])
        with self.assertRaises(ValueError):inspect_v19_backup(doc)
        doc=backup();doc['records'].append({'key':'oauth_access_token','value':'secret'})
        with self.assertRaises(ValueError):inspect_v19_backup(doc)

    def test_no_backup_write_or_user_content_in_report(self):
        doc=backup({'songs':[song()],'arrangements':{}})
        original=json.dumps(doc,sort_keys=True)
        inspect_v19_backup(doc)
        self.assertEqual(json.dumps(doc,sort_keys=True),original)


if __name__=='__main__':
    unittest.main()