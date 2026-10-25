export const isChord = value => /^(?:[A-G][#b♯♭]?(?:(?:maj|min|m|dim|aug|sus|add|M|Δ|ø|°|\+|-)?\d*(?:[#b♯♭]\d+)?(?:sus[24]|add\d+)?)(?:\((?:[#b♯♭]?\d+)(?:,[#b♯♭]?\d+)*\))?(?:\/[A-G][#b♯♭]?)?|N\.?C\.?)$/.test(value);
const expand = (line,tab) => {let col=0;return Array.from(line).map(c=>{if(c==='\t'){const spaces=tab-col%tab;col+=spaces;return ' '.repeat(spaces);}col++;return c;}).join('').replaceAll('\u00a0',' ');};
function chordRow(line){
 const matches=[...line.matchAll(/\S+/gu)];
 if(!matches.length||!matches.every(m=>isChord(m[0])||/^[|:]+$/.test(m[0])))return null;
 const chords=matches.filter(m=>isChord(m[0]));
 return chords.length?chords.map(m=>({chord:m[0],pos:Array.from(line.slice(0,m.index)).length})):null;
}
const heading=line=>/^\s*(?:\[[^\]]+\]|(?:verse|chorus|bridge|intro|outro|interlude|pre[- ]?chorus|ending|tag)(?:\s+\d+)?\s*:?)\s*$/i.test(line);
export function convert(text,{direction='inline',tabSize=4,snap=false}={}){
 const lines=text.replace(/\r\n?/g,'\n').split('\n').map(l=>expand(l,Number(tabSize)||4)),out=[],warnings=[];let count=0;
 if(direction==='broken'){
  let joined='';
  const flush=()=>{if(joined){out.push(joined.trimEnd());joined='';}};
  for(const line of lines){
   if(!line.trim()){flush();if(out.length&&out.at(-1)!=='')out.push('');continue;}
   const row=chordRow(line);
   if(row){joined+=row.map(x=>`[${x.chord}]`).join('');count+=row.length;continue;}
   if(heading(line)){flush();out.push(line);continue;}
   // Keep fragment-edge spaces: missing space can mean a chord inside a word.
   joined+=line;
  }
  flush();
  if(text.trim())warnings.push('Check word spacing and line breaks: broken web copies cannot always preserve the original wrapping.');
 }else if(direction==='above'){
  lines.forEach((line,i)=>{
   const matches=[...line.matchAll(/\[([^\]]+)\]/g)];
   if(!matches.length||!matches.some(m=>isChord(m[1]))){out.push(line);return;}
   if(matches.some(m=>!isChord(m[1]))){warnings.push(`Line ${i+1}: unrecognised bracket text; left unchanged.`);out.push(line);return;}
   let lyrics='',chords='',cursor=0;
   for(const match of matches){lyrics+=line.slice(cursor,match.index);let pos=Array.from(lyrics).length;
    if(chords.length>pos){lyrics+=' '.repeat(chords.length-pos+1);pos=lyrics.length;}
    chords=chords.padEnd(pos,' ')+match[1];cursor=match.index+match[0].length;count++;
   }
   lyrics+=line.slice(cursor);out.push(chords.trimEnd());if(lyrics.trim())out.push(lyrics);
  });
 }else{
  for(let i=0;i<lines.length;i++){
   const row=chordRow(lines[i]);
   if(!row){out.push(lines[i]);continue;}
   const next=lines[i+1];count+=row.length;
   if(next===undefined||!next.trim()||chordRow(next)||heading(next)){out.push(row.map(x=>`[${x.chord}]`).join(' '));continue;}
   if(row.length===1&&/^[A-G]$/.test(lines[i].trim()))warnings.push(`Line ${i+1}: treated “${lines[i].trim()}” as a chord. Check if it was a lyric.`);
   const chars=Array.from(next),at=new Map();
   for(const {chord,pos:original} of row){let pos=Math.min(original,chars.length);
    if(original>chars.length)warnings.push(`Line ${i+1}: ${chord} is beyond the lyric end; placed at the end.`);
    if(snap&&pos<chars.length){if(/\s/.test(chars[pos])){while(pos<chars.length&&/\s/.test(chars[pos]))pos++;}else{while(pos>0&&!/\s/.test(chars[pos-1]))pos--;}}
    at.set(pos,(at.get(pos)||'')+`[${chord}]`);
   }
   let result='';for(let j=0;j<=chars.length;j++)result+=(at.get(j)||'')+(chars[j]||'');out.push(result);i++;
  }
 }
 return {text:out.join('\n'),count,warnings:[...new Set(warnings)]};
}
