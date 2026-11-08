import test from 'node:test';import assert from 'node:assert/strict';import {convert,isChord} from '../dist/converter.mjs';
test('exact positions and altered slash chords',()=>{assert.equal(convert('G    D/F#\nHello world').text,'[G]Hello[D/F#] world');assert.equal(convert('B7(b9) Em7\n123456789abc').text,'[B7(b9)]1234567[Em7]89abc');});
test('headers blanks and instrumental chords',()=>{assert.equal(convert('Verse 1\nG\n\nChorus\nC    G').text,'Verse 1\n[G]\n\nChorus\n[C] [G]');});
test('tabs CRLF and plain lyrics',()=>{assert.equal(convert('\tG\r\n12345678',{tabSize:4}).text,'1234[G]5678');assert.equal(convert('A song of hope\nWe sing on').text,'A song of hope\nWe sing on');});
test('ambiguous and overflow review notes',()=>{assert.equal(convert('A\nword').warnings.length,1);assert(convert('       G\nHi').warnings.some(w=>w.includes('beyond')));});
test('word snap and Unicode accidental',()=>{assert.equal(convert('  F♯m\nHello',{snap:true}).text,'[F♯m]Hello');assert(isChord('B7(b9)'));assert(!isChord('Hm7'));});
test('roundtrip and unknown preservation',()=>{const input='[G]Hello [D]world';assert.equal(convert(convert(input,{direction:'above'}).text).text,input);assert.equal(convert('[Verse 1]\n[Hm7]test',{direction:'above'}).text,'[Verse 1]\n[Hm7]test');});

test('broken copy preserves word spaces and midword chord placement',()=>{
 const result=convert('G \nMorning \nD/F# \nlight\n\nWe re\nEm7\nturn home',{direction:'broken'});
 assert.equal(result.text,'[G]Morning [D/F#]light\n\nWe re[Em7]turn home');assert.equal(result.count,3);assert.equal(result.warnings.length,1);
});
test('broken copy preserves headings adjacent chords and trailing chords',()=>{
 assert.equal(convert('Verse 1\r\nB7(b9) Em\r\nHello \r\nC\r\nworld\r\nChorus\r\nG\r\n\r\nD/F#',{direction:'broken'}).text,'Verse 1\n[B7(b9)][Em]Hello [C]world\nChorus\n[G]\n\n[D/F#]');
});
test('broken copy preserves unknown text and empty input',()=>{
 assert.equal(convert('Hm7\n word',{direction:'broken'}).text,'Hm7 word');assert.deepEqual(convert('',{direction:'broken'}),{text:'',count:0,warnings:[]});
});
