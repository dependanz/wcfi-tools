// Original demonstration text and illustrative arrangements, authored for this UX prototype.
// These are not an actual WCFI repertoire, service plan, or published musical arrangements.
export const songs = [
  {id:'morning-mercy',title:'Morning Mercy',category:'Worship',key:'G',bpm:72,meter:'4/4',color:'sage',description:'An unhurried song for gathering together.',sequence:'Verse 1 · Chorus · Verse 2 · Chorus · Bridge · Chorus',sections:[
    {label:'Intro',lines:['[G] [D/F#] [Em7] [Cadd9]']},
    {label:'Verse 1',lines:['[G]Light arrives across the [D/F#]window,','[Em7]Quiet footsteps in the [Cadd9]hall.','[G]We bring all our scattered [D/F#]stories;','[Cadd9]There is room here for us [G]all.']},
    {label:'Chorus',lines:['[Cadd9]Morning mercy, [G]meet us here,','[D]Teach our restless hearts to [Em7]hear.','[Cadd9]In the stillness, [G/B]in the song,','[Am7]You have held us [D]all a[G]long.']},
    {label:'Verse 2',lines:['[G]Every voice becomes a [D/F#]welcome,','[Em7]Every silence has a [Cadd9]place.','[G]Let the hope we share this [D/F#]morning','[Cadd9]Leave our hands as acts of [G]grace.']},
    {label:'Bridge',lines:['[Em7]One small light, [D]then another,','[Cadd9]One more voice joins [G]in.','[Em7]Teach us how to [D]love each other,','[Cadd9]Let that be where we be[D]gin.']}
  ]},
  {id:'a-place-at-the-table',title:'A Place at the Table',category:'Gathering',key:'D',bpm:92,meter:'4/4',color:'ochre',description:'A bright, welcoming opening song.',sequence:'Verse 1 · Chorus · Verse 2 · Chorus',sections:[
    {label:'Intro',lines:['[D] [G] [Bm7] [A]']},
    {label:'Verse 1',lines:['[D]Open the door, let the [G]daylight in,','[Bm7]Pull up a chair, let the [A]song begin.','[D]There is a story in [G]every face,','[Em7]There is a welcome in [A]this small place.']},
    {label:'Chorus',lines:['[G]A place at the [D]table, a [A]voice in the [Bm7]song,','[G]A hand for the [D/F#]journey when the [Em7]road feels [A]long.','[G]Gather us [D]closer, [A]teach us to [Bm7]see,','[G]The grace we have [A]found is the grace we can [D]be.']},
    {label:'Verse 2',lines:['[D]Bring what you carry, the [G]joy and the ache,','[Bm7]Bring all the questions that [A]keep you awake.','[D]Here in the ordinary, [G]here in the small,','[Em7]Love sets a place at the [A]table for all.']}
  ]},
  {id:'steady-light',title:'Steady Light',category:'Praise',key:'C',bpm:108,meter:'4/4',color:'clay',description:'A joyful song with a simple, repeated refrain.',sequence:'Verse 1 · Chorus · Verse 2 · Chorus · Ending',sections:[
    {label:'Verse 1',lines:['[C]When the morning opens [F]wide,','[Am]We will walk with hope be[G]side.','[C]Every step and every [F]mile,','[Dm7]Give us courage for a [G]while.']},
    {label:'Chorus',lines:['[F]Steady light, [C]burning bright,','[G]Guide us through the [Am]longest night.','[F]Hands held high, [C/E]hearts made new,','[Dm7]All our roads lead [G]back to [C]You.']},
    {label:'Verse 2',lines:['[C]In the city, in the [F]field,','[Am]Teach our busy hands to [G]build.','[C]Little kindness, patient [F]care,','[Dm7]Let our living be a [G]prayer.']},
    {label:'Ending',lines:['[F]Steady light, [C]steady light,','[Dm7]Keep our hearts a[G]wake to[C]night.']}
  ]},
  {id:'still-waters',title:'Still Waters',category:'Prayer',key:'Am',bpm:64,meter:'6/8',color:'blue',description:'A quiet prayer with space between the phrases.',sequence:'Verse 1 · Refrain · Verse 2 · Refrain',sections:[
    {label:'Intro',lines:['[Am] [Fmaj7] [C/G] [E7]']},
    {label:'Verse 1',lines:['[Am]Set down the weight of the [Fmaj7]day,','[C]Let all the noise fall a[E7]way.','[Am]Here where the waters are [Fmaj7]still,','[Dm]Quiet our hearts, if You [E7]will.']},
    {label:'Refrain',lines:['[Fmaj7]Peace like a [C]river,','[Dm]Grace like the [Am]rain,','[Fmaj7]Hold us to[C/G]gether,','[E7]Make us whole a[Am]gain.']},
    {label:'Verse 2',lines:['[Am]Gather the words we cannot [Fmaj7]say,','[C]Carry our worries a[E7]way.','[Am]Give us the patience to [Fmaj7]be,','[Dm]Open our eyes so we [E7]see.']}
  ]},
  {id:'hands-open',title:'Hands Open',category:'Worship',key:'E',bpm:80,meter:'4/4',color:'rose',description:'A gentle arrangement for reflection and response.',sequence:'Verse 1 · Chorus · Verse 2 · Chorus',sections:[
    {label:'Verse 1',lines:['[E]Here are the plans we have [B/D#]carried,','[C#m7]Here are the things we hold [Aadd9]tight.','[E]Give us the courage to [B/D#]offer','[Aadd9]All of our days to the [E]light.']},
    {label:'Chorus',lines:['[Aadd9]Hands open, [E]hearts ready,','[B]Feet upon the [C#m7]ground.','[Aadd9]Love, make us [E/G#]more steady,','[F#m7]Let Your peace be [B]found.']},
    {label:'Verse 2',lines:['[E]Here are the people be[B/D#]side us,','[C#m7]Here is the work we can [Aadd9]do.','[E]Show us the ordinary [B/D#]places','[Aadd9]Where we can start some[E]thing new.']}
  ]},
  {id:'carry-the-song',title:'Carry the Song',category:'Sending',key:'F',bpm:96,meter:'4/4',color:'purple',description:'A sending song to take beyond these walls.',sequence:'Verse 1 · Chorus · Verse 2 · Chorus',sections:[
    {label:'Verse 1',lines:['[F]When we go beyond this [Bb]door,','[Dm7]May we listen a little [C]more.','[F]May the words we sang to[Bb]day','[Gm7]Find a home along the [C]way.']},
    {label:'Chorus',lines:['[Bb]Carry the [F]song into [C]Monday [Dm7]morning,','[Bb]Carry the [F/A]hope into [Gm7]every [C]room.','[Bb]Where there is [F]hurt, let a [C]kindness be [Dm7]forming,','[Bb]Where there is [C]darkness, let [F]mercy bloom.']},
    {label:'Verse 2',lines:['[F]In the work and in the [Bb]rest,','[Dm7]Let us give our simple [C]best.','[F]Every neighbour, every [Bb]friend,','[Gm7]Let the welcome never [C]end.']}
  ]}
];
export const demoSet = [{id:'a-place-at-the-table',key:'D'},{id:'morning-mercy',key:'G'},{id:'carry-the-song',key:'F'}];
