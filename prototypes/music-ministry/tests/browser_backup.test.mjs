import test from 'node:test';
import assert from 'node:assert/strict';
import {BACKUP_MAX_BYTES,captureLocalBackup,inspectLocalBackup,isMusicBackupKey,validateLocalBackup} from './browser_backup.mjs';

class FakeStorage {
  constructor(entries) {this.data = new Map(Object.entries(entries));}
  get length() {return this.data.size;}
  key(i) {return [...this.data.keys()][i] ?? null;}
  getItem(k) {return this.data.get(k) ?? null;}
  setItem() {throw new Error('Backup must never write browser storage');}
  removeItem() {throw new Error('Backup must never delete browser storage');}
}

const source = {
  'wcfi-music-preview-v1': '{"set":[{"id":"morning-mercy"}]}',
  'wcfi-music-hub-v2': '{"lineups":[{"id":"sunday","notes":"not published"}],"draft":{"title":"Unfinished"}}',
  'wcfi-song-workshop-v1': '{"songs":[{"id":"local-one","sections":[]}],"arrangements":{"sunday":{"local-one":{}}},"deletedSongIds":[]}',
  'wcfi-song-editor-draft-v1:local-one': '{"song":{"lines":["[H]invalid but recoverable"]},',
  'wcfi-song-editor-last': '"local-one"',
};

test('captures all V19 work verbatim without mutating localStorage', () => {
  const storage = new FakeStorage({...source, 'oauth_access_token':'SECRET', 'other-app':'not ours'});
  const result = captureLocalBackup(storage, '2026-11-11T00:00:00.000Z');
  const copy = JSON.parse(JSON.stringify(result));
  assert.equal(copy.format, 'wcfi-music-local-backup');
  assert.deepEqual(Object.fromEntries(copy.records.map(({key,value}) => [key,value])), source);
  assert.equal(storage.length, 7);
  assert.deepEqual(inspectLocalBackup(copy), {
    recordCount:5, editorDraftCount:1, hasSongWorkshop:true, hasLineups:true,
    includesLegacy:true, hasEditorResumePointer:true,
  });
});

test('captures empty browser state without inventing any songs or lineups', () => {
  const result = captureLocalBackup(new FakeStorage({}), '2026-11-11T00:00:00.000Z');
  assert.deepEqual(result.records, []);
});

test('restricts backup keys to the Music Ministry namespace', () => {
  for (const key of ['wcfi-song-editor-draft-v1:local-one','wcfi-music-hub-v2'])assert(isMusicBackupKey(key));
  for (const key of ['access_token', 'wcfi-session', 'wcfi-song-editor-draft-v1:', 'wcfi-song-editor-draft-v1:bad.key', 'wcfi-song-editor-draft-v1:../secret'])assert(!isMusicBackupKey(key));
});

test('rejects malformed, duplicated, foreign and overlarge backup records', () => {
  const good = captureLocalBackup(new FakeStorage(source));
  const bad = (...records) => ({...good,records});
  assert.throws(() => validateLocalBackup(bad(good.records[0],good.records[0])), /duplicate/i);
  assert.throws(() => validateLocalBackup(bad({key:'auth',value:'secret'})), /non-WCFI/i);
  assert.throws(() => validateLocalBackup(bad({key:'wcfi-music-hub-v2',value:null})), /Invalid/i);
  assert.throws(() => validateLocalBackup({...good,version:999}), /Invalid/i);
  assert.throws(() => captureLocalBackup(new FakeStorage({'wcfi-music-hub-v2':'x'.repeat(BACKUP_MAX_BYTES)})), /size limit/i);
});

test('aborts, rather than producing a partial backup, when storage fails during capture', () => {
  const storage = new FakeStorage(source);
  storage.getItem = () => { throw new Error('Storage denied'); };
  assert.throws(() => captureLocalBackup(storage), /Storage denied/);
  const volatile = new FakeStorage(source);
  volatile.getItem = () => null;
  assert.throws(() => captureLocalBackup(volatile), /changed during backup/);
});