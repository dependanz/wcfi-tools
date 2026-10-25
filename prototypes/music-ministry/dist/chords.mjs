const CHORD=/^[A-G][#b]?(?:(?:maj|min|m|dim|aug|sus|add)?(?:2|4|5|6|7|9|11|13)?(?:[#b](?:5|9|11|13))?(?:sus[24]|add(?:2|4|9|11|13))?)(?:\((?:[#b]?\d+)(?:,[#b]?\d+)*\))?(?:\/[A-G][#b]?)?$/;
export function isChord(value) {
  const inner=value.startsWith("(")&&value.endsWith(")")?value.slice(1,-1):value;
  return /^N\.?C\.?$/.test(inner)||CHORD.test(inner.replaceAll("♯","#").replaceAll("♭","b"));
}
const PITCH = {C:0,D:2,E:4,F:5,G:7,A:9,B:11};
const SHARPS = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
const FLATS = ['C','Db','D','Eb','E','F','Gb','G','Ab','A','Bb','B'];
export const KEYS = ['C','Db','D','Eb','E','F','F#','G','Ab','A','Bb','B'];
export function pitch(note) {
  const m = /^([A-G])([#b♯♭]?)/.exec(note);
  if (!m) throw new Error(`Invalid note: ${note}`);
  return (PITCH[m[1]] + (['#','♯'].includes(m[2]) ? 1 : ['b','♭'].includes(m[2]) ? -1 : 0) + 12) % 12;
}
export function transpose(chord, from, to, spelling='auto') {
  if(chord.startsWith('(')&&chord.endsWith(')'))return '('+transpose(chord.slice(1,-1),from,to,spelling)+')';
  const interval = (pitch(to) - pitch(from) + 12) % 12;
  const flat = spelling === 'flats' || (spelling === 'auto' && ['F','Bb','Eb','Ab','Db','Gb','Dm','Gm','Cm','Fm','Bbm','Ebm'].includes(to));
  const names = flat ? FLATS : SHARPS;
  const root = (value) => value.replace(/^([A-G])([#b♯♭]?)/, (m) => names[(pitch(m) + interval) % 12]);
  // Preserve chord qualities/extensions; transpose an inversion's bass independently.
  return chord.split('/').map(root).join('/');
}
export function tokens(line) {
  const result = [];
  let chord = '', boundary = true;
  for (const match of line.matchAll(/\[([^\]]+)\]|(\s+)|([^\s\[]+)/g)) {
    if (match[1] !== undefined) {
      if (chord) result.push({chord, lyric:'', join:false});
      chord = match[1];
    } else if (match[2] !== undefined) {
      boundary = true;
    } else {
      result.push({chord, lyric:match[3], join:!boundary});
      chord = ''; boundary = false;
    }
  }
  if (chord) result.push({chord, lyric:'', join:false});
  return result;
}
