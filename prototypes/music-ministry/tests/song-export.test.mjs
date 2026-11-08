import test from 'node:test';
import assert from 'node:assert/strict';
import {songExport,songExportFilename} from '../dist/lineups.mjs';
const song={id:'local-test',title:'Test / song',artist:'Example author',key:'G',category:'Worship',bpm:72,meter:'4/4',sequence:'Verse · Chorus',sections:[{label:'Verse',lines:['[(C#m)]Example [N.C.]','']},{label:'Chorus',lines:['[B7(b9)]Synthetic text']}],privateNotes:'exclude'};
test('export preserves authored song content and order with a versioned envelope',()=>{
 const result=JSON.parse(JSON.stringify(songExport([song,{...song,id:'local-second'}],new Date('2026-10-18T12:00:00Z'))));
 assert.equal(result.version,1);assert.equal(result.format,'wcfi-song-catalogue');assert.equal(result.exportedAt,'2026-10-18T12:00:00.000Z');
 assert.deepEqual(result.songs.map(s=>s.id),['local-test','local-second']);assert.deepEqual(result.songs[0].sections,song.sections);assert.equal(result.songs[0].artist,song.artist);assert.equal(result.songs[0].key,'G');assert(!('privateNotes' in result.songs[0]));
 result.songs[0].sections[0].lines.push('changed');assert.equal(song.sections[0].lines.length,2);
});
test('download filenames are safe and empty selections produce an empty export',()=>{
 assert.equal(songExportFilename('../Test / song?'),'test-song.wcfi-songs.json');assert.equal(songExportFilename(''),'song.wcfi-songs.json');assert.equal(songExportFilename('愛'),'song.wcfi-songs.json');assert.deepEqual(songExport([]).songs,[]);
});


test('export actions use full saved catalogue and block invalid single-song drafts',async()=>{
 const {readFileSync}=await import('node:fs');const {runInNewContext}=await import('node:vm');
 const source=readFileSync(new URL('../dist/hub.mjs',import.meta.url),'utf8');
 const handler=source.slice(source.indexOf('async function workshopAction('),source.indexOf(" if(action==='sx-reset-key')"))+'}';
 const downloads=[],notices=[];let invalid=false;
 const context={visibleSongs:()=>[song],downloadSongs:(items,title)=>downloads.push({items,title}),readSongForm:()=>{},validateSongEdit:()=>{if(invalid)throw Error('invalid');},applyValidSong:()=>true,songFor:()=>song,songEdit:song,showSongIssue:()=>{},notify:message=>notices.push(message)};
 runInNewContext(handler,context);
 await context.workshopAction({dataset:{action:'sx-export-all'}});assert.equal(downloads[0].items[0],song);
 await context.workshopAction({dataset:{action:'sx-export-song'}});assert.equal(downloads[1].items[0],song);
 invalid=true;await context.workshopAction({dataset:{action:'sx-export-song'}});assert.equal(downloads.length,2);assert(notices[0].includes('Fix'));
});
