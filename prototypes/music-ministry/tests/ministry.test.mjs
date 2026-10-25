import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateMinistry,registerMinistrySongs,safeUrl} from '../dist/ministry.mjs';
import {songs} from '../dist/songs.mjs';
import {loadData,saveLineup,keysFor,songFor} from '../dist/lineups.mjs';
const fixture=()=>({schemaVersion:1,featuredLineupId:'synthetic-service',songs:[{id:'synthetic-song',title:'Fixture Song',category:'Test'}],lineups:[{id:'synthetic-service',songs:[{id:'synthetic-song',key:null}],closing:{id:'synthetic-song',key:null}}],resources:[],events:[],unannounced:[]});
test('private snapshots reject broken song references and missing featured plans',()=>{
  assert.equal(validateMinistry(fixture()).lineups[0].songs[0].key,null);
  const a=fixture();a.lineups[0].songs[0].id='missing';assert.throws(()=>validateMinistry(a));
  const b=fixture();b.featuredLineupId='missing';assert.throws(()=>validateMinistry(b));
  const c=fixture();c.songs.push(c.songs[0]);assert.throws(()=>validateMinistry(c));
});
test('metadata songs preserve unknown keys across save/reload without changing the imported plan',()=>{
  const snapshot=fixture(),before=structuredClone(snapshot);registerMinistrySongs(snapshot,songs);
  try{const state=loadData(null,null);state.draft={id:null,title:'Personal fixture',date:'',notes:'',songs:structuredClone(snapshot.lineups[0].songs)};
    saveLineup(state,'personal',1);const restored=loadData(JSON.parse(JSON.stringify(state)));
    assert.equal(restored.lineups[0].songs[0].key,null);assert.deepEqual(snapshot,before);assert(!restored.lineups.some(l=>l.id===snapshot.featuredLineupId));
    assert(keysFor(songFor('synthetic-song')).includes('Cm'));assert.throws(()=>registerMinistrySongs(snapshot,songs),/collision/);
  }finally{songs.splice(songs.findIndex(s=>s.id==='synthetic-song'),1);}
});
test('resource URLs allow HTTPS only',()=>{assert.equal(safeUrl('javascript:alert(1)'),null);assert.equal(safeUrl('file:///private'),null);assert.equal(safeUrl('https://example.com/reference'),'https://example.com/reference');});

test('supplied charts retain their authored key and sections in the registry',()=>{
 const snapshot=fixture();Object.assign(snapshot.songs[0],{key:'E',artist:'Fixture artist',sections:[{label:'Verse',lines:['[E]Synthetic words']}],sequence:'Verse'});
 const registry=[];registerMinistrySongs(snapshot,registry);
 assert.equal(registry[0].metadataOnly,false);assert.equal(registry[0].localChart,true);assert.equal(registry[0].key,'E');assert.deepEqual(registry[0].sections,snapshot.songs[0].sections);
 assert.equal(snapshot.lineups[0].songs[0].key,null);
});
