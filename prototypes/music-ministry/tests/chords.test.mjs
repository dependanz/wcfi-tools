import {test} from 'node:test';
import assert from 'node:assert/strict';
import {transpose, tokens, pitch, isChord} from '../dist/chords.mjs';
import {songs} from '../dist/songs.mjs';

test('transpose roots, minor/extended qualities and slash bass',()=>{
  assert.equal(transpose('D/F#','G','D'),'A/C#');
  assert.equal(transpose('Bm7','G','Bb'),'Dm7');
  assert.equal(transpose('F#m7b5/C#','G','F'),'Em7b5/B');
  assert.equal(transpose('Am','Am','Cm'),'Cm');
  assert.equal(transpose('N.C.','G','D'),'N.C.');
  assert.equal(transpose('G7sus4','G','Bb'),'Bb7sus4');
});
test('all twelve intervals wrap correctly',()=>{
  const keys=['C','Db','D','Eb','E','F','F#','G','Ab','A','Bb','B'];
  for(const from of keys)for(const to of keys){
    assert.equal(pitch(transpose(from,from,to)),pitch(to));
    assert.equal(pitch(transpose(transpose('F#',from,to),to,from)),6);
  }
});
test('chord changes inside a lyric word preserve the word',()=>{
  assert.deepEqual(tokens('[G]Walk a[D]long with [C]me'),[
    {chord:'G',lyric:'Walk',join:false},{chord:'',lyric:'a',join:false},
    {chord:'D',lyric:'long',join:true},{chord:'',lyric:'with',join:false},
    {chord:'C',lyric:'me',join:false}
  ]);
  assert.deepEqual(tokens('[G] [D/F#] [Em7]').map(p=>p.chord),['G','D/F#','Em7']);
});
test('every demo lyric survives chord parsing exactly, modulo whitespace',()=>{
  for(const song of songs)for(const section of song.sections)for(const line of section.lines){
    const actual=tokens(line).filter(p=>p.lyric).map((p,i)=>(i&&!p.join?' ':'')+p.lyric).join('');
    assert.equal(actual,line.replace(/\[[^\]]+\]/g,'').trim().replace(/\s+/g,' '));
  }
});


test('parenthesized chords validate without weakening malformed chord checks',()=>{
 for(const chord of ['(C#m)','(D/F#)','(B7(b9))','B7(b9)','(C♯m)'])assert(isChord(chord),chord);
 for(const chord of ['(Hm7)','(C#m','C#m)','((C#m))','()','(C#m)junk'])assert(!isChord(chord),chord);
});
test('parenthesized roots slash bass and altered extensions transpose intact',()=>{
 assert.equal(transpose('(C#m)','C','D'),'(D#m)');
 assert.equal(transpose('(D/F#)','C','D'),'(E/G#)');
 assert.equal(transpose('(B7(b9))','C','D'),'(C#7(b9))');
 assert.equal(transpose('(C#m)','C','C'),'(C#m)');
 assert.equal(tokens('[(C#m)]Example')[0].chord,'(C#m)');
});

test('no-chord markers validate and remain unchanged through parsing and transposition',()=>{
 for(const marker of ['N.C.','NC','N.C','NC.','(N.C.)']){
  assert(isChord(marker),marker);
  assert.equal(tokens('['+marker+']Example')[0].chord,marker);
  for(const key of ['C','Db','D','Eb','E','F','F#','G','Ab','A','Bb','B'])assert.equal(transpose(marker,'G',key),marker);
 }
 for(const invalid of ['N.C.m','N.C./G','N.C..','N','(N.C.'])assert(!isChord(invalid),invalid);
});
