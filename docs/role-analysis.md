# Role-aware rehearsal analysis

`wcfi audio analyze` provides a local RED/KIT analysis engine, recorded replay,
passive node observation, and processing **proposals**. It does not apply settings
to Ableton. No command in this group starts sources, presses transport controls,
opens a sound output, uses the mouse, loads a neural model, or uploads audio.

The new profiles are separate from the older `soundcheck calibrate` octave-shape
and level presets. They do not force inputs toward a universal spectrum or boost
quiet microphones to a fixed level.

## Profiles and decisions

Run `wcfi audio analyze profiles` to inspect the versioned profiles.

| Profile | Intended input | Enabled analyses |
| --- | --- | --- |
| `red` | Lead singer's microphone, with possible band bleed | Signal health, headroom, energy-level distribution, candidate short HF bursts |
| `kit` | Complete premixed stereo drum feed | Signal health, headroom, energy attacks/dynamics, brightness descriptor, stereo health |

Track intent is authoritative; it is not inferred from whichever instrument is
loudest in an observation. Energy activity is explicitly **not** verified singing
activity. KIT never runs the vocal sibilance module.

Version 1 proposals:

- **Software attenuation** can meet a default −6 dBFS sample-peak ceiling, within
  a 12 dB maximum change. This is an engineering headroom constraint, not a target
  for track loudness or musical balance. There is no automatic boost.
- **HF-burst reduction** is an experimental listening preview, limited to 3 dB.
  Its short-event detector is not a validated sibilance classifier. When aligned
  KIT context is provided, strong coincident KIT HF activity suppresses this
  preview as bleed-ambiguous. Coincidence does not prove the source of the sound.
- KIT descriptors do not automatically add compression or EQ. In aligned pre/post
  comparisons, a crest-factor drop exceeding 3 dB prompts review of dynamics;
  it does not establish that the processing sounds worse.

Exact full-scale samples and possible scaled flat tops produce notes. The cause
and physical location of clipping are not established by these observations.
Floating-point samples above full scale are measured separately and are not
automatically called clipped. Attenuation preserves existing waveform damage.

## Replaying a take

```powershell
wcfi audio analyze take '.\real-sources\2026-10-18-095544' `
  --start 29 --seconds 20 --previews --output '.\_work\role-replay-01'
```

The take manifest selects RED and KIT by exact name, checks their original file
hashes, and reads the same sample range from both. Original files are read-only.
The post-effects reference here is **bypass**, not the current Ableton chain.
Upstream console processing of the recorded sources is not established.

Outputs include per-track JSON, source provenance, a local `report.html`, and
optional original-level and RMS-matched audition WAVs. Reference preview DSP is
not an emulation of Ableton Compressor, Pro-Q or Pro-DS. RMS matching is not LUFS
matching or a guarantee of equal perceived loudness. No audio plays automatically.
Previous result directories are never overwritten.

For saved node captures:

```powershell
wcfi audio analyze file 'RED-before.wav' --after 'RED-after.wav' `
  --profile red --seconds 16 --aligned --chain-state 'chain-snapshot.json' `
  --previews --output '.\_work\red-pair-01'
```

Use `--aligned` only when timing is established externally. Otherwise the engine
reports each stage independently and makes no paired-change attribution. Device
order/settings can be supplied as JSON; the report fingerprints that supplied
state and does not claim to inspect plug-ins from filenames.

If `wcfi` is unavailable in the shell, replace it with
`.\.venv\Scripts\python.exe -m wcfi_tools.cli` from the repository directory.

## Continuous observations

```powershell
wcfi audio analyze watch --seconds 60 --window 10 --interval 5 `
  --output '.\_work\role-watch-01'
```

This passively discovers unique RED/KIT before/after Analysis Node pairs already
receiving audio. It does not start the emulator, start playback, install nodes or
change routing. Capture runs on a separate thread from analysis and read-only
Ableton bridge inspection. Only JSON is saved; bounded audio history stays in RAM.

The same `RoleAnalysisStream` accepts recorded chunks for deterministic replay.
It handles common host sample positions, circular history, gaps, epoch changes,
and observed chain-state changes. It suppresses completed proposals when their
state becomes stale. Watch mode conservatively estimates waveform correspondence;
weak/repetitive matches keep paired-change attribution unavailable. An estimate
is not a measurement of physical latency or a plug-in's declared latency.

Host-exposed chain/mixer state is polled, not sample-atomic. Hidden proprietary
state is unknown. A chain-end node precedes the track fader/pan; final stream mix
balance requires additional context. No observation here authorizes live writes.

CLI workers use one logical CPU at idle priority on Windows, single-threaded
FFTs and bounded windows. This affects only the analysis process. Node capture
and heavy analysis must stay outside the audio callback.

## Controlled tests on Sunday’s stems

```powershell
wcfi audio analyze validate-take '.\real-sources\2026-10-18-095544' `
  --start 29 --seconds 10 --output '.\_work\role-validation-01'
```

This freezes a protocol before analysis and checks identity, silence, quiet inputs,
known gain changes, clipping compensation, synthetic HF perturbations, drum
transient flattening and stereo cancellation. The HF perturbations are **not**
human-labelled sibilants. Independent band measurements check the preview's effect.
Passing these checks establishes limited numerical behavior, not professional
mixing, general detection accuracy or human preference. Exit code 2 means a
controlled check failed.

The October 21 development run is preserved in
`_work/role-analysis-2026-10-21-01/`, including five 20-second Sunday windows,
archived actual RED/KIT node pairs, controlled tests and rolling replay results.
`review.html` indexes the final `acceptance-02` results: **23/23** controlled
numerical checks passed. The full project suite passed **182 tests** and lint
passed. `human-review.html` contains five randomized listening pairs, including
two byte-identical controls; these judgments are still pending. In the 29–49 s
original KIT window, the detector counted 14,891 full-scale samples. Its −6 dB
reference gain preview preserves that source damage while adding headroom.
Live host capture needs further testing; the CLI watcher has mocked capture and
bridge regression coverage. Voice separation, validated singer activity,
hum removal, return/Master profiles and broader processing adapters remain
subsequent milestones.

## Rehearsal lab in the app

Run `wcfi audio emulator launch` and choose **Rehearsal lab**. The same interface
is available through `launch --web`. It does not play audio automatically.

1. Choose **Sunday’s raw stems**, a take, start time and 0.5–20 second window.
   **Analyse stems** reads the original files without feeding Ableton. Raw and
   bypass copies are labelled explicitly; neither represents Live’s current chain.
2. Use the comparison buttons to hear captured audio or a local reference
   preview through the system default output. Listening defaults to −18 dB and
   loops one audition at a time. Stop or leave this page to silence that copy.
   Switching variants preserves the approximate listening position. RMS matching
   applies only to the After/Experimental timbre pair, never the gain-only copy.
3. To evaluate actual processing, send inputs using the existing emulator controls
   or supply live inputs, then choose **Current Ableton nodes → Capture & analyse**.
   The app captures unique RED/KIT before/after pairs from one host. When Live
   provides a shared sample clock, it includes KIT context for RED’s HF candidates.
   With transport stopped, some Live configurations omit both continuous sample
   time and host system time. In that case the app captures each node’s own
   continuous window, marks pair timing unverified and withholds RED’s HF treatment
   because cross-track bleed context is unavailable. Independent windows still
   support per-stage headroom measurements; they do not establish phase or latency.
   Native writer-contention responses are retried with the same cursor; actual
   dropped observations still invalidate the accumulated window. It never starts sources,
   changes routing, presses Play/Record, or uses desktop controls. Gaps reset the
   window; missing input times out after 45 seconds. Before/after chain snapshots
   must match, but these are not sample-atomic and cannot reveal hidden plug-in state.
4. **Apply gain in Ableton** supports negative software gain through the Output
   Gain parameter of an enabled Ableton Compressor immediately before the final
   Analysis Node. The compressor must be fully wet, automatic makeup off, and the
   control enabled and unautomated. Both nodes must bracket the chain. Other
   processors show the setup limitation instead of receiving a guessed parameter.
   Raw-file studies cannot authorize an application. The separate gain-only preview
   matches this supported action; the combined experimental preview may also include
   the HF treatment.
5. Apply rechecks the captured host, session, track/device/parameter state and asks
   Live to verify the dB display in a dry run. The host also checks the expected
   parameter value at write time. Proposals expire after ten minutes; an attempted
   application is persisted before the write and cannot be blindly retried after
   a lost response. Other chain parameters are inspected, not locked against manual
   edits. The parameter readback confirms a write, not the resulting audio.
   Choose **Capture nodes to verify** afterward. Ableton Undo can restore the change.

HF burst processing remains **preview-only**: it is an event-based offline
reference processor, not a signal-driven live de-esser or Pro-DS equivalent. No
vocal boost, EQ or drum compressor setting is automatically selected. A measured
headroom improvement does not establish musical improvement or repair source clipping.

Analysis runs in one separate child process, limited to one logical CPU at idle
priority on Windows, with single-threaded numeric libraries and no GPU/cloud.
At most one job runs; the three latest captures, three latest KIT studies and three
latest full-mix reference comparisons, with finite WAV auditions, are
retained locally under the emulator state directory’s `rehearsal-review` folder.
Cancel and service shutdown stop only this feature’s worker. Authenticated audition
endpoints accept job/track/variant identifiers, never arbitrary file paths or URL tokens.
The desktop and shared web assets need rebuilding after frontend changes; restart
an older running service to load the new API.

## KIT in the full mix

The app can publish a prepared **KIT EQ/compression study** in its review history.
Open it with **Rehearsal lab → Saved comparisons → Open KIT comparison**.
This shortcut stays visible above the capture controls, even when newer node captures
are selected. Captures and comparisons have separate retention budgets, including
after restarting the service; new captures do not remove the saved comparisons.
Newer passage studies include **Compressor bypass** as a fifth version and a
passage selector for quieter, louder and busy playing. The passage labels carry
their selection evidence: energy and attack-pattern heuristics do not establish
that a passage is a chorus or a fill. Native bypass is captured with the device
disabled and checked sample-for-sample against the pre-effects input. This also
bypasses its Output Gain, so unmatched bypass playback has a different level.

With **Match KIT loudness for comparison** enabled, each isolated KIT is EBU R128
matched. In the full mix, each KIT contribution is matched to Current chain, the
other tracks' waveform is held fixed, and one common safe audition gain follows.
This controls the drum-level confound instead of normalizing the whole mix
independently for each version. Whole-mix integrated loudness can differ slightly.
Both native attack and bypass differences must pass independent linear mix null
checks before this substitution is allowed. Files are scanned after matching to
verify KIT loudness and true-peak headroom. All captures remain local; no transport
or desktop controls are used.

Schema 1 packages retain their original four-version matching behavior; schema 2
requires all twenty allowlisted audio variants and bounded passage metadata.
Prepared studies remain a development workflow; **Capture & analyse** does not
automatically conduct the temporary device changes or choose a musical winner.

Original four-version studies offer Current chain, Slower attack, Less low end, and Attack + EQ, with a
Full mix / KIT on its own switch. Integrated loudness matching defaults on:
FFmpeg EBU R128 measurements are used to match the four versions at −24 LUFS
within the scanner’s 0.1 LU precision. This is an audition level, not a broadcast
target. Original-balance copies share a safety attenuation; node measurements
remain separate from all audition gains. Changing versions keeps the listening
position within the sample-aligned loop. Nothing plays automatically.

The October 21 study used the same 29–49 second Sunday passage. Current and
15 ms attack versions were captured from the actual KIT and Master nodes with
Live stopped. Only the native Compressor Attack was temporarily changed from
1 ms to 15 ms. Its exact previous value was restored afterward, and the complete
host-exposed session was checked against the baseline. Threshold −6 dB, ratio
2:1, release 100 ms, and Output Gain −5.877 dB were retained.

The EQ alternative is a **local reference preview**, a −2 dB low shelf at 120 Hz
after compression. It is not a Pro-Q render or a new effect applied to Ableton.
The full-mix EQ preview replaces only the captured KIT contribution, and is
allowed only when its routing is centred, unity, without sends or group effects.
An independent null check must also verify that the measured native full-mix
change agrees with the changed KIT signal at that point. Cyclic alignment is
allowed only for complete repeated passages; it is not a device latency measure.
The current study’s relative null residual was 0.0188%. It does not establish a
musical improvement or make this method valid through a nonlinear Master chain.

These full mixes are captured at the Master node **before its −6 dB fader**.
Positive floating-point node values do not, by themselves, establish clipping
at outputs 23/24. Source clipping in the KIT recording remains an upstream limit.
Spectral proportions describe the window and do not establish a need for EQ.
No additional compression/EQ is automatically retained based on those numbers.

Prepared packages use `kit_study.build_study` and contain checksummed finite WAVs.
`POST /api/rehearsal/import-study` accepts only a UUID in the feature’s local
`study-staging` directory, validates names, sizes, formats and checksums, and
publishes an immutable review job. It never accepts an arbitrary path or changes
Live. Package preparation and the experimental native capture orchestration are
currently a development workflow; the normal Capture & analyse button remains
read-only. Review artifacts and the restoration journal are in
`_work/kit-context-2026-10-21-01/`.

## Full-mix reference comparison

**Rehearsal lab → Compare the full mix → Open · WCFI ↔ Bethel** opens the Bethel study. The saved
WCFI candidates are the Sunday 110–122, 32–44 and 124–136 second Master-node
captures, originally labelled by energy heuristics. The operator subsequently
confirmed that only **32–44 seconds** is a representative performance; the other
two are musicians jamming. They remain technical KIT studies and are excluded
from musical reference findings and reference audition. The eligible passage is
compared with Bethel’s public July 12, 2026 service at 15:30–15:42. The initially inspected 5:00
passage was a spoken welcome and is excluded. Public captions and a 15:22 video
frame establish the selected worship context; there is no claim of machine
listening or a controlled same-performance comparison.

Each pair of stereo 48 kHz WAVs uses scalar-only EBU R128 loudness matching to −24 LUFS
(or a lower common safe target if required), with final loudness and true-peak
checks. There is no added EQ, limiting or widening. WCFI’s captured Master node
precedes its −6 dB fader; it is not the ATEM/encoded output. Different songs,
arrangements, keys and section densities remain confounds after matching.

The app shows spectral band distributions, ratios, stereo side-energy and
peak/RMS contrast. It separates measured input clipping indicators, repeated
reference differences and listening questions. Screening thresholds select
experiments, not professional-quality targets, EQ amounts or automatic writes.
Original recorded source descriptors are not additive contribution percentages
because bleed/correlation and downstream processing affect the sum. A source’s
RMS or spectrum alone does not establish vocal masking or reverb quality.

**Performance selection** records operator eligibility and reasons through
`POST /api/rehearsal/assess-reference`. Decisions are persisted separately from
the captured evidence and recompute findings using only eligible windows and
their source measurements. New packages require performance confirmation. One
eligible passage produces explicitly provisional single-window differences,
never a repeated-performance or three-pair claim. Excluded reference audio is
blocked by the authenticated audition endpoint. Linked KIT studies are labelled
from the same decision only when their restored snapshot timestamp and exact
window match this reference's provenance. Jam passages can diagnose technical
signal behavior but cannot establish musical mix quality.

Prepared `wcfi_reference_package` files use a separate bounded local
`reference-staging/<uuid>` directory. `POST /api/rehearsal/import-reference`
checks allowlisted filenames, hashes, stereo formats, lengths and declared
matching verification; audition uses authenticated `track=MIX` variants.
Reference retention is independent of KIT studies and ordinary captures.
Updated clients request `/api/rehearsal?include_references=1&reviewed_passages=1&named_references=1`; the default GET
hides this report type from already-open older frontends. Stored reference
listening works with Ableton closed and emulator sending paused. Internal
`serve(start_audio=False)` can start the app service without bringing the native
emulator online. Normal launch behavior remains unchanged.

October 21 evidence, input provenance, matching checks and the headless UI check
are under `_work/bethel-reference-2026-10-21-01/`. No private audio upload, GPU
model, Ableton transport action or desktop interaction is part of this workflow.
The operator correction and its regression/UI checks are under
`_work/reference-performance-review-2026-10-21/`; earlier three-window summaries
are historical and must not be used as current reference conclusions.

### Separate Life.Church and Maverick City examples

The same approved WCFI 32–44 second Master capture is also paired separately
with Life.Church's October 11 archived service, **Rest On Us at 5:12–5:24**, and
TRIBL's **Promises (LIVE AT MAVERICK CITY), featuring Naomi Raine, at 3:12–3:24**.
Official channel metadata and public captions establish singing/music context;
this is not a perceptual model assessment. Life.Church uses the saved public
excerpt already held locally. Only a bounded 36-second public Maverick City
excerpt was downloaded. Neither preparation nor audition uploads church audio.

Both new pairs are verified at −24.0 LUFS using scalar gain only. Their WCFI
source hashes are identical to the approved Bethel study's source capture.
Reusing that approval establishes WCFI passage eligibility, not an endorsement
of the references' aesthetics or evidence across additional WCFI performances.
Maverick City is explicitly labelled a released live performance, rather than a
verified Sunday broadcast; editing, overdubs and mastering remain unknown.

Reference package schema 2 supports one to three distinct allowlisted passage
pairs, a reference name and performance type, and generic `wcfi` / `reference`
audio variants. Schema 1 Bethel packages remain readable. Each reference keeps
independent findings, attribution, eligibility and matched audio. The three
reference jobs have a separate three-job retention budget, preserving KIT
studies and ordinary node captures. `named_references=1` is required to expose
the new schema to HTTP clients, so older open frontends cannot mislabel it as
Bethel or attempt to read nonexistent Bethel variants.

Source metadata, selected public captions, checksummed packages, measurements,
publication IDs and the private muted headless UI check are in
`_work/additional-references-2026-10-21/`. No new processing settings were applied.

### Measured source balance experiment

**Rehearsal lab → Source balance experiments → EGUITAR −3 dB · full mix**
compares actual native Master captures of the approved Sunday 32–44 second
loop, before and during a bridge-controlled EGUITAR fader change from 0 to
−3 dB. The exact original fader and emulator loop/feed are restored afterward;
the app auditions saved files and does not apply the candidate. Loudness
matching defaults on, with both files verified at −24 LUFS. Switching it off
uses a single common safe listening gain, preserving original relative levels.
Master remains at −6 dB with output 23/24; the Analysis Node captures precede
that fader and are not ATEM/encoded-stream recordings.

All eleven source-track post-effects signals were captured along with Master.
The complete known loop was cyclically aligned against the source recording,
and the downstream linear sum was checked against the actual Master. These
are independent node counters, so this is not a shared-clock latency estimate.
GREEN is effectively silent here; its existing pan was left untouched.
Talkback is already at silence, and LAPEL is effectively silent.

Contribution ranking uses signed source/Master cross-power in 200–500 Hz,
rather than dividing isolated source powers by their sum. This accounts for
correlated bleed and cancellation: signed shares sum to approximately 100%
and can be negative. They describe measured contributions, not unwanted sound
or perceptual masking. In this passage EGUITAR accounts for 51.4%, RED 17.3%,
AC GUITAR 16.5%, KIT 7.1%, and KEYS 5.0%. The channel label AC GUITAR alone
does not establish whether its signal is intended playing or bleed.

The reconstructed baseline has a 0.0040% relative residual. The actual captured
candidate change agrees with the intended guitar gain change within 0.0173%
of the change waveform. No new emulator underruns were observed during either
capture. The full-mix 200–500 Hz / 1–3 kHz ratio changes from 5.44 to 4.82 dB.
This is a broadband guitar balance test, not an EQ band, and no musical
preference has been established. Lead level, KIT processing, sends, groups and
Master settings are unchanged.

`wcfi_mix_balance_package` schema 1 contains exactly four checksummed finite
stereo 48 kHz WAVs. Authenticated `POST /api/rehearsal/import-balance` imports
only a UUID from feature-owned `balance-staging`, validating file names,
duration, format, finite samples, checksums and declared match/restoration.
Balance studies have their own three-job retention budget, preserving the
three reference and three KIT studies. Updated clients opt in with
`mix_studies=1`; older clients do not receive the unfamiliar report type.
There is no Apply operation for these full-mix experiments.

The captures, alignment/attribution evidence, bridge dry-run and restoration
journal, package, final read-only session verification and private muted
headless app check are in `_work/lowmid-fix-2026-10-21-01/`. Play and Record
remained stopped throughout; no desktop controls or cloud uploads were used.

### EGUITAR fader versus targeted EQ

**Source balance experiments → EGUITAR · fader vs targeted EQ** adds three
alternatives for the same approved 32–44 second passage: Original, Fader −3 dB,
and EQ · 330 Hz. The first two reuse actual native captures from the verified
fader experiment. The third is explicitly a **local EQ preview**, not a new
Ableton/Pro-Q capture or an applied setting. The current bridge cannot insert
an EQ, and the existing Pro-Q exposes only Device On, not band parameters.

The local minimum-phase RBJ bell is −3 dB at 330 Hz, Q 1.0, warmed with one
complete repeated period. Its centre covers measured EGUITAR energy around
294–372 Hz; this is a listening hypothesis, not proof of an undesirable
resonance. The EQ alternative leaves the guitar fader at its original level
and does not include the fader reduction. It replaces only the captured guitar
contribution inside the previously verified linear mix. The unchanged remainder
has a relative residual of 0.0000029%. Current session controls were verified
against the captured baseline before preparation.

Guitar 200–500 Hz energy falls 2.75 dB, while 1–3 kHz energy falls only 0.14 dB.
The full-mix low-mid/presence ratios are 5.44 dB Original, 4.82 dB Fader, and
4.27 dB EQ. All three playback copies are independently verified at −24 LUFS;
original-level copies use a common safe gain. These measurements do not
establish an audible winner. Listen for lead clarity, guitar support, warmth
and harshness; spectral reduction is not a professional-quality score.

Balance package schema 2 contains exactly six allowlisted finite WAVs with
explicit `native_capture` / `local_eq_preview` provenance and three ordered
variants. Schema 1 remains supported. HTTP clients opt in with
`balance_schema=2` to avoid exposing the new structure to older open frontends.
Publication and audition never apply EQ or control Ableton transport.

The recipe, measurements, source-substitution validation, package, publication
ID and private muted headless app check are in
`_work/eguitar-eq-2026-10-21-01/`. No Ableton controls, desktop controls or cloud
audio services were used to prepare the EQ alternative.

### Cross-take EGUITAR comparisons: October 4 and 11

**Rehearsal lab → Source balance experiments** now includes
**10-04 · EGUITAR EQ comparison** and **10-11 · EGUITAR EQ comparison**.
Each compares Original, the actual bridge-controlled Fader −3 dB capture,
and a local guitar-only −3 dB bell at 330 Hz, Q 1.0. Loudness-matched copies
are verified at −24 LUFS. The EQ remains a preview and has not been applied
in Ableton. These older recorded inputs run through the current session's
processing and balance; they are not reconstructions of historical broadcasts.

The selected source windows are October 4, 09:41 take, **26:22–26:42**, and
October 11, 10:49 take, **1:12–1:32**. Selection uses sustained concurrent
activity on RED, EGUITAR, BASS and KIT, with KEYS activity also inspected.
The earlier October 4 guitar excerpt had essentially no bass and was excluded.
Signal activity cannot establish a coherent musical performance: both new
passages are explicitly unconfirmed candidates until the operator listens
and checks **Representative band performance**. Neither performer identity
nor playing ability is inferred.

The first October 4 native capture had 77 new emulator underruns and was
rejected. A retry used synchronized, sample-equal 20-second float WAV excerpts
of all eleven inputs, about 54 MB per take, without processing the source.
All four accepted native captures had zero new underruns. The original source
take, loop, feed, channel settings and all Ableton controls were restored.
Play and Record remained stopped; no desktop control or audio uploads were used.

All source paths were checked against Master by cyclically aligning complete
known repeated passages. The node counters are independent, so these checks
do not measure latency. For the active, panned GREEN channel, the L/R path gains
were estimated from the Master residual and independently verified on both
capture halves. Baseline sum residuals were 0.0083% (October 4) and 0.0017%
(October 11); actual fader-change residuals were 0.0223% and 0.0173% of the
intended change waveform, respectively.

| Measurement | October 4 | October 11, later take |
| --- | --- | --- |
| EGUITAR signed contribution to 200–500 Hz mix energy | 57.9% | 3.1% |
| Guitar 200–500 Hz / 1–3 kHz, Original → EQ | 19.69 → 17.19 dB | 2.81 → 0.83 dB |
| Full-mix ratio, Original / Fader / EQ | 10.40 / 9.06 / 9.10 dB | 7.62 / 7.76 / 7.57 dB |

October 4 supports auditioning EGUITAR as a substantial low-mid contributor.
In the selected October 11 passage, AC GUITAR contributes 82.5% and RED 12.4%;
cutting EGUITAR has little effect on the full-mix ratio. Signed cross-power
includes correlated bleed and cancellation; it does not establish unwanted
content or an audible winner. Additional raw October 11 morning-take candidates
also differ from the later take. A universal guitar cut is not supported.

`POST /api/rehearsal/assess-balance` stores the operator's boolean assessment,
reason and timestamp separately from immutable capture evidence. Updated
clients request `candidate_balance=1` in addition to `balance_schema=2`.
Older clients exclude unconfirmed passages so their earlier approved-passage
wording cannot misrepresent these candidates. This review changes metadata
only and never applies processing or controls Live.

The comprehensive October 18 three-way EQ study, three reference studies and
three KIT studies remain available. Before the balance study retention limit
aged out the superseded two-way October 18 fader study, a verified recoverable
copy was saved in the work directory's `archived-reviews` folder.

Passage selection, rejected and accepted captures, restoration journal, dated
packages, publication IDs and final read-only verification are in
`_work/cross-take-eq-2026-10-21/`. Validation passed 71 focused backend tests,
Ruff and the frontend build. A private muted headless app check verified both
three-way studies, all six authenticated matched-audio requests, unconfirmed
passage labels, preservation of other studies, and wide/compact layouts.
Its media playback was mocked; it produced no sound or control requests.

### October 11 AC GUITAR input, level and EQ experiment

**Source balance experiments → 10-11 · AC GUITAR level and EQ** inspects the
same 10:49 take, 72–92 second candidate. The accepted eleven-source capture
was reused only after the complete current session matched its snapshot.
Its linear source reconstruction residual remains 0.0017%.

The raw AC GUITAR input averages −7.64 dBFS RMS, versus RED at −17.13 dBFS.
Approximately 3.31% of raw samples reach full scale, with near-full-scale
plateaus lasting up to 80 samples. This supports reporting clipping already
present in the recorded input. It does not identify the originating analogue
or digital stage. The before/after node signals are identical, the channel
contains only the two analysis nodes, its fader and group are at 0 dB, and
its source sends are off. Current effects are not causing this excess level.
Roughly 81% of its measured spectral energy lies below 500 Hz. Together,
source level and low-frequency content explain its 82.5% signed contribution
to the captured mix's 200–500 Hz energy. This is not proof that all of that
contribution is unwanted or that the channel label establishes source identity.

The comparison offers three independently matched −24 LUFS full-mix copies:

- **Original**: the accepted native capture at the original channel level.
- **Fader −9 dB**: a new actual native capture with only AC GUITAR lowered.
- **Level + EQ**: the same reduced-level capture plus a local AC GUITAR-only
  −3 dB bell at 300 Hz, Q 1.0, substituted through the verified linear path.

The additional EQ is tested with the level reduction held fixed; it is not
an EQ-only alternative. It reduces the source's 200–500 Hz energy by 2.48 dB
and 1–3 kHz energy by 0.15 dB beyond the uniform fader reduction. Full-mix
low-mid/presence ratios are 7.62, 6.42 and 5.67 dB respectively. No audible
winner or professional target has been established. A lower spectral ratio
alone does not establish a better mix. Neither gain nor this EQ restores
peaks lost before recording. The next physical check is the instrument output
and mixer input path, including analogue trim if that stage is overloading.

The new native capture had zero new emulator underruns and its delta agrees
with the intended source gain change within 0.0033% of the change waveform.
An initially omitted `db=None` argument prevented the first exact-value fader
restore call. A corrected guarded call restored the original value, and both
the complete session and emulator configuration were verified afterward.
The final AC GUITAR fader is back at 0 dB; Master remains −6 dB, output 23/24,
and the original October 18 input take and stopped feed were restored. No
new processing remains applied. No Play, Record, desktop or cloud controls
were used.

Balance package schema 3 adds an explicit target source, input findings,
comparison explanation and listening notes. The frontend renders the correct
source instead of assuming EGUITAR. HTTP clients opt in with `balance_schema=3`;
older source-specific clients cannot receive and mislabel the new study.
Schemas 1 and 2 remain readable. Balance retention is now four studies,
preserving the three previous guitar comparisons; reference, KIT and ordinary
capture budgets remain three each. Import tests verify source identity,
legacy visibility, required findings and the finite retention budget.

Evidence, accepted native capture, package, publication ID, recovery journal,
and final read-only verification are in `_work/ac-guitar-2026-10-21-01/`.
All 79 focused backend tests passed with workspace-owned temporary files and
local loopback access, along with Ruff and the frontend build. A private,
muted headless check verified all six matched/original-level audio routes,
correct AC GUITAR findings, incremental EQ labels, preservation of existing
studies, legacy visibility and responsive layout. Media playback was mocked;
it produced no sound or control requests.

### RED/KIT cross-session evaluation

**October 22 replacement:** October 11's **0:40–1:00 in the 10:49 take**
replaces the rejected 1:12–1:32 excerpt in both suites. The operator clarified
that the drums were inaudible in **Full mix**. Checking the authenticated app
files against the package checksums showed correct delivery. Raw and solo KIT
contain sustained drum activity; the former full mix has KIT RMS 21.82 dB
below overall mix RMS. The earlier description of this as “no kit playing”
was too strong. The former excerpt and its assessments are archived.

Both October 11 takes were scanned using concurrent source levels and repeated
low/body/high-band attacks. The replacement has attacks in all ten 2-second
bins, with maximum detected gaps of 0.34/0.44/0.45 seconds. A local CPU-only
[YAMNet sound classifier](https://www.tensorflow.org/hub/tutorials/yamnet)
corroborates percussion/snare/hi-hat activity throughout. Its
[pinned ONNX conversion](https://huggingface.co/audiomagic/yamnet-onnx/tree/f25b741c2f0bdc6d7e6db24b5fddda23347dbafd)
is 16.1 MB and is retained with its license and checksums under the work folder;
no recordings were uploaded. Scores are uncalibrated source-identity evidence,
not proof of coherent performance or professional quality. Guitar and vocal
controls score far below KIT on drum classes. The rejected full-mix excerpt
is **not** a negative control for drum presence in the isolated KIT source.

New native RED/KIT/Master baseline and RED headroom test captures had zero
input underruns; one earlier candidate capture with an underrun was rejected.
Full-mix change residual is 0.0183% for the accepted native test. All fader and
emulator settings were restored. The new passage remains machine-screened,
not operator-confirmed. Other dates' audio/evidence and existing judgments are
preserved. The app shows the replacement's selection evidence and the balance
warning: KIT RMS remains 21.84 dB below mix RMS, and AC GUITAR accounts for
86.3% of signed 200–500 Hz mix energy. Source-processing previews do not fix
that balance or establish perceptual improvement.

**Rehearsal lab → Cross-session evaluation → Open · RED & KIT · three sessions**
applies the same role profiles to October 4 (26:22–26:42), October 11's later
10:49 take (0:40–1:00), and October 18 (0:32–0:44). October 11 is a
machine-screened performance candidate; October 18's passage was previously
confirmed by the operator. Performance judgments are saved separately for each
study. The recordings belong to different leaders/bands
according to the operator; the signal analysis does not infer performer identity.

Fresh RED/KIT/Master baseline pairs were captured through one unchanged current
chain. Small synchronized lossless excerpts of all eleven raw sources reduced
disk replay work. All three baselines and the one native edited capture had
zero new emulator underruns. Known complete source cycles establish alignment;
independent Analysis Node counters still do not provide host latency or a
shared sample clock. Baseline linear source sums agree with Master within
0.0086%, 0.0019%, and 0.0043% relative residual respectively (October 11 updated).

The suite policy tests a configurable −6 dBFS **sample-peak** headroom ceiling
with a 0.25 dB tolerance, avoiding edits that chase a tenth of a decibel. This
is an engineering rule, not a musical level target. There are no automatic
boosts, EQ, de-essing or new compression settings. Short HF bursts, brightness,
energy dynamics and source activity remain descriptors requiring interpretation.
The existing role-analysis observations/proposals remain intact; the suite's
separate decision layer records which edits, if any, were eligible for this test.

| Session | RED current peak / decision | KIT current peak / decision | Input note |
| --- | --- | --- | --- |
| October 4 | −11.05 dBFS / keep | −5.88 dBFS / keep within tolerance | KIT: 380 full-scale samples, 0.020% |
| October 11 (replacement 0:40–1:00) | −4.61 dBFS / test −1.39 dB fader reduction | −10.66 dBFS / keep | No RED/KIT full-scale samples; band balance remains problematic |
| October 18 | −9.55 dBFS / keep | −5.90 dBFS / keep within tolerance | KIT: 11,324 full-scale samples, 0.983% |

The KIT counts include both channels. Existing output attenuation does not
remove a pre-effects input-health warning or identify the exact originating
stage. RED's short HF burst counts are 8, 14 and 3; these are not confirmed
sibilance and no de-esser was enabled. KIT crest changes are approximately
+0.25, −0.01 and +0.86 dB, below the existing 3 dB transient-flattening review
criterion. These results do not establish optimal drum tone or compression.

RED has no supported post-effects gain processor in its current chain. The
October 11 test therefore uses its existing fader, explicitly **after the
mixer**: the replacement's after-effects node remains at −4.61 dBFS, while the verified
post-mixer contribution reaches −6.00 dBFS. The actual full-mix delta agrees
with the isolated RED change within 0.0183% relative residual. This can lower
the lead relative to the band and is not automatically a better mix. The
existing KIT Compressor Output Gain adapter is supported by the policy and
tests, but no KIT parameter edit was needed in these cases.

Every original device parameter, fader, routing, emulator channel setting,
take, loop and feed was restored. Play and Record remained stopped. Current
Master is still −6 dB with output 23/24. No desktop controls, cloud audio
services, neural models or GPU were used.

The app provides a date selector, RED and KIT before-effects/current/candidate
solo auditions, and current/candidate full mixes. Forty-eight finite files
cover all dates/scopes with both original relative levels and verified LUFS
matches. Full mixes match −24 LUFS; solo scopes have separately verified
targets and safe true peaks. Gain-only solo changes largely disappear when
matched. October 4 and 18 Current/Candidate copies are explicitly identical
controls, because no edit was authorized; a sonic difference is not expected.
Solo processed files use captured post-effects signals with verified fader
gains, not separate after-mixer Analysis Node captures.

`wcfi_rehearsal_suite_package` schema 1 has fixed passage/scope/audio keys,
checksums, finite stereo 48 kHz assets, 0.5–20 second windows, explicit native
versus unchanged-control validation and bounded attenuation decisions.
`POST /api/rehearsal/import-suite` imports only a feature-owned staged UUID.
HTTP clients opt in with `role_suites=1`; older clients cannot misrender the
new report. Suites have a separate two-job retention budget, preserving all
four balance experiments, three KIT studies and three reference studies.
The package importer and audition paths do not apply any saved setting.

`POST /api/rehearsal/assess-suite` persists each passage's performance decision
and Better/Same/Worse/Unsure judgment separately from immutable measurements.
The app never infers operator preference from numerical results. This metadata
operation does not control the DAW. During initial headless QA an asynchronous
mocked rating request left an automated test entry in the service. Its payload
and timestamp identified the exact entry; it was archived and removed without
changing audio, measurements or other reviews. The final QA browser blocks
all mutation fetches, performs no rating submissions, and verifies clean
default judgments. Judgment persistence is tested in isolated temporary data.

Capture/restore journals, six role reports, native change checks, the package,
publication ID, QA metadata cleanup audit and final read-only verification are
in `_work/red-kit-suite-2026-10-21-01/`. Validation passed 116 focused tests,
Ruff and the frontend build. The final muted headless app check verified all
48 authenticated audition routes, both level modes, stage labels, unchanged
controls, responsive layout, legacy visibility and preserved existing studies.
Media playback was mocked, and no Play, Record or desktop operations were used.

### Experimental EQ, compression and RED de-essing comparisons

**Cross-session evaluation → Open · RED & KIT · EQ, compression & de-essing**
adds a separate processing study using the same October 4, 11 and 18 passages.
This does not change the existing CLI role profiles or authorize unattended
tonal/dynamics edits. `role_processing.py` contains a new fixed version-1 set
of bounded listening hypotheses. `processing_study.py` prepares and validates
the comparisons. All treated versions are **local reference DSP**, not native
Pro-Q, Pro-DS or Ableton Compressor renders. The captured current chain was
verified against the open session before reuse. No previous RED headroom
fader reduction is carried into this experiment.

The EQ hypothesis tests two separate conditions: substantial energy below
80 Hz on RED or below 30 Hz on KIT may select a gentle role-specific high-pass;
200–500 Hz density more than 4 dB above the geometric mean of neighbouring
100–200 and 500–1200 Hz densities, with over 20% low-mid energy, may select a
broad bell. Bell cuts are capped at 2 dB, Q 0.7, centred at 300 Hz for RED or
350 Hz for KIT. These are explicit hypotheses: musical fundamentals can meet
the same conditions. The biquad equations follow the
[RBJ Audio EQ Cookbook](https://www.w3.org/TR/audio-eq-cookbook/).

Compression candidates require usable signal, energy activity, sufficient crest
and an upper-level tail more than 3 dB above the active median. The threshold
comes from the active-window 80th percentile. RED uses 2:1, 15 ms attack,
100 ms release, 6 dB soft knee and at most 3 dB reduction. KIT uses 1.5:1,
25 ms attack, 120 ms release, 4 dB knee and at most 2 dB reduction. The custom
feed-forward RMS processor has stereo-linked gain and no makeup. It uses a
5 ms power detector and interpolated 1 kHz gain control. Its curve/envelope
concepts can be compared with the
[W3C dynamics processing description](https://www.w3.org/TR/webaudio/#DynamicsCompressorNode),
but it does not implement or emulate that node or any commercial compressor.

RED de-essing candidates are short 30–250 ms HF bursts, requiring both an
absolute HF fraction and an increase above that passage's HF/body baseline.
Strong coincident bright KIT events veto a candidate. Synthetic overlap tests
exercise the veto, but this remains a heuristic rather than confirmed speech
or sibilance detection. Eligible events receive at most 2.5 dB of brief
wideband attenuation, 2 ms attack and 35 ms release. This can duck vocal body
or produce lisping; it is not a selective reconstruction of consonants. KIT
never runs this vocal detector. EQ and dynamics are warmed with one known
repeated source period; the offline interpolated controls need separate
real-time/latency validation before a live processor is implemented.

| Passage | RED bell at 300 Hz | RED maximum compression | RED HF events / affected fraction | KIT maximum compression |
| --- | --- | --- | --- | --- |
| October 4 | −0.91 dB | 2.98 dB | 8 / 8.79% | 1.83 dB |
| October 11 (replacement 0:40–1:00) | −0.75 dB | 2.57 dB | 14 / 14.95% | 1.49 dB |
| October 18 | −1.06 dB | 1.10 dB | 3 / 5.17% | 1.77 dB |

No high-pass gate fired in these windows. KIT's body-concentration EQ gate
also stayed off on all dates, so its EQ/current versions are intentionally
identical controls. Its full chain therefore equals its compression-only
candidate. This only tests the stated rumble/body hypotheses; it does not
certify ideal drum tone. Current KIT already contains its existing Ableton
compressor, so these are additional gentle-processing trials. Input clipping
warnings persist and no processor claims to reconstruct lost peaks.

The app offers RED, KIT or Both, full mix or solo, individual component or
full chain, and matched/original-level playback. The chain order is EQ,
compression, then RED de-essing. Single-source mix variants substitute only
that captured source; Both substitutes the two complete chains. Before
global audition gain, the remaining mix waveform is checked unchanged, with
relative residual below 0.000004% in these comparisons. The accepted original
linear source sums remain the prerequisite for substitution.

In matched mode each treated source is first restored to its original
integrated LUFS before reinsertion. The full mixes then receive scalar
loudness matches, verified at −24 LUFS here. Solo variants have independent
verified targets and safe true peaks. Compensation is capped at ±6 dB.
Original-level mode preserves the actual processing level changes and uses
one safe gain per scope. Thus a reduced treatment does not become a tonal
winner merely by making its source quieter. The matching gains and measured
gain reduction are recorded separately from DSP settings.

`wcfi_processing_study_package` schema 1 contains 108 fixed-key finite 48 kHz
stereo assets across the three dates. Checks include checksums, format, duration,
peak safety, loudness matching, bounded source compensation, role identity,
local-DSP provenance and complete linear-substitution evidence. WAV container
metadata can differ between identical signals; twelve KIT EQ identity checks
compare decoded PCM and confirm zero sample difference in solo/full-mix and
matched/original modes.

Authenticated `POST /api/rehearsal/import-processing` imports a locally staged
UUID through the suite manager. Clients require `processing_studies=1` in
addition to `role_suites=1`; older suite frontends do not receive the new report
kind. The prior headroom suite and all four balance, three KIT and three
reference studies are preserved. No live Apply operation is supplied for
these local processors.

`POST /api/rehearsal/assess-processing` records Better/Same/Worse/Unsure per
date, role, component and solo/full-mix context. These judgments are independent
of passage confirmation and immutable measurements, and survive restart.
No numerical result is converted into a human preference. QA ratings run only
against isolated test data; the final headless app check blocks every mutation.

Recipes were frozen before rendering all dates, with no per-date threshold
tuning. Evidence, prepared files, publication ID, identity checks, muted
headless screenshots and final read-only session verification are in
`_work/role-processing-2026-10-21-01/`. All 131 focused tests, Ruff and the
frontend build passed. The app check exercised 90 combination/level states and
all 108 authenticated audition routes, with no console errors, autoplay, posts
or desktop interaction. Media playback was mocked. No neural model, GPU,
private audio upload, Ableton signal write, Play or Record command was used.
