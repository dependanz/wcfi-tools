import {songs,demoSet} from './songs.mjs';
import {KEYS,pitch,transpose,tokens} from './chords.mjs';

const $ = (id) => document.getElementById(id);
const icons = {
  music:'<path d="M9 18V5l12-2v13M9 9l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  star:'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  moon:'<path d="M20.8 13A9 9 0 0 1 11 3.2 9 9 0 1 0 20.8 13Z"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2m-3-7 1.5-1.5M5 19l1.5-1.5m0-11L5 5m12.5 12.5L19 19"/>',
  arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>',
  back:'<path d="M19 12H5m6-6-6 6 6 6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  check:'<path d="m5 12 4 4 10-10"/>',
  link:'<path d="m10 13 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 3 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 1)"/>',
  print:'<path d="M7 8V3h10v5M7 17H4V9h16v8h-3M7 14h10v7H7Z"/><path d="M17 11h.01"/>',
  focus:'<path d="M8 3H3v5M16 3h5v5M21 16v5h-5M8 21H3v-5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  up:'<path d="m6 15 6-6 6 6"/>',
  down:'<path d="m6 9 6 6 6-6"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>'
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name]}</svg>`;
const displayKey = (key) => key.replaceAll('b','♭').replaceAll('#','♯');
const songFor = (id) => songs.find(s => s.id === id);
const keyOptions = (song) => KEYS.map(k=>k + (song.key.endsWith('m')?'m':''));
const STORE = 'wcfi-music-preview-v1';
function initialState() { return {selected:songs[0].id,keys:{},favorites:[],set:structuredClone(demoSet),font:20,lyrics:false,dark:false}; }
function load() {
  const base=initialState();
  try {
    const s=JSON.parse(localStorage.getItem(STORE));
    if(!s || typeof s!=='object')return base;
    base.selected=songFor(s.selected)?s.selected:base.selected;
    base.favorites=Array.isArray(s.favorites)?[...new Set(s.favorites.filter(songFor))]:[];
    base.font=[18,20,22,24,26,28].includes(s.font)?s.font:20;
    base.lyrics=s.lyrics===true; base.dark=s.dark===true;
    for(const song of songs)if(keyOptions(song).includes(s.keys?.[song.id]))base.keys[song.id]=s.keys[song.id];
    if(Array.isArray(s.set))base.set=s.set.filter((x,i,a)=>x&&songFor(x.id)&&keyOptions(songFor(x.id)).includes(x.key)&&a.findIndex(y=>y?.id===x.id)===i).map(x=>({id:x.id,key:x.key}));
  } catch {}
  return base;
}
let state=load(), collection='library', favoritesOnly=false, focused=false, toastTimer;
const currentSong=()=>songFor(state.selected);
const currentKey=()=>state.keys[state.selected]||currentSong().key;
function save(){try{localStorage.setItem(STORE,JSON.stringify(state));}catch{notify('Browser storage unavailable. Changes will last for this visit.');}}
function notify(message){$('toast').textContent=message;$('toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('visible'),3000);}
function applyPreferences(){document.body.classList.toggle('dark',state.dark);document.documentElement.style.setProperty('--chart-size',`${state.font}px`);$('theme-toggle').innerHTML=icon(state.dark?'sun':'moon');$('theme-toggle').setAttribute('aria-label',`Switch to ${state.dark?'light':'dark'} theme`);$('font-down').disabled=state.font<=18;$('font-up').disabled=state.font>=28;document.querySelector('meta[name="theme-color"]').content=state.dark?'#161f1e':'#f4f3ef';}
function makeKeySelect(song,value,handler,label){const select=document.createElement('select');select.setAttribute('aria-label',label);for(const key of keyOptions(song)){const option=new Option(displayKey(key),key);select.add(option);}select.value=value;select.addEventListener('change',()=>handler(select.value));return select;}
function buttonIcon(name,label,handler){const b=document.createElement('button');b.innerHTML=icon(name);b.setAttribute('aria-label',label);b.addEventListener('click',handler);return b;}
function renderList(){
  $('library-tab').setAttribute('aria-pressed',collection==='library');$('set-tab').setAttribute('aria-pressed',collection==='set');$('set-count').textContent=state.set.length;
  $('filters').hidden=collection==='set';$('set-intro').hidden=collection!=='set';$('search').placeholder=collection==='set'?'Find a song in this set…':'Find a song…';
  $('favorites-filter').setAttribute('aria-pressed',favoritesOnly);
  const query=$('search').value.trim().toLowerCase(),category=$('category').value;
  const candidates=collection==='set'?state.set.map(x=>songFor(x.id)):songs;
  const visible=candidates.filter(song=>(`${song.title} ${song.category}`.toLowerCase().includes(query))&&(collection==='set'||((category==='all'||song.category===category)&&(!favoritesOnly||state.favorites.includes(song.id)))));
  $('result-label').textContent=collection==='set'?'RUN OF SONGS':favoritesOnly?'YOUR FAVORITES':'ALL SONGS';$('result-count').textContent=`${visible.length} ${visible.length===1?'song':'songs'}`;
  const list=$('song-list');list.replaceChildren();
  for(const song of visible){
    const b=document.createElement('button');b.className=`song-card${state.selected===song.id?' active':''}`;b.setAttribute('aria-label',`Open ${song.title}`);if(state.selected===song.id)b.setAttribute('aria-current','true');
    const display=collection==='set'?state.set.find(x=>x.id===song.id).key:song.key;
    b.innerHTML=`<span class="song-art ${song.color}">${icon('music')}</span><span class="song-card-text"><b>${song.title}</b><small>${song.category} · ${song.bpm} BPM${state.favorites.includes(song.id)?' · ★':''}</small></span><span class="card-key">${displayKey(display)}</span>`;
    b.addEventListener('click',()=>selectSong(song.id));
    if(collection==='library'){list.append(b);continue;}
    const wrapper=document.createElement('div');wrapper.className='set-item';wrapper.append(b);
    const controls=document.createElement('div');controls.className='set-item-controls';const label=document.createElement('label');label.textContent='Set key';const select=makeKeySelect(song,display,key=>{state.set.find(x=>x.id===song.id).key=key;save();renderList();renderKeyContext();notify(`Set key saved: ${song.title} in ${displayKey(key)}`);},`Performance key for ${song.title}`);select.id=`set-key-${song.id}`;label.htmlFor=select.id;controls.append(label,select);
    for(const [delta,ic,word] of [[-1,'up','up'],[1,'down','down']]){const button=buttonIcon(ic,`Move ${song.title} ${word}`,()=>{const i=state.set.findIndex(x=>x.id===song.id);[state.set[i],state.set[i+delta]]=[state.set[i+delta],state.set[i]];save();renderList();notify(`${song.title} moved ${word}`);});const index=state.set.findIndex(x=>x.id===song.id);button.disabled=index+delta<0||index+delta>=state.set.length;controls.append(button);}
    controls.append(buttonIcon('trash',`Remove ${song.title} from set`,()=>{state.set=state.set.filter(x=>x.id!==song.id);save();renderList();renderAddButton();renderKeyContext();notify('Removed from your demo set');}));wrapper.append(controls);list.append(wrapper);
  }
  if(!visible.length){const empty=document.createElement('div');empty.className='empty-state';empty.innerHTML=collection==='set'&&!query?'<b>Your set is a blank page.</b>Open a song from the library and choose “Add to set”.':'<b>No songs found.</b>Try another title or clear your filters.';list.append(empty);}
}
function renderAddButton(){const added=state.set.some(x=>x.id===state.selected);$('add-to-set').innerHTML=icon(added?'check':'plus')+(added?'In Sunday set':'Add to set');$('add-to-set').setAttribute('aria-pressed',added);$('add-to-set').setAttribute('aria-label',added?'Show song in Sunday set':'Add song to Sunday set');}
function renderKeyContext(){const song=currentSong(),setKey=state.set.find(x=>x.id===song.id)?.key;$('key-context').innerHTML=`Original: <strong>${displayKey(song.key)}</strong>${setKey?` · Set key: <strong>${displayKey(setKey)}</strong>`:''} · Display: <strong>${displayKey(currentKey())}</strong> (just you)`;$('reset-key').hidden=currentKey()===song.key;}
function renderChart(){
  const song=currentSong();$('chart').replaceChildren();$('chart').classList.toggle('lyrics-only',state.lyrics);$('chords-mode').setAttribute('aria-pressed',!state.lyrics);$('lyrics-mode').setAttribute('aria-pressed',state.lyrics);
  song.sections.forEach((section,index)=>{
    const block=document.createElement('section');block.className=`chart-section${['Chorus','Refrain'].includes(section.label)?' chorus':''}`;block.id=`section-${index}`;block.setAttribute('aria-labelledby',`section-heading-${index}`);
    const title=document.createElement('h3');title.id=`section-heading-${index}`;title.textContent=section.label;block.append(title);
    for(const raw of section.lines){const line=document.createElement('div');line.className='lyric-line';const parts=tokens(raw);if(parts.every(p=>!p.lyric))line.classList.add('instrumental');let word;for(const part of parts){if(!part.join||!word){word=document.createElement('span');word.className='lyric-word';if(line.children.length)line.append(document.createTextNode(' '));line.append(word);}const chunk=document.createElement('span');chunk.className='lyric-chunk';const chord=document.createElement('span');chord.className='chord';chord.textContent=part.chord?displayKey(transpose(part.chord,song.key,currentKey())):'';const lyric=document.createElement('span');lyric.className='lyric';lyric.textContent=part.lyric;chunk.append(chord,lyric);word.append(chunk);}block.append(line);}
    if(!(state.lyrics&&section.lines.every(raw=>tokens(raw).every(p=>!p.lyric))))$('chart').append(block);
  });
  $('section-nav').replaceChildren();song.sections.forEach((section,index)=>{if(!$(`section-${index}`))return;const b=document.createElement('button');b.textContent=section.label;b.addEventListener('click',()=>{const el=$(`section-${index}`);el.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});});$('section-nav').append(b);});
  renderKeyContext();
}
function renderReader(){const song=currentSong();document.title=`${song.title} · WCFI Music`;$('song-title').textContent=song.title;$('song-category').textContent=`${song.category.toUpperCase()} / ORIGINAL DEMO`;$('song-description').textContent=song.description;$('song-tempo').textContent=`${song.bpm} BPM`;$('song-meter').textContent=`${song.meter} time`;$('key-select').replaceChildren(...keyOptions(song).map(k=>new Option(displayKey(k),k)));$('key-select').value=currentKey();$('favorite-button').innerHTML=icon('star');$('favorite-button').setAttribute('aria-pressed',state.favorites.includes(song.id));$('favorite-button').setAttribute('aria-label',state.favorites.includes(song.id)?'Unfavorite this song':'Favorite this song');$('sequence').textContent=song.sequence;$('next-song').innerHTML='Next song '+icon('arrow');renderAddButton();renderChart();applyPreferences();}
function route(){const fragment=location.hash.slice(1);if(!fragment.startsWith('song/')){document.body.classList.remove('song-open');return;}const [id,query='']=fragment.slice(5).split('?');const song=songFor(id);if(!song){document.body.classList.remove('song-open');notify('Song not found. Choose one from the library.');return;}state.selected=id;const key=new URLSearchParams(query).get('key');if(keyOptions(song).includes(key))state.keys[id]=key;document.body.classList.add('song-open');renderList();renderReader();save();}
function setHash(hash,replace=false){history[replace?'replaceState':'pushState'](null,'',`#${hash}`);}
function selectSong(id){state.selected=id;setHash(`song/${id}?key=${encodeURIComponent(currentKey())}`);document.body.classList.add('song-open');renderList();renderReader();save();window.scrollTo({top:0,behavior:'instant'});if(matchMedia('(max-width: 760px)').matches)$('song-title').focus({preventScroll:true});}
function changeKey(value){state.keys[state.selected]=value;$('key-select').value=value;renderChart();save();setHash(`song/${state.selected}?key=${encodeURIComponent(value)}`,true);notify(`Your display is now in ${displayKey(value)}`);}
function setCollection(value){collection=value;$('search').value='';renderList();}
function setFocus(value){focused=value;document.body.classList.toggle('focus',focused);$('focus-button').innerHTML=icon(focused?'close':'focus')+`<span>${focused?'Exit focus':'Focus'}</span>`;$('focus-button').setAttribute('aria-pressed',focused);$('focus-button').setAttribute('aria-label',focused?'Exit focus mode':'Enter focus mode');}
$('search-icon').innerHTML=icon('search');$('about-button').innerHTML=icon('info');$('note-icon').innerHTML=icon('info');$('back-button').innerHTML=icon('back')+'Songbook';$('copy-button').innerHTML=icon('link');$('print-button').innerHTML=icon('print');setFocus(false);
$('library-tab').onclick=()=>setCollection('library');$('set-tab').onclick=()=>setCollection('set');$('search').oninput=renderList;$('category').onchange=renderList;$('favorites-filter').onclick=()=>{favoritesOnly=!favoritesOnly;renderList();};
$('key-select').onchange=e=>changeKey(e.target.value);
for(const [id,delta] of [['key-down',-1],['key-up',1]])$(id).onclick=()=>{const index=(pitch(currentKey())+delta+12)%12;changeKey(keyOptions(currentSong())[index]);};
$('reset-key').onclick=()=>changeKey(currentSong().key);
for(const [id,value] of [['chords-mode',false],['lyrics-mode',true]])$(id).onclick=()=>{state.lyrics=value;renderChart();save();};
for(const [id,delta] of [['font-down',-2],['font-up',2]])$(id).onclick=()=>{state.font=Math.max(18,Math.min(28,state.font+delta));applyPreferences();save();};
$('focus-button').onclick=()=>setFocus(!focused);
$('favorite-button').onclick=()=>{const i=state.favorites.indexOf(state.selected);if(i<0)state.favorites.push(state.selected);else state.favorites.splice(i,1);save();renderList();renderReader();};
$('add-to-set').onclick=()=>{if(state.set.some(x=>x.id===state.selected)){setCollection('set');document.body.classList.remove('song-open');setHash('library');setFocus(false);window.scrollTo({top:0,behavior:'instant'});return;}state.set.push({id:state.selected,key:currentKey()});save();renderAddButton();renderKeyContext();renderList();notify('Added to your demo Sunday set');};
$('back-button').onclick=()=>{setFocus(false);setHash('library');document.body.classList.remove('song-open');window.scrollTo({top:0,behavior:'instant'});};
document.querySelector('.brand').onclick=e=>{e.preventDefault();setFocus(false);setCollection('library');setHash('library');document.body.classList.remove('song-open');window.scrollTo({top:0,behavior:'instant'});};
$('next-song').onclick=()=>{const ordered=collection==='set'&&state.set.length?state.set.map(x=>songFor(x.id)):songs;const i=ordered.findIndex(x=>x.id===state.selected);selectSong(ordered[(i+1)%ordered.length].id);};
$('theme-toggle').onclick=()=>{state.dark=!state.dark;applyPreferences();save();};
$('copy-button').onclick=async()=>{const url=new URL(location.href);url.hash=`song/${state.selected}?key=${encodeURIComponent(currentKey())}`;try{await navigator.clipboard.writeText(url.href);notify('Link copied with your displayed key');}catch{notify('Use your browser’s Share button to copy this song link.');}};
$('print-button').onclick=()=>window.print();$('about-button').onclick=()=>$('about-dialog').showModal();$('close-dialog').onclick=()=>$('about-dialog').close();
$('about-dialog').addEventListener('click',e=>{if(e.target===$('about-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
$('reset-demo').onclick=()=>{state=initialState();collection='library';favoritesOnly=false;$('search').value='';$('category').value='all';setFocus(false);save();setHash('library',true);document.body.classList.remove('song-open');renderList();renderReader();$('about-dialog').close();notify('Demo set and preferences reset');};
addEventListener('hashchange',route);addEventListener('popstate',route);
addEventListener('keydown',e=>{if(e.key==='Escape'&&focused)setFocus(false);if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)&&!$('about-dialog').open){e.preventDefault();setFocus(false);document.body.classList.remove('song-open');setHash('library');$('search').focus();}});
renderList();renderReader();route();
