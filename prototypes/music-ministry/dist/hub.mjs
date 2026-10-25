import {songs} from './songs.mjs';
import {loadMinistry,registerMinistrySongs,createMinistryViews} from './ministry.mjs';
const headerClock=document.getElementById('header-clock');
const clockFormat=new Intl.DateTimeFormat('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
function updateHeaderClock(){
  if(!headerClock)return;
  const now=new Date(),label=clockFormat.format(now);
  headerClock.textContent=label;
  headerClock.dateTime=now.toISOString();
  headerClock.setAttribute('aria-label',`Local time ${label}`);
}
updateHeaderClock();
setInterval(()=>{if(!document.hidden)updateHeaderClock();},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)updateHeaderClock();});
let ministry=null,ministryError=false;
try{ministry=await loadMinistry();registerMinistrySongs(ministry,songs);}catch{ministryError=true;}
import {transpose,tokens,pitch,isChord} from './chords.mjs';
import {songFor,keysFor,loadData,saveLineup,cleanSongs,songExport,songExportFilename} from './lineups.mjs';

const $=id=>document.getElementById(id);
const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const decode=s=>{try{return decodeURIComponent(s);}catch{return '';}};
const keyText=k=>k?escape(k).replaceAll('b','♭').replaceAll('#','♯'):'Unknown';
const paths={
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',back:'<path d="M20 12H4m6-6-6 6 6 6"/>',
  list:'<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M9 9h6M9 13h6M9 17h3"/>',
  music:'<path d="M9 18V5l11-2v13M9 9l11-2"/><ellipse cx="6" cy="18" rx="3" ry="2.5"/><ellipse cx="17" cy="16" rx="3" ry="2.5"/>',
  book:'<path d="M12 5C8 2 4 3 2 4v15c4-2 7-1 10 1 3-2 6-3 10-1V4c-2-1-6-2-10 1Zm0 0v15"/><path d="M5 8h3M5 11h3m8-3h3m-3 3h3"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',plus:'<path d="M12 5v14M5 12h14"/>',
  check:'<path d="m5 12 4 4 10-10"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',
  up:'<path d="m6 15 6-6 6 6"/>',down:'<path d="m6 9 6 6 6-6"/>',
  pause:'<path d="M9 5v14M15 5v14"/>',play:'<path d="m8 4 12 8-12 8Z"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  heart:'<path d="M12 20 3 11C-2 4 8 0 12 7c4-7 14-3 9 4Z"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/>',
  focus:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  print:'<path d="M7 8V3h10v5M7 17H4V9h16v8h-3M7 14h10v7H7Z"/>',
  link:'<path d="m10 14 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 2 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 0)"/>',
  edit:'<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-4-4L5 15Z"/>'
};
const icon=(name,cls='')=>`<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
const read=key=>{try{return JSON.parse(localStorage.getItem(key));}catch{return null;}};
const STORE='wcfi-music-hub-v2';
// Browser-local song authoring and service-specific section order.
const SONG_STORE='wcfi-song-workshop-v1';
let workshop={songs:[],arrangements:{}};
try{const saved=JSON.parse(localStorage.getItem(SONG_STORE));if(saved&&Array.isArray(saved.songs)&&saved.arrangements&&typeof saved.arrangements==='object')workshop=saved;}catch{}
workshop.deletedSongIds=Array.isArray(workshop.deletedSongIds)?workshop.deletedSongIds.filter(id=>typeof id==='string'):[];
for(const saved of workshop.songs){if(!saved||typeof saved.id!=='string'||!Array.isArray(saved.sections)||!saved.sections.every(s=>typeof s.label==='string'&&Array.isArray(s.lines)&&s.lines.every(l=>typeof l==='string'))||!/^([A-G])([b#]?)(m?)$/.test(saved.key))continue;const index=songs.findIndex(s=>s.id===saved.id);if(index<0)songs.push(saved);else songs[index]=saved;}
let songEdit=null,flowEdit=null;
let songEditorTab='edit',editorSection=0,songEditBaseline='',songEditorReturn='catalogue';
const DRAFT_PREFIX='wcfi-song-editor-draft-v1:';
let draftSaved=true;
function recoverSongDraft(id){const draft=read(DRAFT_PREFIX+id);if(!draft?.song||draft.song.id!==id||!Array.isArray(draft.song.sections))return false;songEdit=structuredClone(draft.song);songEditBaseline=draft.baseline||JSON.stringify(songFor(id)||songEdit);editorSection=Math.min(draft.section||0,songEdit.sections.length-1);songEditorReturn=draft.back||'catalogue';return true;}
function saveSongDraft(){if(!songEdit)return true;try{localStorage.setItem(DRAFT_PREFIX+songEdit.id,JSON.stringify({song:songEdit,baseline:songEditBaseline,section:editorSection,back:songEditorReturn}));localStorage.setItem('wcfi-song-editor-last',JSON.stringify(songEdit.id));draftSaved=true;}catch{draftSaved=false;}return draftSaved;}
function clearSongDraft(id){try{localStorage.removeItem(DRAFT_PREFIX+id);if(read('wcfi-song-editor-last')===id)localStorage.removeItem('wcfi-song-editor-last');}catch{}}
function draftResumeButton(){const id=read('wcfi-song-editor-last');return typeof id==='string'&&!ministry?.hiddenSongIds?.includes(id)&&read(DRAFT_PREFIX+id)?`<button class="secondary" data-route="songedit/${encodeURIComponent(id)}">Resume song draft</button>`:'';}
function syncSongEditor(){
 if(!songEdit)return;
 readSongForm();const elements=[...document.querySelectorAll('.section-edit')],names=songEdit.sections.map(s=>s.label);
 elements.forEach((element,i)=>{const section=songEdit.sections[i],count=section.lines.filter(l=>l.trim()).length;const label=element.querySelector('.section-summary-label'),lines=element.querySelector('.section-summary-lines');if(label)label.textContent=section.label||'New section';if(lines)lines.textContent=`${count} ${count===1?'line':'lines'}`;element.querySelectorAll('[aria-invalid]').forEach(e=>e.removeAttribute('aria-invalid'));const note=element.querySelector('.section-error');note.hidden=true;note.textContent='';if(element.open)editorSection=i;});
 const issues=songEditIssues(),issue=issues[0];
 names.forEach((name,i)=>{if(name&&names.filter(n=>n.toLowerCase()===name.toLowerCase()).length>1){const element=elements[i];if(!element)return;element.querySelector('input').setAttribute('aria-invalid','true');const note=element.querySelector('.section-error');note.hidden=false;note.textContent=`“${name}” is already used. Use a distinct name, such as ${name} 2. To repeat the same section, add it again in the line-up arrangement.`;}});
 for(const error of issues){if(error.section<0)continue;const el=elements[error.section];if(el&&!el.querySelector('[aria-invalid]')){el.querySelector(error.field==='name'?'input':'textarea').setAttribute('aria-invalid','true');const note=el.querySelector('.section-error');note.hidden=false;note.textContent=error.message;}}
 elements.forEach(el=>{const invalid=!!el.querySelector('[aria-invalid=true]');el.classList.toggle('section-invalid',invalid);let badge=el.querySelector('.section-error-badge');if(!badge){badge=document.createElement('span');badge.className='section-error-badge';el.querySelector('summary').append(badge);}badge.hidden=!invalid;badge.textContent='Needs attention';});
 const stored=issue?saveSongDraft():applyValidSong();
 const status=$('editor-draft-status');if(status){status.textContent=stored?(issue?'Draft saved · Fix errors to preview':'Saved'):'Couldn’t save changes';status.classList.toggle('draft-save-failed',!stored||!!issue);}
 if($('song-error')){const global=issues.find(e=>e.section<0);$('song-error').hidden=!global;$('song-error').textContent=global?global.message:'';}

}

function applyValidSong(){
 const old=songFor(songEdit.id),names=songEdit.sections.map(s=>s.label);
 const saved={...songEdit,title:songEdit.title.trim(),localChart:true,metadataOnly:false,sequence:sectionFlow(songEdit).filter(l=>names.includes(l)).join(' · ')};
 if(JSON.stringify(old)!==JSON.stringify(saved)){
  const next={...workshop,songs:[...workshop.songs.filter(s=>s.id!==saved.id),saved]};
  if(!storeWorkshop(next)){saveSongDraft();draftSaved=false;return false;}
  if(old)Object.assign(old,saved);else songs.push(saved);
 }
 clearSongDraft(saved.id);songEditBaseline=JSON.stringify(songEdit);draftSaved=true;return true;
}
function canLeaveSongEditor(){
 readSongForm();syncSongEditor();
 if(songEditIssues().length)return confirm('Leave with errors? Your draft is kept on this device, but these changes have not been applied to the song.');
 if(!draftSaved)return confirm('Changes could not be saved. Leave anyway?');
 return true;
}
const displayKeys=new Map();
const displayKeyId=()=>`${current.lineup||'catalogue'}/${current.id}`;
function storeWorkshop(next){try{localStorage.setItem(SONG_STORE,JSON.stringify(next));workshop=next;return true;}catch{notify('Could not save. Browser storage is full or unavailable. Keep this editor open.');return false;}}
function sectionFlow(song){const names=song.sections.map(s=>s.label);const sequence=(song.sequence||'').split(' · ').filter(x=>names.includes(x));return sequence.length?sequence:names;}
function serviceSong(){return current.lineup?(data.lineups.find(l=>l.id===current.lineup)||ministryViews?.lineup(current.lineup))?.songs.find(s=>s.id===current.id):null;}
function servicePlan(song){return workshop.arrangements[current.lineup]?.[song.id]||{key:serviceSong()?.key||song.key,flow:sectionFlow(song)};}
function songRoute(){return `song/${encodeURIComponent(current.id)}?from=${current.from||'lineups'}${current.lineup?'&lineup='+encodeURIComponent(current.lineup):''}`;}
function beginSongEdit(id){songEditorReturn=id?songRoute():'catalogue';songEditorTab='edit';editorSection=0;const song=songFor(id);songEdit=song?structuredClone(song):{id:'local-'+crypto.randomUUID(),title:'',key:'G',category:'Worship',bpm:72,meter:'4/4',sections:[{label:'Verse 1',lines:['']},{label:'Chorus',lines:['']}],sequence:''};songEdit.key ||= 'G';if(!songEdit.sections.length)songEdit.sections=[{label:'Verse 1',lines:['']},{label:'Chorus',lines:['']}];songEdit.metadataOnly=false;songEditBaseline=JSON.stringify(songEdit);recoverSongDraft(songEdit.id);saveSongDraft();go(`songedit/${songEdit.id}`);}
function readSongForm(){if(!$('song-title'))return;songEdit.title=$('song-title').value;songEdit.artist=$('song-artist').value;songEdit.key=$('song-base-key').value;songEdit.sections=[...document.querySelectorAll('.section-edit')].map(e=>({label:e.querySelector('input').value.trim(),lines:e.querySelector('textarea').value.split('\n')}));}
function songEditor(){
 if(!songEdit||songEdit.id!==current.id){songEdit=null;if(!recoverSongDraft(current.id)){const saved=songFor(current.id);if(!saved)return missing();songEdit=structuredClone(saved);songEditorReturn=`song/${encodeURIComponent(current.id)}`;songEditBaseline=JSON.stringify(songEdit);}}
 return `<section class="screen tool song-workshop song-editor">${heading(songFor(songEdit.id)?'Edit song':'Add song','Valid edits save automatically.', songFor(songEdit.id)&&!workshop.deletedSongIds.includes(songEdit.id)?`<button class="secondary danger-action" data-action="sx-delete-song">Delete song</button>`:'',songEditorReturn)}<div class="editor-bar"><div role="tablist" aria-label="Song editor"><button role="tab" aria-selected="${songEditorTab==='edit'}" data-action="sx-editor-edit">Edit</button><button role="tab" aria-selected="${songEditorTab==='preview'}" data-action="sx-preview">Preview</button></div><div class="song-export-actions"><button class="secondary" data-action="sx-export-song">Export song</button><button class="secondary" data-action="sx-cancel">Close</button></div></div><p id="editor-draft-status" role="status" class="caption"></p><p id="song-error" role="alert" tabindex="-1" hidden></p><div class="surface scroll workshop-body" id="song-editor-body" role="tabpanel" aria-label="Edit" ${songEditorTab!=='edit'?'hidden':''}><div class="editor-meta"><label class="field">Song title<input id="song-title" maxlength="100" value="${escape(songEdit.title)}"></label><label class="field">Chart key<select id="song-base-key">${['C','Db','D','Eb','E','F','F#','G','Ab','A','Bb','B'].flatMap(k=>[k,k+'m']).map(k=>`<option ${k===songEdit.key?'selected':''}>${k}</option>`).join('')}</select></label><label class="field song-artist-field">Artist / songwriter (optional)<input id="song-artist" maxlength="160" value="${escape(songEdit.artist||'')}" placeholder="Who is this song by?"></label></div><p class="caption editor-help">Write chords in brackets: <code>[G]Morning [C]mercy</code>. Preview places them above the lyrics. Chart key describes these chords; it does not transpose them.</p><p class="caption">Drag a section heading to reorder.</p><div id="song-sections">${songEdit.sections.map((s,i)=>`<details class="section-edit" ${i===editorSection?'open':''}><summary><span class="section-summary-label">${escape(s.label||'New section')}</span><span class="section-summary-lines">${s.lines.filter(l=>l.trim()).length} lines</span></summary><div class="section-edit-fields"><label class="field">Section name<input aria-label="Section ${i+1} name" maxlength="60" value="${escape(s.label)}"></label><label class="field">Lyrics and chords<textarea aria-label="Section ${i+1} lyrics and chords" aria-describedby="section-error-${i}" rows="6" maxlength="12000" spellcheck="false">${escape(s.lines.join('\n'))}</textarea></label><p class="section-error" id="section-error-${i}" hidden></p><button class="text-link" data-action="sx-remove-section" data-index="${i}" ${songEdit.sections.length===1?'disabled':''}>Remove section</button></div></details>`).join('')}</div><button class="secondary" data-action="sx-add-section">+ Add section</button></div><div class="surface scroll workshop-body" id="song-preview-body" role="tabpanel" aria-label="Preview" ${songEditorTab!=='preview'?'hidden':''}><p class="caption" id="song-preview-key"></p><div id="song-edit-preview"></div></div></section>`;
}
function switchSongEditorTab(tab){
 const edit=$('song-editor-body'),preview=$('song-preview-body');
 if(!edit||!preview)return;
 const outgoing=songEditorTab==='edit'?edit:preview;
 outgoing.dataset.scrollPosition=String(outgoing.scrollTop);
 if(tab==='preview'){
  const signature=JSON.stringify([songEdit.key,songEdit.sections]);
  if(preview.dataset.chartSignature!==signature){
   $('song-edit-preview').innerHTML=chartMarkup(songEdit,songEdit.key);
   $('song-preview-key').textContent='Chart preview · '+songEdit.key;
   preview.dataset.chartSignature=signature;
  }
 }
 songEditorTab=tab;
 edit.hidden=tab!=='edit';preview.hidden=tab!=='preview';
 const incoming=tab==='edit'?edit:preview;
 incoming.scrollTop=Number(incoming.dataset.scrollPosition||0);
 document.querySelectorAll('.editor-bar [role="tab"]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.action===(tab==='edit'?'sx-editor-edit':'sx-preview'))));
}
function authorChart(song){const item=serviceSong(),plan=item?servicePlan(song):null,key=displayKeys.get(displayKeyId())||plan?.key||data.keys[song.id]||song.key;const flow=plan?.flow||song.sections.map(s=>s.label);const ordered=flow.map(label=>song.sections.find(s=>s.label===label)).filter(Boolean);return `<section class="screen tool song-workshop">${heading(song.title,song.artist?'By '+song.artist:(song.id==='morning-mercy'?'Original demo song':'Local song chart'),`<button class="secondary" data-action="sx-edit-song">Edit song</button>`,chartReturn)}<div class="surface workshop-controls"><label>Key: <select id="workshop-key">${keysFor(song).map(k=>`<option value="${escape(k)}" ${k===key?'selected':''}>${keyText(k)}${pitch(k)===pitch(song.key)?' (original)':''}</option>`).join('')}</select></label><button class="secondary" id="reset-song-key" data-action="sx-reset-key" ${pitch(key)===pitch(song.key)?'hidden':''}>Reset to original</button>${item?`<span>Line-up key: ${escape(plan.key)}</span><button class="secondary" data-action="sx-edit-flow">Edit arrangement</button>`:''}</div><div class="surface scroll workshop-body"><div id="workshop-chart">${chartMarkup({...song,sections:ordered},key)}</div></div></section>`;}
function flowEditor(){const song=songFor(current.id);if(!song||!serviceSong())return missing();flowEdit ||= structuredClone(servicePlan(song));return `<section class="screen tool song-workshop">${heading('Arrangement',song.title,`<button class="primary" data-action="sx-save-flow">Save arrangement</button>`,songRoute())}<div class="surface scroll workshop-body"><label class="field">Line-up key<select id="flow-key">${keyOptions(song,flowEdit.key)}</select></label><p class="caption">For this line-up only. Repeat a section by adding it again.</p><ol class="flow-editor">${flowEdit.flow.map((label,i)=>`<li><strong>${escape(label)}</strong><div><button class="secondary" data-action="sx-flow-up" data-index="${i}" aria-label="Move section ${i+1} up" ${i===0?'disabled':''}>↑</button><button class="secondary" data-action="sx-flow-down" data-index="${i}" aria-label="Move section ${i+1} down" ${i===flowEdit.flow.length-1?'disabled':''}>↓</button><button class="secondary" data-action="sx-flow-repeat" data-index="${i}">Repeat</button><button class="secondary" data-action="sx-flow-remove" data-index="${i}">Remove</button></div></li>`).join('')}</ol><div class="workshop-controls"><select id="flow-section" aria-label="Section to add">${song.sections.map(s=>`<option>${escape(s.label)}</option>`).join('')}</select><button class="secondary" data-action="sx-flow-add">Add section</button></div></div></section>`;}
function editorIssue(message,section=-1,line=-1,field='lyrics'){return Object.assign(new Error(message),{section,line,field});}
function showSongIssue(error){
 if(songEditorTab!=='edit')switchSongEditorTab('edit');
 const summary=$('song-error');summary.hidden=false;summary.textContent='Song has an error — '+error.message;
 if(error.section>=0){const section=document.querySelectorAll('.section-edit')[error.section];section.open=true;const field=section.querySelector(error.field==='name'?'input':'textarea');field.setAttribute('aria-invalid','true');const inline=$('section-error-'+error.section);inline.hidden=false;inline.textContent=error.message;section.scrollIntoView({block:'nearest'});field.focus({preventScroll:true});if(error.line>=0){const lines=field.value.split('\n');const start=lines.slice(0,error.line).join('\n').length+(error.line?1:0);field.setSelectionRange(start,start+lines[error.line].length);} }
 else{const field=error.field==='title'?$('song-title'):summary;field.focus({preventScroll:true});field.scrollIntoView({block:'nearest'});}
}
function songEditIssues(){
 const issues=[];
 const previous=songFor(songEdit.id);
 if(previous?.key&&previous.key.endsWith('m')!==songEdit.key.endsWith('m')&&[...data.lineups,data.draft].some(l=>l.songs.some(s=>s.id===songEdit.id)))issues.push(editorIssue('This song is used in a line-up. Keep its major/minor mode or create a separate version.'));
 if(!songEdit.title.trim())issues.push(editorIssue('Enter a song title.',-1,-1,'title'));
 const names=songEdit.sections.map(s=>s.label);

 for(const [i,s] of songEdit.sections.entries()){
  if(!s.label||names.findIndex(n=>n.toLowerCase()===s.label.toLowerCase())!==i)issues.push(editorIssue('Give this section a unique name.',i,-1,'name'));
  if(!s.lines.some(l=>l.trim()))issues.push(editorIssue(`${s.label}: add lyrics or chords.`,i));
  for(const [j,line] of s.lines.entries()){
   const tokens=[...line.matchAll(/\[([^\]]*)\]/g)];
   for(const match of tokens)if(!isChord(match[1]))issues.push(editorIssue(`${s.label}, line ${j+1}: “${match[1]}” is not a supported chord. Use an A–G chord such as [Bm7], [D/F#] or [(C#m)], or [N.C.] for no chord.`,i,j));
   const lyrics=line.replace(/\[[^\]]*\]/g,'');
   if(/[\[\]]/.test(lyrics))issues.push(editorIssue(`${s.label}, line ${j+1}: close each chord with matching [brackets].`,i,j));
   const typo=lyrics.match(/\b[H-Z](?:#|b)?(?:m|maj|min|dim|aug|sus|add)?(?:7|9|11|13)(?:\/[A-Z][#b]?)?\b/);
   if(typo)issues.push(editorIssue(`${s.label}, line ${j+1}: “${typo[0]}” looks like a chord typo. Chord roots are A–G; write chords in brackets.`,i,j));
  }
 }
 for(const perLineup of Object.values(workshop.arrangements))for(const [id,plan] of Object.entries(perLineup))if(id===songEdit.id&&plan.flow.some(label=>!names.includes(label)))issues.push(editorIssue('A saved line-up uses a section you renamed or removed. Update its arrangement first.'));
 return issues;
}
function validateSongEdit(){const issue=songEditIssues()[0];if(issue)throw issue;}
function downloadSongs(items,title){
 const blob=new Blob([JSON.stringify(songExport(items),null,2)+'\n'],{type:'application/json;charset=utf-8'});
 const url=URL.createObjectURL(blob),link=document.createElement('a');
 link.href=url;link.download=songExportFilename(title);document.body.append(link);
 try{link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);}
 notify(`Export ready · ${items.length} saved song${items.length===1?'':'s'}`);
}
async function workshopAction(button){const action=button.dataset.action;if(!action?.startsWith('sx-'))return false;
 if(action==='sx-export-all'){const items=visibleSongs();if(items.length)downloadSongs(items,'wcfi-all-songs-'+new Date().toISOString().slice(0,10));return true;}
 if(action==='sx-export-song'){
  readSongForm();try{validateSongEdit();if(applyValidSong())downloadSongs([songFor(songEdit.id)],songEdit.title);}catch(error){showSongIssue(error);notify('Fix the song errors before exporting.');}return true;
 }
 if(action==='sx-reset-key'){const select=$('workshop-key'),song=songFor(current.id);select.value=keysFor(song).find(key=>pitch(key)===pitch(song.key));select.dispatchEvent(new Event('change',{bubbles:true}));select.focus({preventScroll:true});return true;}
 if(action==='sx-delete-song'){
  const song=songFor(current.id);if(!song)return true;
  if(!confirm(`Are you sure you want to delete “${song.title}” from the song catalogue on this device? Existing line-ups will keep this song.`))return true;
  const next={...workshop,deletedSongIds:[...new Set([...workshop.deletedSongIds,song.id])]};
  if(!storeWorkshop(next))return true;
  clearSongDraft(song.id);songEdit=null;catalogueQuery='';catalogueCategory='All songs';go('catalogue');notify('Song deleted from the catalogue.');return true;
 }
 if(action==='sx-cancel'){go(songEditorReturn);return true;}
 if(action==='sx-editor-edit'){switchSongEditorTab('edit');return true;}
 if(action==='sx-new-song')beginSongEdit();
 if(action==='sx-edit-song')beginSongEdit(current.id);
 if(action==='sx-add-section'||action==='sx-remove-section'){readSongForm();if(action==='sx-add-section'){songEdit.sections.push({label:'',lines:['']});editorSection=songEdit.sections.length-1;}else{const section=songEdit.sections[Number(button.dataset.index)];if(!confirm(`Are you sure you want to remove ${section.label||'this section'} and its lyrics and chords?`))return true;songEdit.sections.splice(Number(button.dataset.index),1);editorSection=Math.max(0,Number(button.dataset.index)-1);}saveSongDraft();render();document.querySelectorAll('.section-edit')[editorSection]?.scrollIntoView({block:'nearest'});}
 if(action==='sx-preview'){readSongForm();try{validateSongEdit();switchSongEditorTab('preview');}catch(e){showSongIssue(e);}return true;}
 if(action==='sx-edit-flow'){flowEdit=structuredClone(servicePlan(songFor(current.id)));go(songRoute().replace('song/','arrange/'));}
 if(action.startsWith('sx-flow-')){flowEdit.key=$('flow-key').value;const i=Number(button.dataset.index),flow=flowEdit.flow;if(action==='sx-flow-add'&&flow.length<100)flow.push($('flow-section').value);if(action==='sx-flow-repeat'&&flow.length<100)flow.splice(i+1,0,flow[i]);if(action==='sx-flow-remove')flow.splice(i,1);const j=i+(action==='sx-flow-up'?-1:1);if(['sx-flow-up','sx-flow-down'].includes(action)&&j>=0&&j<flow.length)[flow[i],flow[j]]=[flow[j],flow[i]];render();}
 if(action==='sx-save-flow'){flowEdit.key=$('flow-key').value;if(!flowEdit.flow.length){notify('Add at least one section.');return true;}const next=structuredClone(workshop);next.arrangements[current.lineup] ||= {};next.arrangements[current.lineup][current.id]=structuredClone(flowEdit);if(storeWorkshop(next)){const local=data.lineups.find(l=>l.id===current.lineup)?.songs.find(s=>s.id===current.id);if(local){local.key=flowEdit.key;persist();}displayKeys.delete(displayKeyId());persist();flowEdit=null;go(songRoute());notify('Arrangement saved for this line-up.');}}
 return true;
}

let data=loadData(read(STORE),read('wcfi-music-preview-v1'));
let homeChoice='latest',navigationTimer;
let current={name:'home'},makerTab='songs',catalogueQuery='',catalogueCategory='All songs',pickerQuery='',resource='prepare',chartReturn='catalogue',focus=false,toastTimer;
function notify(message){$('toast').textContent=message;$('toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('visible'),3000);}
function persist(){try{localStorage.setItem(STORE,JSON.stringify(data));}catch{notify('Browser storage unavailable. Changes last for this visit.');}}
function go(route){
  if(current.name==='songedit'&&songEdit&&!route.startsWith('songedit/')){if(!canLeaveSongEditor())return;songEdit=null;}

  const [path,search='']=route.split('?');
  if(path.startsWith('lineup/')||path.startsWith('song/')){
    const query=new URLSearchParams(search);
    if(!query.has('from'))query.set('from',current.name==='home'?'home':current.name==='lineups'?'lineups':current.from||'lineups');
    route=path+'?'+query;
  }
  if(location.hash==='#'+route){render();return;}
  if(current.name==='home'&&['lineups','maker','catalogue','resources'].includes(route))homeChoice=route;
  clearTimeout(navigationTimer);
  if(matchMedia('(prefers-reduced-motion: reduce)').matches){location.hash=route;return;}
  document.body.classList.add('leaving');
  navigationTimer=setTimeout(()=>{location.hash=route;},180);
}
function dateText(value){if(!value)return 'Date to be decided';const [y,m,d]=value.split('-').map(Number);return new Date(y,m-1,d).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}
const recent=()=>[...data.lineups].filter(l=>!ministry||!l.demo).sort((a,b)=>b.updated-a.updated);
const visibleSongs=()=>songs.filter(s=>!workshop.deletedSongIds.includes(s.id)&&!ministry?.hiddenSongIds?.includes(s.id)&&(s.localChart||s.id==='morning-mercy')).sort((a,b)=>Number(b.id==='morning-mercy')-Number(a.id==='morning-mercy'));
const heading=(title,subtitle,actions='',back='home')=>`<div class="view-heading"><button class="back" data-route="${back}" aria-label="${back==='home'?'Back to main menu':'Back'}">${icon('back')}</button><div><h1>${escape(title)}</h1>${subtitle?`<p>${escape(subtitle)}</p>`:''}</div>${actions?`<div class="heading-actions">${actions}</div>`:''}</div>`;
const search=(id,placeholder,value='')=>`<label class="search">${icon('search')}<input id="${id}" type="search" placeholder="${placeholder}" aria-label="${placeholder}" value="${escape(value)}" autocomplete="off"></label>`;
let ministryViews=null;
function refreshMinistryViews(){if(!ministry)return;const snapshot={...ministry,songs:ministry.songs.map(s=>({...s,title:songFor(s.id)?.title||s.title})),lineups:ministry.lineups.map(l=>({...l,songs:l.songs.map(s=>({...s,key:workshop.arrangements[l.id]?.[s.id]?.key??s.key}))}))};ministryViews=createMinistryViews(snapshot,{heading,icon,dateText});}
refreshMinistryViews();
const keyOptions=(song,value)=>(song.metadataOnly?`<option value="" ${value===null?'selected':''}>Unknown</option>`:'')+keysFor(song).map(k=>`<option value="${k}" ${value===k?'selected':''}>${keyText(k)}</option>`).join('');
function latestPreview(lineup){
  if(!lineup)return `<button class="latest-card latest-empty" data-route="maker" data-menu="latest"><span class="latest-name">No line-ups yet</span><span class="latest-empty-copy">Choose your songs and put the gathering in order.</span><span class="latest-open">Create your first line-up ${icon('arrow')}</span></button>`;
  const date=lineup.date?dateText(lineup.date):'Date to be decided';
  const updated=lineup.updated>0?new Date(lineup.updated):null;
  const updateLabel=updated?updated.toLocaleDateString(undefined,{month:'short',day:'numeric'}):'';
  const count=`${lineup.songs.length} ${lineup.songs.length===1?'song':'songs'}`;
  return `<button class="latest-card" data-route="lineup/${encodeURIComponent(lineup.id)}" data-menu="latest" aria-label="Open line-up: ${escape(lineup.title)}" aria-describedby="latest-date latest-status">
    <span class="latest-top"><span class="latest-name">${escape(lineup.title)}</span><span class="latest-date" id="latest-date">${lineup.date?`<time datetime="${escape(lineup.date)}">${escape(date)}</time>`:date}</span></span>
    <span class="latest-tracks">${lineup.songs.map((item,i)=>`<span class="latest-track"><span class="latest-number">${String(i+1).padStart(2,'0')}</span><span class="latest-song">${escape(songFor(item.id).title)}</span><span class="latest-key"><span>Key</span> ${keyText(item.key)}</span></span>`).join('')||'<span class="latest-no-songs">No songs added</span>'}</span>
    <span class="latest-footer"><span class="latest-status" id="latest-status">${lineup.demo?'Example · ':''}${count}${lineup.songs.length>3?` · +${lineup.songs.length-3} more`:''}${!lineup.demo&&updated?`<span class="latest-updated" title="${escape(updated.toLocaleString())}"> · Updated ${escape(updateLabel)}</span>`:''}</span><span class="latest-open">Open line-up ${icon('arrow')}</span></span>
  </button>`;
}
function home(){
  const latest=recent()[0];
  return `<section class="screen simple-menu" aria-label="Music Ministry main menu">
    <h1 class="menu-title">Music <em>Ministry</em></h1>
    <section class="latest-section" aria-labelledby="recent-heading"><div class="recent-heading"><h2 id="recent-heading">${ministry?'Upcoming line-up':'Latest line-up'}</h2><button class="view-lineups" data-route="lineups" data-menu="lineups">All line-ups ${icon('arrow')}</button></div>${ministry?ministryViews.preview():latestPreview(latest)}</section>
    <nav class="tool-choices" aria-label="Ministry tools">${[
      ['maker','list','Line-up Maker'],['catalogue','music','Song Catalogue'],['resources','book','Resources']
    ].map(([route,art,title])=>`<button class="tool-choice" data-route="${route}" data-menu="${route}"><span class="choice-icon">${icon(art)}</span><span>${title}</span><span class="choice-arrow">${icon('arrow')}</span></button>`).join('')}</nav>
  </section>`;
}

function selectMenu(route,focusButton=false){
  if(!['latest','lineups','maker','catalogue','resources'].includes(route))return;homeChoice=route;
  if(focusButton)document.querySelector(`[data-menu="${route}"]`)?.focus({preventScroll:true});
}

function songRow(song,{key=song.key,index,route=`song/${song.id}`}={}){
  if(song.metadataOnly)return `<button class="song-row ministry-song-row" data-route="${escape(route)}"><span class="song-emblem">${icon('music')}</span><span class="song-info"><strong>${escape(song.title)}</strong><small>${escape(song.category)} · Song information</small></span><span class="pill ${key?'':'unknown-key'}">${key?'Key '+keyText(key):'No chart'}</span>${icon('arrow','arrow')}</button>`;
  return `<button class="song-row" data-route="${escape(route)}">${index!==undefined?`<span class="order">${String(index+1).padStart(2,'0')}</span>`:`<span class="song-emblem">${icon('music')}</span>`}<span class="song-info"><strong>${escape(song.title)}</strong><small>${song.artist?'By '+escape(song.artist)+' · ':''}${escape(song.category)}${song.localChart?'':' · '+song.bpm+' BPM · '+escape(song.meter)}</small></span><span class="caption">${song.localChart?'Local chart':'Original demo'}</span><span class="pill">${keyText(key)}</span>${icon('arrow','arrow')}</button>`;
}
function catalogueRows(){if(!visibleSongs().length)return '<div class="empty"><b>No songs yet.</b>Choose Add song to start your catalogue.</div>';const list=visibleSongs().filter(s=>(catalogueCategory==='All songs'||s.category===catalogueCategory)&&`${s.title} ${s.artist||''} ${s.description} ${s.category} ${s.sections.flatMap(x=>x.lines).join(' ')}`.toLowerCase().includes(catalogueQuery.toLowerCase()));return list.length?list.map(s=>songRow(s)).join(''):'<div class="empty"><b>No songs found.</b>Try another title, lyric or category.</div>';}
function catalogue(){return `<section class="screen tool">${heading('Song Catalogue','A songbook for the whole team.',`<div class="song-export-actions"><button class="secondary" data-action="sx-export-all" ${visibleSongs().length?'':'disabled'}>Export all songs</button><button class="primary" data-action="sx-new-song">+ Add song</button></div>`)}${draftResumeButton()}<div class="toolbar">${search('catalogue-search','Search titles, artists or lyrics…',catalogueQuery)}<select id="catalogue-category" aria-label="Song category">${['All songs',...new Set(visibleSongs().map(s=>s.category))].map(c=>`<option ${c===catalogueCategory?'selected':''}>${escape(c)}</option>`).join('')}</select></div><div id="catalogue-results" class="surface scroll catalogue-content">${catalogueRows()}</div></section>`;}
function lineups(){return `<section class="screen tool">${heading('Latest line-ups','',`<button class="primary" data-route="maker">${icon('plus')} Make a line-up</button>`)}<div class="surface scroll lineups-list">${ministry?ministryViews.listCards():''}${recent().map(l=>`<button class="lineup-card" data-route="lineup/${encodeURIComponent(l.id)}"><span class="eyebrow">${l.demo?'EXAMPLE LINE-UP':escape(dateText(l.date))}</span><h2>${escape(l.title)}</h2><p>${l.songs.slice(0,3).map(x=>escape(songFor(x.id).title)).join(' · ')}${l.songs.length>3?'…':''}</p><footer><span>${l.songs.length} songs · ${l.demo?'Try the flow':ministry?'Personal draft · not announced':'Saved in this browser'}</span>${icon('arrow')}</footer></button>`).join('')||(!ministry?'<div class="empty"><b>A fresh start.</b>Create your first line-up with the maker.</div>':'')}</div></section>`;}
function lineupView(id){if(ministryViews?.lineup(id))return ministryViews.detail(id,current.tab,current.from);const l=data.lineups.find(x=>x.id===id);if(!l)return missing();return `<section class="screen tool">${heading(l.title,l.demo?'Example line-up · Original demo songs':(ministry?'Personal draft · ':'')+dateText(l.date),`<button class="primary" data-action="edit-lineup" data-id="${escape(id)}">${icon('edit')} Edit</button>`,current.from)}<div class="surface scroll lineup-detail">${l.notes?`<p class="lineup-notes">${escape(l.notes)}</p>`:''}${l.songs.map((x,i)=>songRow(songFor(x.id),{key:x.key,index:i,route:`song/${x.id}?key=${encodeURIComponent(x.key)}&lineup=${encodeURIComponent(id)}`})).join('')}</div><p class="caption">Tap a song to open its chart in the line-up key. Personal transposition won’t change this line-up.</p></section>`;}
function pickerRows(){const list=visibleSongs().filter(s=>`${s.title} ${s.category}`.toLowerCase().includes(pickerQuery.toLowerCase()));return list.map(s=>{const added=data.draft.songs.some(x=>x.id===s.id);return `<div class="picker-row"><span class="song-info"><strong>${escape(s.title)}</strong><small>${escape(s.category)} · ${s.metadataOnly?'Key to be chosen':escape(s.key)+' · '+s.bpm+' BPM'}</small></span><button class="icon-button" data-action="add-song" data-id="${s.id}" data-added="${added}" aria-label="${added?'Already added:':'Add'} ${escape(s.title)}" ${added?'disabled':''}>${icon(added?'check':'plus')}</button></div>`;}).join('')||'<p class="empty">No matching songs.</p>';}
function draftRows(){return data.draft.songs.map((item,i)=>{const s=songFor(item.id);return `<div class="draft-row"><div class="draft-row-top"><span class="order">${String(i+1).padStart(2,'0')}</span><strong>${escape(s.title)}</strong></div><div class="draft-controls"><label for="draft-key-${s.id}">Performance key</label><select id="draft-key-${s.id}" data-key-id="${s.id}" aria-label="Performance key for ${escape(s.title)}">${keyOptions(s,item.key)}</select><button data-action="move-up" data-id="${s.id}" aria-label="Move ${escape(s.title)} up" ${i===0?'disabled':''}>${icon('up')}</button><button data-action="move-down" data-id="${s.id}" aria-label="Move ${escape(s.title)} down" ${i===data.draft.songs.length-1?'disabled':''}>${icon('down')}</button><button data-action="remove-song" data-id="${s.id}" aria-label="Remove ${escape(s.title)}">${icon('close')}</button></div></div>`;}).join('')||'<div class="empty"><b>Start with a song.</b>Add songs from the collection.<br>Then arrange the flow and choose your keys.</div>';}
function maker(){return `<section class="screen tool maker">${heading('Line-up Maker',ministry?'Personal draft · does not change announced plans':data.draft.id?'Editing a saved line-up':'A little intention before the first note.',`<button class="primary" data-action="save-lineup">${icon('check')} Save</button>`)}<div class="maker-meta"><label class="field">Line-up name<input id="draft-title" maxlength="100" placeholder="e.g. Sunday worship" value="${escape(data.draft.title)}"></label><label class="field">Gathering date (optional)<input id="draft-date" type="date" value="${escape(data.draft.date)}"></label></div><div class="maker-tabs"><button data-maker-tab="songs" aria-pressed="${makerTab==='songs'}">Add songs</button><button data-maker-tab="lineup" aria-pressed="${makerTab==='lineup'}">Your line-up <span id="draft-count">${data.draft.songs.length}</span></button></div><div class="maker-grid" data-tab="${makerTab}"><section class="surface maker-pane songs-pane"><h2>Find your songs</h2>${search('picker-search',ministry?'Search songs…':'Search demo songs…',pickerQuery)}<div class="scroll picker-list" id="picker-results">${pickerRows()}</div></section><section class="surface maker-pane arrangement-pane"><h2>Your line-up</h2><div class="scroll draft-list" id="draft-results">${draftRows()}</div><label class="field maker-note">Notes for the gathering<textarea id="draft-notes" maxlength="2000" placeholder="Transitions, cues, moments to leave space…">${escape(data.draft.notes)}</textarea></label></section></div><p class="save-status">${ministry?'Draft kept in this browser · Saving does not confirm or publish the plan.':'Draft kept in this browser · Save to feature it on the main menu.'}</p></section>`;}
const resources={
  prepare:{label:'Prepare',icon:'heart',eyebrow:'BEFORE THE FIRST NOTE',title:'Bring your whole heart.',intro:'Preparation is more than knowing the songs. Take a moment to arrive, listen, and remember the people you are serving.',quote:'What would it look like to make room for someone else today?',heading:'A quiet moment before rehearsal',steps:['Pause. Take three unhurried breaths and put down the rush of the day.','Pray for the people gathering, the team beside you, and a heart ready to listen.','Read a passage together. Psalm 95:1–7 or Colossians 3:16 can be a place to begin.','Choose one intention for today: listen closely, encourage someone, or leave room for silence.']},
  rehearse:{label:'Rehearse',icon:'music',eyebrow:'PRACTICE TOGETHER',title:'Listen before you add.',intro:'A clear arrangement gives every voice a place. Use rehearsal to agree on the small things that let the whole team play with confidence.',quote:'Leave enough space to hear the person beside you.',heading:'A simple rehearsal rhythm',steps:['Confirm the line-up, performance keys, tempos, openings and endings before a full run.','Walk through transitions. Decide who gives each cue and where the room needs a breath.','Play the quietest section first. Make room for the voice and listen for the pulse.','Finish with one uninterrupted run, then share one encouragement and one clear adjustment.']},
  serve:{label:'Serve',icon:'sun',eyebrow:'ON THE DAY',title:'Make room for the room.',intro:'The gathering is full of people arriving with different stories. A steady, welcoming team helps them find their place.',quote:'We bring our preparation. We stay attentive to the moment.',heading:'Before you step in',steps:['Arrive with time to settle, check your equipment and help someone else.','Confirm the latest line-up and keys with the leader. Keep one agreed version in view.','Keep water, spare essentials and your chart ready; silence distracting notifications.','Afterwards, put things away together and note what the team wants to carry into next time.']}
};
function resourceBody(){const r=resources[resource];return `<span class="eyebrow">${r.eyebrow}</span><h2>${r.title}</h2><p>${r.intro}</p><p class="reflection">${r.quote}</p><h3>${r.heading}</h3><ol>${r.steps.map(s=>`<li>${s}</li>`).join('')}</ol><p class="caption">A starting point for conversation with your worship leader.</p>`;}
function resourceView(){if(ministryViews)return ministryViews.resources();return `<section class="screen tool">${heading('Worshipper Resources','Prepare the heart. Practice the craft. Serve together.')}<div class="resources-grid"><nav class="surface resource-nav" aria-label="Resource topics">${Object.entries(resources).map(([id,r])=>`<button class="resource-tab" data-resource="${id}" aria-pressed="${resource===id}">${icon(r.icon)} ${r.label}</button>`).join('')}</nav><article class="surface scroll resource-reading" id="resource-body">${resourceBody()}</article></div></section>`;}
function currentKey(song){return keysFor(song).includes(data.keys[song.id])?data.keys[song.id]:song.key;}
function chartMarkup(song,displayKey=currentKey(song)){return song.sections.map(section=>`<section class="chart-section"><h3>${escape(section.label)}</h3>${section.lines.map(raw=>{
  const parts=tokens(raw),instrumental=parts.every(p=>!p.lyric);if(data.lyrics&&instrumental)return '';
  let html='',word=false;
  for(const part of parts){if(!part.join||!word){if(word)html+='</span> ';html+='<span class="lyric-word">';word=true;}html+=`<span class="lyric-chunk"><span class="chord" data-source-chord="${escape(part.chord||'')}">${part.chord?keyText(transpose(part.chord,song.key,displayKey)):''}</span><span class="lyric">${escape(part.lyric)}</span></span>`;}
  if(word)html+='</span>';return `<div class="lyric-line ${instrumental?'instrumental':''}">${html}</div>`;
}).join('')}</section>`).join('');}
function chartView(id){const song=songFor(id);if(!song)return missing();if(!song.metadataOnly)return authorChart(song);if(song.metadataOnly)return ministryViews.song(id,current.lineup,chartReturn,`<button class="secondary" data-action="sx-edit-song">Add chart</button>`,current.slot);const lineKey=current.lineup?data.lineups.find(x=>x.id===current.lineup)?.songs.find(x=>x.id===id)?.key:null;
 return `<section class="screen tool chart-tool">${heading(song.title,`${escape(song.category)} · ${song.bpm} BPM · ${escape(song.meter)} · Original demo`,`<button class="secondary" data-action="chart-add" aria-label="Add ${song.title} to draft">${icon('plus')} Add</button>`,chartReturn)}<div class="surface chart-toolbar"><div class="key-control"><label for="chart-key">Your key</label><button data-action="key-down" aria-label="Transpose down one semitone">−</button><select id="chart-key" aria-label="Display key">${keyOptions(song,currentKey(song))}</select><button data-action="key-up" aria-label="Transpose up one semitone">+</button></div><button class="secondary" data-action="lyrics" aria-pressed="${data.lyrics}">${data.lyrics?'Show chords':'Lyrics only'}</button><div class="font-control"><button data-action="font-down" aria-label="Smaller chart text" ${data.font<=18?'disabled':''}>A−</button><button data-action="font-up" aria-label="Larger chart text" ${data.font>=28?'disabled':''}>A+</button></div><button class="secondary" data-action="focus" aria-pressed="${focus}">${icon(focus?'close':'focus')}${focus?'Exit focus':'Focus'}</button><button class="icon-button" data-action="copy" aria-label="Copy song link">${icon('link')}</button><button class="icon-button" data-action="print" aria-label="Print chart">${icon('print')}</button></div><div class="scroll chart-scroll ${data.lyrics?'lyrics-only':''}" id="chart-scroll"><div class="chart-context"><span>Original <b>${keyText(song.key)}</b>${lineKey?` · Line-up <b>${keyText(lineKey)}</b>`:''}<br>Display <b id="display-label">${keyText(currentKey(song))}</b> · just for you</span><span>${song.sequence}</span></div><div id="chart-content">${chartMarkup(song)}</div><p class="chart-bottom">WCFI Music Ministry · Original demonstration chart</p></div></section>`;
}
function missing(){return `<section class="screen tool">${heading('Let’s find your way back.','This item is no longer available.')}<button class="primary" data-route="home">Return to the main menu</button></section>`;}
function parseRoute(){const fragment=location.hash.slice(1)||'home';const [path,query='']=fragment.split('?');const [name,routeId]=path.split('/');const id=ministry?.songAliases?.[routeId]||routeId;const params=new URLSearchParams(query);current={name:['home','library','catalogue','lineups','maker','resources','song','lineup','songedit','arrange'].includes(name)?name:'home',id:id?decode(id):null,lineup:params.get('lineup'),tab:params.get('tab'),slot:params.get('slot'),from:params.get('from')==='home'?'home':'lineups'};if(name==='song'&&songFor(id)){const key=params.get('key');if(!songFor(id).metadataOnly&&keysFor(songFor(id)).includes(key))data.keys[id]=key;chartReturn=current.lineup?`lineup/${encodeURIComponent(current.lineup)}`:'catalogue';persist();}if(name!=='song'){focus=false;document.body.classList.remove('focus-chart');}}
function render(){refreshMinistryViews();if(ministryError){$('view').innerHTML='<section class="load-error"><h1>Ministry information could not load</h1><p>The announced plans are unavailable. Please retry.</p><button class="primary" data-action="reload">Retry</button></section>';return;}clearTimeout(navigationTimer);document.body.classList.remove('leaving');parseRoute();document.body.classList.toggle('title-mode',current.name==='home');const functions={home,library:catalogue,catalogue,lineups,maker,resources:resourceView,song:()=>chartView(current.id),songedit:songEditor,arrange:flowEditor,lineup:()=>lineupView(current.id)};const fn=functions[current.name]||home;$('view').innerHTML=fn();document.title=`${({home:'Music Ministry',maker:'Line-up Maker',catalogue:'Song Catalogue',lineups:'Latest line-ups',resources:'Worshipper Resources',song:songFor(current.id)?.title,lineup:ministryViews?.lineup(current.id)?.title||data.lineups.find(x=>x.id===current.id)?.title})[current.name]||'Music Ministry'} · WCFI`;document.documentElement.style.setProperty('--chart-size',`${data.font}px`);$('view').focus({preventScroll:true});if(current.name==='songedit')syncSongEditor();}
function selectLineupTab(tab,focusTab=false){
  if(current.name!=='lineup'||!ministryViews?.lineup(current.id)||!['songs','team','details'].includes(tab))return;
  current.tab=tab;
  document.querySelectorAll('.lineup-tabs [role="tab"]').forEach(button=>{const selected=button.dataset.lineupTab===tab;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});
  document.querySelectorAll('.lineup-panel').forEach(panel=>{panel.hidden=panel.id!==`lineup-panel-${tab}`;});
  const params=new URLSearchParams({from:current.from});if(tab!=='songs')params.set('tab',tab);const query='?'+params;
  history.replaceState(null,'',`#lineup/${encodeURIComponent(current.id)}${query}`);
  if(focusTab)document.getElementById(`lineup-tab-${tab}`).focus({preventScroll:true});
}
function updateDraftLists(){if($('picker-results'))$('picker-results').innerHTML=pickerRows();if($('draft-results'))$('draft-results').innerHTML=draftRows();if($('draft-count'))$('draft-count').textContent=data.draft.songs.length;}
function addSong(id){if(data.draft.songs.some(x=>x.id===id)){notify('That song is already in your draft.');return;}const song=songFor(id);if(!song)return;data.draft.songs.push({id,key:currentKey(song)});persist();updateDraftLists();notify(`${song.title} added to your line-up`);}
function changeKey(value){const song=songFor(current.id);if(!song||!keysFor(song).includes(value))return;data.keys[song.id]=value;persist();$('chart-key').value=value;$('chart-content').innerHTML=chartMarkup(song);$('display-label').innerHTML=keyText(value);const q=new URLSearchParams({key:value});if(current.lineup)q.set('lineup',current.lineup);q.set('from',current.from);history.replaceState(null,'',`#song/${song.id}?${q}`);}

// Drag from the disclosure heading; the panel body remains available for scrolling.
let sectionDrag=null,sectionClickUntil=0;
function reindexSongSections(){
 document.querySelectorAll('#song-sections > .section-edit').forEach((el,i)=>{
  el.querySelector('input').setAttribute('aria-label',`Section ${i+1} name`);
  const text=el.querySelector('textarea');text.setAttribute('aria-label',`Section ${i+1} lyrics and chords`);text.setAttribute('aria-describedby',`section-error-${i}`);
  el.querySelector('.section-error').id=`section-error-${i}`;
  el.querySelector('[data-action="sx-remove-section"]').dataset.index=String(i);
 });
}
function finishSectionDrag(cancel=false){
 const d=sectionDrag;if(!d)return;sectionDrag=null;cancelAnimationFrame(d.frame);
 if(!d.active)return;
 if(cancel)d.order.forEach(el=>d.list.append(el));
 d.panel.classList.remove('section-dragging');d.ghost.remove();document.body.classList.remove('section-reordering');
 sectionClickUntil=Date.now()+500;
 reindexSongSections();syncSongEditor();
 if(!cancel)notify(`Section moved to position ${[...d.list.children].indexOf(d.panel)+1}.`);
}
function moveSectionDrag(x,y){
 const d=sectionDrag;if(!d)return;
 d.x=x;d.y=y;
 if(!d.active){if(Math.hypot(x-d.startX,y-d.startY)<4)return;d.activate();}
 d.ghost.style.left=`${Math.max(8,Math.min(innerWidth-d.ghost.offsetWidth-8,x-70))}px`;d.ghost.style.top=`${y+12}px`;
 const others=[...d.list.children].filter(el=>el!==d.panel);
 const before=others.find(el=>{const r=el.getBoundingClientRect();return y<r.top+r.height/2;});
 if(before!==d.panel.nextElementSibling)d.list.insertBefore(d.panel,before||null);
}
function startSectionDrag(target,x,y,type,id){
 if(current.name!=='songedit'||songEditorTab!=='edit'||sectionDrag||target.closest('input,textarea,select,button,a'))return;
 const summary=target.closest('#song-sections > .section-edit > summary');if(!summary)return;const panel=summary.parentElement;
 const list=panel.parentElement;
 const d=sectionDrag={panel,list,order:[...list.children],x,y,startX:x,startY:y,type,id,active:false};
 d.activate=()=>{
  if(sectionDrag!==d||!panel.isConnected)return;
  d.active=true;panel.classList.add('section-dragging');document.body.classList.add('section-reordering');
  d.ghost=document.createElement('div');d.ghost.className='section-drag-ghost';d.ghost.textContent=panel.querySelector('input').value||'New section';document.body.append(d.ghost);
  const scroll=()=>{if(sectionDrag!==d)return;const body=$('song-editor-body'),r=body.getBoundingClientRect();const delta=d.y<r.top+55?-9:d.y>r.bottom-55?9:0;if(delta){body.scrollTop+=delta;moveSectionDrag(d.x,d.y);}d.frame=requestAnimationFrame(scroll);};d.frame=requestAnimationFrame(scroll);
 };
}
$('view').addEventListener('pointerdown',e=>{if(e.pointerType!=='touch'&&e.button===0)startSectionDrag(e.target,e.clientX,e.clientY,'pointer',e.pointerId);});
addEventListener('pointermove',e=>{if(sectionDrag?.type==='pointer'&&sectionDrag.id===e.pointerId){if(sectionDrag.active)e.preventDefault();moveSectionDrag(e.clientX,e.clientY);}});
addEventListener('pointerup',e=>{if(sectionDrag?.type==='pointer'&&sectionDrag.id===e.pointerId)finishSectionDrag();});
addEventListener('pointercancel',()=>{if(sectionDrag?.type==='pointer')finishSectionDrag(true);});
$('view').addEventListener('touchstart',e=>{if(e.touches.length!==1){finishSectionDrag(true);return;}const t=e.touches[0];startSectionDrag(e.target,t.clientX,t.clientY,'touch',t.identifier);},{passive:true});
addEventListener('touchmove',e=>{if(sectionDrag?.type!=='touch')return;const t=[...e.touches].find(t=>t.identifier===sectionDrag.id);if(!t)return;e.preventDefault();moveSectionDrag(t.clientX,t.clientY);},{passive:false});
addEventListener('touchend',()=>{if(sectionDrag?.type==='touch')finishSectionDrag();});
addEventListener('touchcancel',()=>finishSectionDrag(true));
addEventListener('blur',()=>finishSectionDrag(true));
$('view').addEventListener('contextmenu',e=>{if(sectionDrag){e.preventDefault();}});
$('view').addEventListener('click',e=>{if(Date.now()<sectionClickUntil){e.preventDefault();e.stopImmediatePropagation();}},true);
addEventListener('keydown',e=>{
 if(e.key==='Escape'&&sectionDrag){e.preventDefault();e.stopImmediatePropagation();finishSectionDrag(true);return;}
 const summary=e.target.closest?.('#song-sections > .section-edit > summary');
 if(summary&&e.altKey&&['ArrowUp','ArrowDown'].includes(e.key)){
  e.preventDefault();e.stopImmediatePropagation();const panel=summary.parentElement,list=panel.parentElement;
  if(e.key==='ArrowUp'&&panel.previousElementSibling)list.insertBefore(panel,panel.previousElementSibling);
  else if(e.key==='ArrowDown'&&panel.nextElementSibling)list.insertBefore(panel.nextElementSibling,panel);
  reindexSongSections();syncSongEditor();summary.focus({preventScroll:true});panel.scrollIntoView({block:'nearest'});
 }
},true);

$('view').addEventListener('input',e=>{
  if(current.name==='songedit'){syncSongEditor();}


  if(e.target.id==='catalogue-search'){catalogueQuery=e.target.value;$('catalogue-results').innerHTML=catalogueRows();}
  if(e.target.id==='picker-search'){pickerQuery=e.target.value;$('picker-results').innerHTML=pickerRows();}
  for(const [id,key] of [['draft-title','title'],['draft-date','date'],['draft-notes','notes']])if(e.target.id===id){data.draft[key]=e.target.value;persist();}
});
$('view').addEventListener('change',e=>{
  if(current.name==='songedit')syncSongEditor();
  if(e.target.id==='catalogue-category'){catalogueCategory=e.target.value;$('catalogue-results').innerHTML=catalogueRows();}
  if(e.target.id==='workshop-key'){
    const song=songFor(current.id),key=e.target.value;if(!song||!keysFor(song).includes(key))return;
    displayKeys.set(displayKeyId(),key);if(!current.lineup){data.keys[current.id]=key;persist();}
    const panel=document.querySelector('.workshop-body'),scroll=panel.scrollTop;
    document.querySelectorAll('#workshop-chart .chord').forEach(node=>{node.textContent=node.dataset.sourceChord?transpose(node.dataset.sourceChord,song.key,key).replaceAll('b','♭').replaceAll('#','♯'):'';});
    panel.scrollTop=scroll;
    $('reset-song-key').hidden=pitch(key)===pitch(song.key);
  }
  if(e.target.id==='chart-key')changeKey(e.target.value);
  if(e.target.dataset.keyId){const item=data.draft.songs.find(x=>x.id===e.target.dataset.keyId);if(item){item.key=e.target.value||null;persist();}}
});
$('view').addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b||b.disabled)return;
  if(await workshopAction(b))return;
  if(b.dataset.lineupTab){selectLineupTab(b.dataset.lineupTab,true);return;}
  if(b.dataset.route){go(b.dataset.route);return;}
  if(b.dataset.makerTab){makerTab=b.dataset.makerTab;document.querySelector('.maker-grid').dataset.tab=makerTab;document.querySelectorAll('[data-maker-tab]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.makerTab===makerTab)));return;}
  if(b.dataset.resource){resource=b.dataset.resource;$('resource-body').innerHTML=resourceBody();$('resource-body').scrollTop=0;document.querySelectorAll('[data-resource]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.resource===resource)));return;}
  const action=b.dataset.action,id=b.dataset.id;
  if(action==='reload')location.reload();
  if(action==='copy-source'){const source=ministryViews?.lineup(id);if(source){data.draft={id:null,title:source.title,date:source.date,notes:'Personal working copy; source announcements are unchanged.',songs:source.songs.map(s=>({id:s.id,key:s.key}))};persist();go('maker');}return;}
  if(action==='info')$('about').showModal();
  if(action==='add-song')addSong(id);
  if(action==='chart-add')addSong(current.id);
  if(action==='remove-song'){data.draft.songs=data.draft.songs.filter(x=>x.id!==id);persist();updateDraftLists();}
  if(action==='move-up'||action==='move-down'){const i=data.draft.songs.findIndex(x=>x.id===id),j=i+(action==='move-up'?-1:1);if(i>=0&&j>=0&&j<data.draft.songs.length){[data.draft.songs[i],data.draft.songs[j]]=[data.draft.songs[j],data.draft.songs[i]];persist();updateDraftLists();}}
  if(action==='save-lineup'){try{const saved=saveLineup(data,crypto.randomUUID(),Date.now());persist();go(`lineup/${encodeURIComponent(saved.id)}`);notify(ministry?'Personal draft saved. Announced plans are unchanged.':'Line-up saved. Find it on the main menu.');}catch(error){notify(error.message);if(!data.draft.title.trim())$('draft-title')?.focus();}}
  if(action==='edit-lineup'){const item=data.lineups.find(x=>x.id===id);if(item){data.draft={...structuredClone(item),id:item.demo?null:item.id,demo:false};makerTab='lineup';persist();go('maker');}}
  if(action==='key-up'||action==='key-down'){const song=songFor(current.id),delta=action==='key-up'?1:-1;changeKey(keysFor(song)[(pitch(currentKey(song))+delta+12)%12]);}
  if(action==='lyrics'){data.lyrics=!data.lyrics;persist();const scroll=$('chart-scroll').scrollTop;render();$('chart-scroll').scrollTop=scroll;}
  if(action==='font-up'||action==='font-down'){data.font=Math.min(28,Math.max(18,data.font+(action==='font-up'?2:-2)));persist();document.documentElement.style.setProperty('--chart-size',`${data.font}px`);document.querySelector('[data-action=font-up]').disabled=data.font>=28;document.querySelector('[data-action=font-down]').disabled=data.font<=18;}
  if(action==='focus'){focus=!focus;document.body.classList.toggle('focus-chart',focus);b.innerHTML=icon(focus?'close':'focus')+(focus?'Exit focus':'Focus');b.setAttribute('aria-pressed',String(focus));}
  if(action==='print')window.print();
  if(action==='copy'){const url=new URL(location.href);url.hash=`song/${current.id}?key=${encodeURIComponent(currentKey(songFor(current.id)))}`;try{await navigator.clipboard.writeText(url.href);notify('Song link copied with your display key.');}catch{notify('Use your browser’s Share button to copy this link.');}}
});
$('about-open').innerHTML=icon('info');$('about-open').onclick=()=>$('about').showModal();for(const id of ['about-close','about-done'])$(id).onclick=()=>$('about').close();

$('view').addEventListener('focusin',e=>{const b=e.target.closest('[data-menu]');if(b&&b.dataset.menu!==homeChoice)selectMenu(b.dataset.menu);});
addEventListener('beforeunload',e=>{if(current.name==='songedit'&&songEdit){readSongForm();syncSongEditor();if(songEditIssues().length||!draftSaved){e.preventDefault();e.returnValue='';}}});
addEventListener('hashchange',()=>{if(current.name==='songedit'&&songEdit&&location.hash.split('?')[0]!==`#songedit/${current.id}`){if(!canLeaveSongEditor()){history.replaceState(null,'',`#songedit/${encodeURIComponent(current.id)}`);return;}songEdit=null;}render();});
addEventListener('keydown',e=>{
  if($('about').open)return;
  const editorTab=e.target.closest?.('.editor-bar [role="tab"]');
  if(editorTab&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const tabs=[...document.querySelectorAll('.editor-bar [role="tab"]')];const index=e.key==='Home'?0:e.key==='End'?1:1-tabs.indexOf(editorTab);tabs[index].click();document.querySelectorAll('.editor-bar [role="tab"]')[index]?.focus();return;}
  const activeTab=e.target.closest?.('.lineup-tabs [role="tab"]');
  if(activeTab&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
    e.preventDefault();const tabs=['songs','team','details'],index=tabs.indexOf(activeTab.dataset.lineupTab);
    selectLineupTab(tabs[e.key==='Home'?0:e.key==='End'?2:(index+(e.key==='ArrowRight'?1:2))%3],true);return;
  }
  if(e.key==='Escape'){if(focus){focus=false;document.body.classList.remove('focus-chart');render();}else if(current.name!=='home'){e.preventDefault();go('home');}return;}
  if(current.name!=='home')return;
  const order=['latest','lineups','maker','catalogue','resources'];let index=order.indexOf(homeChoice);
  if(['ArrowUp','ArrowDown','Home','End'].includes(e.key)){e.preventDefault();index=e.key==='Home'?0:e.key==='End'?order.length-1:(index+(e.key==='ArrowDown'?1:order.length-1))%order.length;selectMenu(order[index],true);}
  if(e.key==='Enter'&&document.activeElement===$('view')){e.preventDefault();document.querySelector(`[data-menu="${homeChoice}"]`)?.click();}
});
render();
