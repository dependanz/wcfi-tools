import {songs,demoSet} from './songs.mjs';
import {KEYS} from './chords.mjs';
export const songFor=id=>songs.find(s=>s.id===id);
export const keysFor=song=>song.metadataOnly?KEYS.flatMap(key=>[key,key+'m']):KEYS.map(key=>key+(song.key.endsWith('m')?'m':''));
export const cleanSongs=items=>Array.isArray(items)?items.filter((x,i,a)=>x&&songFor(x.id)&&((songFor(x.id).metadataOnly&&x.key===null)||keysFor(songFor(x.id)).includes(x.key))&&a.findIndex(y=>y?.id===x.id)===i).slice(0,60).map(x=>({id:x.id,key:x.key})):[];
export const blankDraft=()=>({id:null,title:'',date:'',notes:'',songs:[]});
export function cleanLineup(item){
  if(!item||typeof item!=='object')return null;
  return {id:typeof item.id==='string'?item.id.slice(0,100):null,title:String(item.title||'').slice(0,100),date:/^\d{4}-\d{2}-\d{2}$/.test(item.date||'')?item.date:'',notes:String(item.notes||'').slice(0,2000),songs:cleanSongs(item.songs),updated:Number.isFinite(item.updated)?item.updated:0,demo:item.demo===true};
}
export function initialData(legacy={}){
  return {lineups:[{id:'demo-sunday',title:'Sunday worship',date:'',notes:'An example flow: gather, respond, and carry the song into the week.',songs:structuredClone(demoSet),updated:0,demo:true}],draft:{...blankDraft(),songs:cleanSongs(legacy?.set)},keys:legacy?.keys&&typeof legacy.keys==='object'?legacy.keys:{},font:(Number.isInteger(legacy?.font)&&legacy.font>=18&&legacy.font<=28)?legacy.font:21,lyrics:legacy?.lyrics===true};
}
export function loadData(raw,legacy){
  const data=initialData(legacy);
  if(!raw||typeof raw!=='object')return data;
  if(Array.isArray(raw.lineups))data.lineups=raw.lineups.map(cleanLineup).filter(x=>x&&x.id&&x.title).slice(0,100);
  data.draft=cleanLineup(raw.draft)||blankDraft();
  for(const song of songs)if(keysFor(song).includes(raw.keys?.[song.id]))data.keys[song.id]=raw.keys[song.id];
  if((Number.isInteger(raw.font)&&raw.font>=18&&raw.font<=28))data.font=raw.font;
  data.lyrics=raw.lyrics===true;
  return data;
}
export function saveLineup(data,id,now){
  const draft=cleanLineup(data.draft);
  if(!draft.title.trim())throw new Error('Give your line-up a name first.');
  if(!draft.songs.length)throw new Error('Add at least one song to your line-up.');
  const result={...draft,id:draft.id||id,title:draft.title.trim(),updated:now,demo:false};
  data.lineups=[result,...data.lineups.filter(x=>x.id!==result.id)].slice(0,100);
  data.draft=blankDraft();
  return result;
}

// Portable song data only: no browser preferences, ministry source notes or line-ups.
export function songExport(items, now=new Date()) {
  return {format:'wcfi-song-catalogue',version:1,exportedAt:now.toISOString(),songs:items.map(song=>{
    const result={};
    for(const key of ['id','title','artist','key','category','bpm','meter','description','sequence'])if(song[key]!==undefined)result[key]=song[key];
    result.sections=song.sections.map(section=>({label:section.label,lines:[...section.lines]}));
    return result;
  })};
}
export function songExportFilename(title) {
  const name=String(title||'song').normalize('NFKD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-zA-Z0-9_-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,80)||'song';
  return name.toLowerCase()+'.wcfi-songs.json';
}
