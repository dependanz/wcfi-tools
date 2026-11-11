import test from 'node:test';
import assert from 'node:assert/strict';
import {captureLocalBackup} from '../dist/browser_backup.mjs';
import {planLocalRestore,restoreLocalBackupIntoEmptyStorage} from '../dist/browser_restore.mjs';

class FakeStorage {
  constructor(values={}) {this.values=new Map(Object.entries(values));this.writes=0;this.deletes=0;}
  get length() {return this.values.size;}
  key(i) {return [...this.values.keys()][i]??null;}
  getItem(k) {return this.values.get(k)??null;}
  setItem(k,v) {this.writes++;this.values.set(k,v);}
  removeItem(k) {this.deletes++;this.values.delete(k);}
}

const sample = {
  'wcfi-music-hub-v2':'{"lineups":[{"id":"local-synthetic","notes":"not published"}]}',
  'wcfi-song-workshop-v1':'{"songs":[{"id":"synthetic"}],"arrangements":{"set-1":{}}}',
  'wcfi-song-editor-draft-v1:local-test':'{"incomplete":', // preserve invalid editor state verbatim
  'wcfi-song-editor-last':'"local-test"',
  'wcfi-music-preview-v1':'{"legacy":true}',
};
const backup = () => captureLocalBackup(new FakeStorage(sample), '2026-11-11T00:00:00.000Z');

test('dry run reports source and conflicts without any browser writes',()=>{
  const store=new FakeStorage({'unrelated-app-key':'keep','wcfi-song-editor-draft-v1:existing':'current'});
  const original=[...store.values];
  const result=planLocalRestore(store,backup());
  assert.equal(result.safeToRestore,false);
  assert.deepEqual(result.existingKeys,['wcfi-song-editor-draft-v1:existing']);
  assert.equal(result.recordCount,5);
  assert.equal(result.editorDraftCount,1);
  assert.deepEqual([...store.values],original);
  assert.equal(store.writes,0);
  assert.equal(store.deletes,0);
});

test('restores every original local value exactly into empty WCFI namespace',()=>{
  const store=new FakeStorage({'other-app':'must remain'});
  assert.equal(planLocalRestore(store,backup()).safeToRestore,true);
  const result=restoreLocalBackupIntoEmptyStorage(store,backup());
  assert.deepEqual(result,{restoredRecords:5});
  for(const [key,value] of Object.entries(sample))assert.equal(store.getItem(key),value,key);
  assert.equal(store.getItem('other-app'),'must remain');
  assert.equal(store.deletes,0);
  assert.equal(planLocalRestore(store,backup()).safeToRestore,false);
});

test('rejects destination with existing work even if the backup contains different keys',()=>{
  const store=new FakeStorage({'wcfi-music-hub-v2':'existing local lineup'});
  assert.throws(()=>restoreLocalBackupIntoEmptyStorage(store,backup()),/already contains/i);
  assert.equal(store.getItem('wcfi-music-hub-v2'),'existing local lineup');
  assert.equal(store.writes,0);
});

test('rejects invalid and foreign-key backup before writing anything',()=>{
  const store=new FakeStorage();
  const bad={...backup(),records:[...backup().records,{key:'oauth_access_token',value:'secret'}]};
  assert.throws(()=>restoreLocalBackupIntoEmptyStorage(store,bad),/non-WCFI/i);
  assert.equal(store.writes,0);
});

test('one failed write rolls back all writes, including a write-then-throw',()=>{
  const store=new FakeStorage({'unrelated':'safe'});
  store.setItem=function(k,v){
    this.writes++;
    this.values.set(k,v);
    if(this.writes===3)throw new Error('Simulated quota failure after write');
  };
  assert.throws(()=>restoreLocalBackupIntoEmptyStorage(store,backup()),/rolled back/i);
  assert.deepEqual(Object.fromEntries(store.values),{unrelated:'safe'});
  assert.equal(store.deletes,3);
});

test('failed rollback is explicitly reported, never treated as success',()=>{
  const store=new FakeStorage();
  store.setItem=function(k,v){this.values.set(k,v);throw new Error('write then failure');};
  store.removeItem=function(){throw new Error('rollback denied');};
  assert.throws(()=>restoreLocalBackupIntoEmptyStorage(store,backup()),/rollback was incomplete/i);
});

test('storage unavailable and missing writable interface fail closed',()=>{
  assert.throws(()=>planLocalRestore({},backup()),/unavailable/i);
  const store=new FakeStorage();
  store.setItem=undefined;
  assert.throws(()=>restoreLocalBackupIntoEmptyStorage(store,backup()),/not writable/i);
});

test('empty backup cannot silently replace existing session',()=>{
  const empty=captureLocalBackup(new FakeStorage());
  const dest=new FakeStorage();
  assert.throws(()=>restoreLocalBackupIntoEmptyStorage(dest,empty),/contains no local records/i);
  assert.equal(dest.writes,0);
});
