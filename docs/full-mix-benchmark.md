# Full-mix candidate and selection benchmark

This file-based workflow renders synchronized raw stems, stereo ambience and
bounded channel/bus processing. It never connects to Ableton, an audio device,
the emulator sender, or a cloud API. CLI workers use one logical CPU at idle
priority with numerical-library thread counts set to one. No GPU is used.

```
wcfi audio full-mix-benchmark prepare sources.json --output benchmark --ffmpeg PATH_TO_FFMPEG
wcfi audio full-mix-benchmark run benchmark --output run-01
wcfi audio full-mix-benchmark report benchmark --run-directory run-01 --package app-package
```

Preparation takes a JSON profile with exactly `oct04`, `oct11`, `oct18` sessions
and `bethel`, `maverick` references. Each session names a checksummed bounded
raw-take manifest and a checksummed saved Master WAV plus its output-fader gain.
See `_work/full-mix-benchmark-2026-10-22-01/sources.json` for the real local
profile. Paths refer to local files; no assets are distributed with the code.

All raw channels must share twelve seconds at 48 kHz. Talkback, preacher and
laptop channels are excluded from this music benchmark. Backing microphones
remain conservatively limited because activity versus bleed is unconfirmed.
Source health and the exact gain/EQ/compression/placement recipe are saved.
Recorded clipping is not repaired by lowering source gain.

Eight candidates per date include the saved Master baseline, raw equal-fader
sum, role-balanced dry mix, shaped dry mix, short room, restrained hall, more
hall, and restrained hall with gentle bus compression. The original current
Master is an **earlier saved capture**, not the historical stream or the latest
October 11 seven-source local working preview. Local processors are not claimed
to emulate particular Ableton plug-ins.

Ambience uses deterministic stereo convolution, 25 ms predelay, high-pass/low-pass
filtering, and 0.7/1.4 second decays. Sends come from lead, KIT, guitars and keys;
BASS and uncertain backing microphones have no send. Wet return RMS is set
relative to the dry mix. Periodic warmup uses the known bounded loop, not an
unrecorded real-room tail. All complete mixes and both reference excerpts are
verified at −24 LUFS and below −3 dBTP. This is an audition level, not a broadcast
loudness prescription. Each entire mix gets one scalar; internal balance remains.

## Frozen selection policy and challenge set

`policy.json`, opaque `blind-inputs.json`, and a separate `private-key.json` are
checksummed before scoring. `run` reads only the first two and the verified audio,
never the private key or candidate names. Preparation seed fixes shuffling.
Reports unblind labels **after** inference. No policy was tuned after viewing the
real results. These are reproducible engineering rules, not a trained model.

The selector combines distance from reference ranges for low-mid/presence,
sub/presence, highs/presence, crest and stereo side energy with broad source-balance
and residual penalties. Nonnegative fitting against the known raw sources is a
proxy, not separation, intelligibility measurement or reliable wet/dry inference.
Bleed, EQ, dynamics and timing differences can bias it. Technical guardrails check
plateaus, dropouts, tonal hum, mono cancellation and large unexplained residuals.
Lower cost means closer to those rules, not more professional sound.

Ten controls per session comprise identical audio, gain-only audio and eight
deliberate alterations: lead −18 dB, KIT −18 dB, acoustic +18 dB, 2 kHz low-pass,
60 Hz hum/harmonics, severe clipping, excessive reverb, and an antiphase width
fault. Controls derive from the same shaped short-room anchor. The ambience send
is held fixed in source-level controls, so a reduced vocal/KIT retains its wet
contribution. Exact null ties, repeatability, input order and separate-reference
choice agreement are also tested. All 37 checks must pass; a degradation must
either be technically rejected or cost at least 0.05 more than its anchor.

Even passing this gate **cannot validate professional mixing judgment**. These
three dates and the two reference excerpts are already-examined development data.
Holding out one reference tests reference sensitivity, not generalization to a
new recording session. No independent same-stem professional mix or expert
preference labels are available. `professional_quality_judgment_validated` stays
false. Failed controls withhold automatic recommendations for every date. The
app importer provides no host apply action.

## First frozen run: October 22, 2026

Artifacts: `_work/full-mix-benchmark-2026-10-22-01/benchmark`, `run-01`, `package`.
54 versions plus two reused reference excerpts. Scoring took **19.67 seconds**
on one CPU, without a learned audio model, GPU, upload or device/DAW connection.

**35/37 checks passed. Gate failed.** All nulls/repeats/order checks passed, both
references selected the same candidate per date, and clipping/phase guards worked.
The exceptions were both October 11:

* Raising the acoustic guitar by 18 dB **improved** the heuristic cost by 0.1559.
  The selector can reward a serious balance change when aggregate spectral/style
  features move closer to the references.
* Added hum worsened cost by 0.0486, below the frozen 0.05 separation margin.
  It was directionally recognized but not confidently separated by this rule.

The top feature candidates were more-hall on October 4/11 and short-room on
October 18. These are **unvalidated candidates, not accepted better mixes**.
All recommendations were withheld and no host processing was applied. This run
demonstrates why reference feature proximity alone cannot select production
quality reliably. Keep these failures as regression cases for a future evaluator;
do not lower the gate or change weights to turn this run into a success.
