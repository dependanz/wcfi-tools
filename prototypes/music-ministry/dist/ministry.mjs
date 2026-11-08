// Generic rendering only. Ministry content is supplied by the private server.
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function safeUrl(value){try{const u=new URL(value);return u.protocol==='https:'?u.href:null;}catch{return null;}}
export function validateMinistry(value){
  if(!value||value.schemaVersion!==1||!Array.isArray(value.songs)||!Array.isArray(value.lineups)||!Array.isArray(value.resources)||!Array.isArray(value.events)||!Array.isArray(value.unannounced))throw Error('Invalid ministry snapshot');
  const unique=items=>new Set(items.map(x=>x.id)).size===items.length&&items.every(x=>typeof x.id==='string'&&/^[a-z0-9-]+$/.test(x.id));
  if(!unique(value.songs)||!unique(value.lineups)||value.songs.length>500||value.lineups.length>100)throw Error('Invalid ministry records');
  const ids=new Set(value.songs.map(s=>s.id));
  if(!value.lineups.some(l=>l.id===value.featuredLineupId))throw Error('Featured line-up unavailable');
  for(const l of value.lineups){if(!Array.isArray(l.songs)||!l.songs.every(s=>ids.has(s.id)&&(s.key===null||/^[A-G](?:b|#)?m?$/.test(s.key)))||!ids.has(l.closing?.id))throw Error('Invalid line-up song reference');}
  return value;
}
export async function loadMinistry(){
  const response=await fetch('./ministry-data.json',{cache:'no-store',credentials:'same-origin'});
  if(!response.ok)throw Error('Ministry snapshot unavailable');
  const value=await response.json();return value.available===false?null:validateMinistry(value);
}
export function registerMinistrySongs(snapshot,registry){
  if(!snapshot)return;
  for(const song of snapshot.songs){if(registry.some(s=>s.id===song.id))throw Error('Song identifier collision');const chart=Array.isArray(song.sections)&&song.sections.length>0;registry.push(chart?{...song,metadataOnly:false,localChart:true}:{...song,metadataOnly:true,key:null,bpm:null,meter:'',sections:[]});}
}
export function createMinistryViews(snapshot,{heading,icon,dateText}){
  snapshot={...snapshot,lineups:snapshot.lineups.map(l=>({...l,title:`${dateText(l.date)} · ${l.leader||'Song leader not set'}`}))};
  const song=id=>snapshot.songs.find(s=>s.id===id),lineup=id=>snapshot.lineups.find(l=>l.id===id);
  const key=value=>value?esc(value).replaceAll('b','♭').replaceAll('#','♯'):'Unknown';
  const badge=(text,tone='neutral')=>`<span class="evidence-tag ${tone}">${esc(text)}</span>`;
  const links=items=>(items||[]).map(item=>{const url=safeUrl(item.url);return url?`<a class="source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(item.label)} ${icon('arrow')}${item.note?`<small>${esc(item.note)}</small>`:''}</a>`:'';}).join('');
  const notes=items=>(items||[]).length?`<ul class="evidence-notes">${items.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'';
  const sourceDetails=l=>`<details class="source-details"><summary>Sources and review history</summary>${links(l.sources)}${notes(l.evidenceNotes)}</details>`;
  const arrangements=item=>item.arrangement?.length?`<div class="arrangement-sequence">${item.arrangement.map((label,i)=>`<span class="${label==='Blank'?'cue-unknown':''}"><small>${i+1}</small>${esc(label)}</span>`).join('')}</div>`:'<p class="muted">Arrangement not supplied.</p>';
  const songRow=(item,index,parent,closing=false)=>{
    const inferred=item.key&&item.keyState==='inferred';
    return `<button class="song-row service-song" data-route="song/${esc(item.id)}?lineup=${esc(parent)}${closing?'&slot=closing':''}"><span class="order">${closing?icon('music'):String(index+1).padStart(2,'0')}</span><span class="song-info">${closing?'<small class="song-role">Closing</small>':''}<strong>${esc(song(item.id).title)}</strong>${!closing&&item.role?`<small>${esc(item.role)}</small>`:''}</span><span class="service-key ${item.key?'':'unknown-key'}" aria-label="${item.key?'Key '+esc(item.key)+(inferred?', inferred':''):'Key unknown'}">${item.key?key(item.key):'?'}${inferred?'<small>inferred</small>':''}</span>${icon('arrow','arrow')}</button>`;
  };
  const keyNotice=l=>l.songs.some(item=>!item.key)?'Keys not set':'';
  const team=l=>{
    const assignments=[...new Set(l.team.map(p=>p.assignment))];
    return `<div class="team-list">${l.team.map(p=>`<article class="team-row"><span><small>${esc(p.role)}</small><strong>${esc(p.name)}</strong></span>${assignments.length>1?`<span><small>${esc(p.assignment)}</small></span>`:''}</article>`).join('')}</div>${assignments.length===1?`<p class="assignment-basis">Assignment basis: ${esc(assignments[0])}.</p>`:''}`;
  };
  const details=l=>{
    const facts=l.facts.filter(f=>['Practice','Practice date / time','Opening Scripture'].includes(f.label));
    return `<section class="details-section"><h2>Service details</h2><dl class="service-facts">${facts.map(f=>{const missing=f.state==='Unknown'||f.value==='Not supplied';return `<div><dt>${esc(f.label)}</dt><dd><strong class="${missing?'missing-value':''}">${missing?'Not supplied':esc(f.value)}</strong></dd></div>`;}).join('')}</dl></section>`;
  };
  return {
    lineup,
    preview(){const l=lineup(snapshot.featuredLineupId);return `<button class="latest-card upcoming-card" data-menu="latest" data-route="lineup/${esc(l.id)}" aria-label="Open upcoming line-up: ${esc(dateText(l.date))}" aria-describedby="latest-date latest-status"><span class="latest-top"><span class="latest-name">${esc(dateText(l.date))}</span><span class="latest-date" id="latest-date">${esc(l.leader)} · ${esc(l.practice.short)}</span></span><span class="latest-tracks">${l.songs.map((s,i)=>`<span class="latest-track"><span class="latest-number">${String(i+1).padStart(2,'0')}</span><span class="latest-song">${esc(song(s.id).title)}</span><span class="latest-key ${s.key?'':'unknown-key'}">${s.key?'Key '+key(s.key):'Key ?'}</span></span>`).join('')}</span><span class="latest-footer"><span class="latest-status" id="latest-status">${esc(keyNotice(l))}<span class="upcoming-count">${keyNotice(l)?' · ':''}${l.songs.length} songs</span></span><span class="latest-open">Open line-up ${icon('arrow')}</span></span></button>`;},
    listCards(){return snapshot.lineups.map(l=>`<button class="lineup-card" data-route="lineup/${esc(l.id)}"><h2>${esc(l.title)}</h2><p>${esc(l.practice.short)}</p>${keyNotice(l)?`<p>${esc(keyNotice(l))}</p>`:''}<footer><span>Announced plan · ${l.songs.length} songs</span>${icon('arrow')}</footer></button>`).join('');},
    detail(id,selected='songs',back='lineups'){
      const l=lineup(id);if(!l)return null;
      const active=['songs','team','details'].includes(selected)?selected:'songs';
      const panels={
        songs:`<div class="song-list-heading"><h2>Order of worship</h2><span>Key</span></div><div class="service-songs">${l.songs.map((s,i)=>songRow(s,i,id)).join('')}</div><p class="songs-hint">Tap a song for its arrangement and references.${l.songs.some(s=>!s.key)?' <span class="unknown-key">? = key unknown.</span>':''}</p>`,
        team:team(l),
        details:details(l)
      };
      return `<section class="screen tool ministry-tool lineup-viewer">${heading(l.title,'','',back)}<div class="surface lineup-shell"><div class="lineup-overview"><p class="service-lead"><span>Worship lead</span> <strong>${esc(l.leader)}</strong></p><p class="service-practice">${esc(l.practice.short)}</p></div>${keyNotice(l)?`<div class="lineup-notice"><span>${esc(keyNotice(l))}</span></div>`:''}<div class="lineup-tabs" role="tablist" aria-label="Line-up sections">${['songs','team','details'].map(tab=>`<button type="button" role="tab" id="lineup-tab-${tab}" data-lineup-tab="${tab}" aria-controls="lineup-panel-${tab}" aria-selected="${tab===active}" tabindex="${tab===active?0:-1}">${tab[0].toUpperCase()+tab.slice(1)}</button>`).join('')}</div>${Object.entries(panels).map(([tab,body])=>`<section id="lineup-panel-${tab}" role="tabpanel" aria-labelledby="lineup-tab-${tab}" tabindex="0" class="scroll lineup-panel" ${tab===active?'':'hidden'}>${body}</section>`).join('')}</div></section>`;
    },
    song(id,parent,back,actions,slot){const s=song(id);if(!s)return null;const selected=lineup(parent);const uses=(selected?[selected]:snapshot.lineups).flatMap(l=>(slot==='closing'&&selected?[{...l.closing,role:'Closing song',notes:[l.closing.note]}]:l.songs).filter(x=>x.id===id).map(item=>({l,item})));return `<section class="screen tool ministry-tool">${heading(s.title,s.category,actions,back)}<div class="surface scroll ministry-content"><section class="evidence-alert"><h2>Song information</h2><p>${esc(s.description||'')}</p><p>Lyrics and chord charts have not been supplied here. Transposition is unavailable until a source chart and key are established.</p></section>${uses.map(({l,item})=>`<section class="ministry-block"><div class="section-heading"><h2>${esc(dateText(l.date))}</h2>${badge(item.key?'Performance key '+item.key+(item.keyState==='inferred'?' · inferred':''):'Performance key unknown',item.key?'neutral':'unknown')}</div>${item.role?`<p>${esc(item.role)}</p>`:''}${arrangements(item)}${notes(item.notes)}<button class="secondary" data-route="lineup/${esc(l.id)}">View this line-up ${icon('arrow')}</button></section>`).join('')}${s.references?.length?`<section class="ministry-block"><h2>Reference recordings</h2>${links(s.references)}<p class="muted">A reference recording does not establish the service performance key.</p></section>`:''}${notes(s.notes)}${selected?sourceDetails(selected):''}</div></section>`;},
    resources(){return `<section class="screen tool ministry-tool">${heading('Worshipper Resources','Reviewed material, rehearsal resources and preparation.')}<div class="surface scroll ministry-content"><p class="caption">${esc(snapshot.reviewLabel)} · ${esc(snapshot.timezone)}</p>${snapshot.events.map(e=>`<section class="resource-card"><span class="eyebrow">${esc(dateText(e.date))}</span><h2>${esc(e.title)}</h2><p><strong>${esc(e.timing)}</strong></p>${badge('Exact time and location unknown','unknown')}<ul>${e.songIds.map(id=>`<li><button class="text-link" data-route="song/${esc(id)}">${esc(song(id).title)}</button></li>`).join('')}</ul>${notes(e.notes)}${links(e.links)}</section>`).join('')}${snapshot.resources.map(r=>`<section class="resource-card"><h2>${esc(r.title)}</h2><p>${esc(r.description)}</p>${r.steps?.length?`<ol>${r.steps.map(s=>`<li>${esc(s)}</li>`).join('')}</ol>`:''}${links(r.links)}${notes(r.notes)}</section>`).join('')}<section class="evidence-alert"><h2>Needs clarification</h2>${notes(snapshot.pending)}</section><details class="source-details"><summary>Evidence boundaries</summary>${notes(snapshot.evidenceNotes)}</details></div></section>`;}
  };
}
