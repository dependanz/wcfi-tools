import {test} from 'node:test';
import assert from 'node:assert/strict';
import {loadData,saveLineup,initialData,cleanSongs} from '../dist/lineups.mjs';

test('prior browser set is copied into a draft without mutating legacy data',()=>{
  const old={set:[{id:'still-waters',key:'Cm'}],keys:{'morning-mercy':'D'},font:24};
  const before=structuredClone(old),data=loadData(null,old);
  data.draft.songs[0].key='Dm';assert.deepEqual(old,before);
  assert.equal(data.keys['morning-mercy'],'D');assert.equal(data.lineups[0].demo,true);
});
test('save validates name and song presence, then updates exactly one line-up',()=>{
  const data=initialData();assert.throws(()=>saveLineup(data,'x',1),/name/);
  data.draft.title='Sunday';assert.throws(()=>saveLineup(data,'x',1),/song/);
  data.draft.songs=[{id:'morning-mercy',key:'Bb'}];
  const saved=saveLineup(data,'x',1);assert.equal(saved.songs[0].key,'Bb');assert.equal(data.lineups.length,2);
  data.draft=structuredClone(saved);data.draft.title='Updated';saveLineup(data,'new-id',2);
  assert.equal(data.lineups.length,2);assert.equal(data.lineups[0].id,'x');assert.equal(data.lineups[0].title,'Updated');
  assert.equal(data.draft.songs.length,0);
});
test('personal display key does not change saved performance keys',()=>{
  const data=initialData();data.keys['morning-mercy']='Db';
  assert.equal(data.lineups[0].songs.find(x=>x.id==='morning-mercy').key,'G');
});
test('invalid songs, duplicate choices and incompatible minor keys are rejected',()=>{
  assert.deepEqual(cleanSongs([{id:'bogus',key:'C'},{id:'still-waters',key:'C'},{id:'still-waters',key:'Cm'}]),[]);
  assert.deepEqual(cleanSongs([{id:'morning-mercy',key:'G'},{id:'morning-mercy',key:'D'}]),[{id:'morning-mercy',key:'G'}]);
  const state=loadData({lineups:[null,{}, {id:'x',title:'Test',songs:[{id:'morning-mercy',key:'G'}]}],draft:null});
  assert.equal(state.lineups.length,1);assert.equal(state.draft.id,null);
});
