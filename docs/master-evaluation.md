# Master evaluator benchmark

`wcfi audio master-benchmark` prepares a reproducible challenge set and runs
Audiobox Aesthetics entirely on the CPU. It reads saved stereo Master excerpts;
it never opens an audio device, accesses Ableton, starts a source loop or uploads
recordings. This is a test of a prospective evaluator, not an automatic mix fix.

The same evaluator scores Master pre-FX observations, the current final feed and
candidate final feeds. Pre-FX is context, not a clean or preferred reference.
In the initial three-service fixture, the saved Master chain has no processing
between its Analysis Nodes; their aligned waveforms are exactly equal. The
verified −6 dB Master fader is applied to the post-FX capture locally to represent
the Ableton output feed. External TouchMix/ATEM processing is not represented.
These are the older sources replayed through the shared current chain, not the
historical service broadcasts.

## Challenge protocol

Each session contributes ten versions of one identical musical passage:

| Condition | Intervention | Interpretation |
| --- | --- | --- |
| Current | Saved post-FX Master plus verified output gain | Same-performance baseline |
| Identical | Exact decoded PCM copy | All predicted axes must tie within 1e−6 |
| Matched level only | Exact 0.5× gain followed by 2× restoration | Matched-input invariance; does not test unmatched loudness invariance |
| Mild clipping | Limit samples above the 99th percentile of absolute amplitude | Approximately 1% of samples limited; secondary sensitivity/severity test |
| Severe clipping | Limit samples above the 90th percentile | Approximately 10% limited; expected lower PQ in the coarse gate |
| Low-pass | Sixth-order 2 kHz Butterworth, forward/reverse | Expected lower PQ in the coarse gate; offline intervention, not a plug-in emulation |
| Mains hum | Centered 60/120/180 Hz hum at −12 dB RMS relative to the program | Expected lower PQ in the coarse gate |
| Dropouts | Four 250 ms missing-audio intervals with 2 ms edge ramps | Expected lower PQ in the coarse gate |
| Candidate | Existing local RED/KIT chain preview in the full mix | Unknown human preference; no expected winner |
| Pre-FX | Saved Master input observation | Unknown preference; not a clean reference |

Each Current input is independently inferred again at the end. Exact duplicates
are not cached or deduplicated. Audiobox scores clips independently; subtracting
those scores in reversed order is algebra, not a separate test of pairwise
reasoning. Repeated inference instead checks numerical repeatability.

All audition versions receive scalar full-mix loudness matches. No individual
source is normalized in this benchmark. The per-session target is at most −24
LUFS, reduced if needed for a −3 dBTP listening ceiling. Matches must be within
0.15 LU and true peaks below −2.85 dBTP. Original levels and physical intervention
measurements are retained separately. Null controls use the same matched samples
and must produce byte-identical mono model WAVs.

Prepared inputs have opaque shuffled IDs. Waveforms are converted to mono
16 kHz PCM16 using the existing documented model converter. The runner reads
the blind manifest and hashes, not the private answer key, condition names or
measurement descriptions. The protocol and answer-key hashes are frozen before
inference; scoring refuses changed or incomplete inputs/predictions.

The primary axis is Audiobox's predicted Production Quality (PQ). CE, CU and PC
are retained but are not alternative targets chosen after looking at results.
The preregistered coarse gate requires all null/repeat checks and all four coarse
degradations per session to pass. A lower PQ beyond 1e−6 is a numerical direction,
not a perceptual effect size. A separate 0.1 PQ reporting bin is descriptive and
has no calibrated audibility or preference meaning.

These expected degradation directions are engineering hypotheses, not human
ratings. In particular, an intervention to an already poor mix might change its
perceived quality unexpectedly. Passing coarse controls cannot establish useful
judgment of subtle EQ/compression or professional church production. Human
same-performance comparisons and held-out services remain necessary. Variants
and repeated inferences do not increase the number of independent performances.

## CPU execution

The inference command hides CUDA before imports, uses explicit CPU float32 model
construction, one Torch thread, one interop thread, one logical CPU and idle
priority. It blocks socket connections in its own worker, uses only local
safetensors and has no network fallback. GPU initialization and tensor devices
are checked before and after every inference. Other processes' devices, CPU
affinity and priority are not changed.

One model is loaded for the run; calls are serialized. Each inference is followed
by a pause of its own duration, capped at ten seconds. Deadlines are 45 seconds
for loading, 30 seconds for an inference and 20 minutes for the entire run.
Interrupted evidence is preserved; commands do not retry or overwrite it.
Elapsed inference time, wall time, CPU process time and the worker's memory usage
are recorded. CPU execution feasibility is separate from model judgment quality
and from sustained stability during a livestream.

## Commands

Use the ordinary project environment for preparation/reporting and the existing
listening environment, containing Torch and Audiobox, for inference:

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio master-benchmark prepare SOURCES.json --output BENCHMARK --ffmpeg FFMPEG.exe
.\.venv-listen\Scripts\python.exe -m wcfi_tools.cli audio master-benchmark run BENCHMARK --output RUN --checkpoint LOCAL_CHECKPOINT
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio master-benchmark report BENCHMARK --run-directory RUN
```

The source JSON has `kind: wcfi_master_benchmark_sources`, `schema_version: 1`
and a `sessions` array. Each session supplies an ID, verified output gain and
`pre_fx`, `post_fx`, `candidate` entries containing a local WAV `file` and its
`sha256`. Paths resolve relative to the profile. Excerpts must be active stereo
48 kHz WAVs, 10–20 seconds, and contain the same aligned musical passage.

The initial WCFI run is under `_work/master-benchmark-2026-10-22-01`:
October 4 26:22–26:42, October 11's 10:49 take 0:40–1:00, and October 18
0:32–0:44. Thirty prepared clips plus three repeats give 33 inference calls,
but only three service excerpts. No new performance confirmation or human
preference is inferred. The HTML/JSON result explicitly retains
`professional_mixing_judgment_validated: false` regardless of the coarse gate.

## Initial CPU result, October 22

The 33-call run completed with the pinned Audiobox weights
`a5a3c2412649cc2384ec525ffd5180ce6c4778f43bed6108e0a1303de04d014e`.
All model parameters/buffers stayed on CPU, Torch and interop thread counts
remained one, CUDA was uninitialized, and there were zero connection attempts.

| Check | Result |
| --- | --- |
| Identical and exactly matched level controls | 6/6 tied on all four axes |
| Independently repeated Current inputs | 3/3 tied on all four axes |
| Severe clipping expected to lower PQ | 3/3 |
| 2 kHz low-pass expected to lower PQ | 3/3 |
| Added hum expected to lower PQ | 2/3 |
| Dropouts expected to lower PQ | 2/3 |

The preregistered coarse gate failed: October 4 dropouts increased PQ by
0.0665 and October 11 hum increased it by 0.0755. These are numerical ranking
counterexamples, not calibrated perceptual effect sizes or human ratings.
Both are below the descriptive 0.1 reporting bin; the gate was not changed
after seeing the results. All twelve coarse interventions were independently
verified to survive the actual mono/16 kHz conversion, and all six null inputs
were sample-identical after conversion.

The existing RED/KIT candidate changed PQ by −0.0292, +0.1891 and −0.1292 for
October 4, 11 and 18 respectively. No candidate is endorsed on this basis.
Cross-date absolute PQ values must not be interpreted as mix-quality rankings
because performances, songs and source material differ.

Median inference was 5.469 seconds (range 4.453–8.703); summed inference was
183.529 seconds. Total wall time including 13.922 seconds of loading and
deliberate cooldowns was 383.125 seconds. Peak worker working set was 1301.2 MiB,
about 1.27 GiB. No audio was played, uploaded, newly captured or fed into Live.
This completed run establishes bounded CPU feasibility in these conditions,
not thermal certification or sustained performance alongside a live broadcast.

The report is `_work/master-benchmark-2026-10-22-01/run-cpu-01/report.html`;
raw predictions, prepared inputs, protocol, source hashes and conversion audit
are retained beside it. This predictor remains a possible supplementary feature,
not a validated autonomous professional-mix judge.

## October 11 balance follow-up

The follow-up under `_work/oct11-balance-2026-10-22-01` uses the same
10:49 take, 0:40–1:00 passage. Four new native Master captures isolate AC GUITAR's
fader at −∞ (mute remains off), KIT's fader at +6 dB (from 0 dB), and both changes.
Play and Record remain stopped. The emulator loops the saved inputs independently
of Live's transport. All original faders and emulator settings are restored after
capture; these are saved experiments, not a permanent mix recommendation.

All captures had zero new input underruns. A complete source-sum check and each
fader-change prediction matched the actual Master to within 0.006% relative RMS
error. Near-silent YELLOW/LAPEL inputs cannot establish correlation reliably;
their raw pass-through estimates are retained and bounded by this sum check.
Known complete loops are cyclically aligned, not interpreted as host latency.
The unchanged −6 dB Master fader is included in the comparison feed.

| Native balance | Audiobox PQ | Change from current |
| --- | --- | --- |
| Current: AC 0 dB, KIT 0 dB | 7.510 | — |
| AC −∞, KIT 0 dB | 8.092 | +0.582 |
| AC 0 dB, KIT +6 dB | 7.425 | −0.085 |
| AC −∞, KIT +6 dB | 7.960 | +0.450 |

The combined-above-current hypothesis and PQ axis were recorded before inference.
An exact Current duplicate tied on all four axes; independent Current and Both
repeats also tied within 1e−6. Five blinded inputs plus two repeats ran on one CPU
thread with CUDA uninitialized and no network connections. Median inference was
5.156 seconds; wall time including loading and cooldowns was 85.75 seconds.

The model responds in the hypothesized direction to removing the dominant
acoustic in this passage; it does not prefer raising KIT by itself. These remain
model rankings with unknown human preference, not evidence of a professional
finished mix. A 20-second passage is one performance context, regardless of the
number of variants. Removing an instrument is a diagnostic extreme.

Rehearsal Lab's **October 11 · AC down / KIT forward** panel contains all four
actual captures, scalar loudness matches verified at −24 LUFS, original relative
levels with one shared safety gain, and an expandable CPU model-results table.
Schema 4 keeps this study hidden from older frontends; reload the app to load
the updated UI. The five-study balance budget preserves the four older balance
experiments. No new human assessments are inferred or fabricated.

## Acoustic/KIT level sweep

`_work/oct11-level-sweep-2026-10-22-01` expands the same passage into nine
native combinations: AC GUITAR −24, −18 and −12 dB, each with KIT 0, +3 and
+6 dB. The original mix and earlier AC −∞ / KIT 0 and +6 captures remain
references. The complete inspected chain must match the earlier capture chain
before those references can be reused. AC GUITAR stays unmuted throughout.

All nine new captures had zero input underruns; actual gain fits differed from
the requested levels by less than 0.001 dB. Full-mix waveform changes after
fitting those two gains had relative residual below 0.002%. The independent
earlier KIT-up capture verifies the source basis separately. Actual captures,
rather than reconstructions, supply all twelve listening/model versions.
Live's original controls and the emulator source configuration are restored.

All versions are verified at −24 LUFS. Thirteen opaque inputs include an exact
original-mix duplicate; original and center-grid (AC −18 / KIT +3) independent
repeats produce fifteen scheduled CPU inferences. PQ and the nine-point grid are
fixed before inference. The initial app candidate is the highest PQ among the
nine acoustic-present versions, with no automatic permanent fader application
or inference of human preference. This is an exploratory search on one passage,
not a held-out quality benchmark.

Rehearsal Lab's **October 11 · acoustic / KIT level sweep** uses separate AC and
KIT selectors plus a reference selector. These controls select saved audio only.
Matching is enabled by default; both listening modes retain all twelve versions.
Schema 5 is hidden from older frontends, and the six-study balance budget keeps
the five preceding experiments. No GPU, cloud requests, transport commands or
desktop control are used.

The completed sweep's PQ scores were:

| AC GUITAR fader | KIT 0 dB | KIT +3 dB | KIT +6 dB |
| --- | --- | --- | --- |
| −24 dB | 7.838 | 7.838 | 7.712 |
| −18 dB | 7.650 | 7.702 | 7.623 |
| −12 dB | 7.428 | 7.377 | 7.330 |

The original remains 7.510, AC absent / KIT 0 is 8.092, and AC absent / KIT +6
is 7.960. AC −24 / KIT +3 is the numerical grid maximum, but it exceeds the
same acoustic setting with KIT 0 by only 0.000087 PQ. That does not establish
a useful listening preference. All nine acoustic-present versions score below
the acoustic-absent reference; optimizing this predictor alone therefore does
not establish the correct musical contribution of every instrument.

The duplicate and both independent repeats tied on all four axes within 1e−6.
All fifteen inferences remained on one CPU thread with CUDA uninitialized and
zero network connections. Median inference was 4.985 seconds; total wall time,
including loading and cooldowns, was 161.453 seconds. No new grid preference
labels or professional-quality claims were inferred from these scores.

## Building BASS against KIT without a quality model

`_work/bass-kit-foundation-2026-10-22-01` starts from the operator-requested
drums-first mix: KIT +6 dB, Master −6 dB, other source/return faders at −∞,
groups at unity. It captures KIT-only and BASS at 0, +3 and +6 dB. No mute,
transport, device, send or pan changes are made. BASS +3 dB is left applied;
the original KIT-only foundation and a guarded BASS-only undo are saved.

The provisional +3 dB choice uses measured source level, spectral context and
headroom rather than Audiobox. BASS is then approximately 1.1 dB below KIT in
broadband RMS and 2.1 dB below it in 20–200 Hz power. BASS is mainly 80–200 Hz
with 200–500 Hz body, while KIT has more 20–80 Hz energy. These observations
guide a conservative starting balance; neither equal RMS nor a fixed band
ratio is treated as a universal musical objective. No listening preference is
inferred from the measurements.

| Capture | Actual output LUFS | Actual true peak |
| --- | --- | --- |
| KIT only | −28.2 | −10.6 dBTP |
| BASS 0 dB | −26.5 | −10.3 dBTP |
| BASS +3 dB, starting balance | −25.3 | −10.2 dBTP |
| BASS +6 dB | −23.6 | −8.4 dBTP |

All captures had zero new underruns, and KIT+BASS reconstruct the native Master
within 0.006% relative RMS error. The app's **October 11 · BASS against KIT**
comparison contains all four native captures and scalar-only −24 LUFS copies.
Unmatched mode keeps KIT at a fixed listening level; matched mode changes the
whole-pair listening gain and preserves the relative instrument balance.

Schema 6 supports 3–6 explicitly named native foundation variants, a saved
starting decision and bounded explanations without model scores. The panel
marks the chosen starting version, explains that it is applied in Live, and
does not claim a professional-quality score or fabricate a performance rating.
Older clients do not see schema 6 studies. Seven balance studies are retained,
preserving the six preceding experiments. Source feed configuration and all
existing app-study metadata are preserved during publication.

## Adding RED over the rhythm foundation without a quality model

`_work/red-foundation-2026-10-22-01` captures the same October 11 10:49 take,
40–60 second passage, with KIT +6 dB, BASS +3 dB and Master −6 dB fixed.
The app's **October 11 · RED lead balance** compares the rhythm pair alone
with RED at −3, 0 and +3 dB. RED 0 dB is left applied. All other source and
return faders remain at −∞; no processing, routing, mute or transport controls
are changed. Original controls and a guarded RED-only undo are saved.

The provisional choice uses the intended prominent worship lead, measured
source level and headroom. At 0 dB RED's microphone contribution has about
0.8 dB more broadband RMS energy than KIT+BASS combined. The mic includes
bleed, so this is not a validated vocal activity, masking or intelligibility
measurement. The assistant has not heard these captures; no audio model,
GPU or cloud upload is used. A professional sound or listening preference
cannot be established from these measurements alone.

| Capture | Actual output LUFS | Actual true peak |
| --- | --- | --- |
| KIT + BASS, RED absent | −25.3 | −10.2 dBTP |
| RED −3 dB | −22.8 | −9.4 dBTP |
| RED 0 dB, starting balance | −21.2 | −8.2 dBTP |
| RED +3 dB | −19.1 | −6.1 dBTP |

All three changed-level captures have zero new input underruns. The three
sources reconstruct the captured Master within 0.006% relative RMS error.
The app includes four native captures and scalar-only −24 LUFS copies.
Unmatched listening holds KIT+BASS fixed; matched listening scales the entire
mix while preserving the relative source balance. This is a three-source
balance study, not the completed band mix. Eight balance studies are now
retained, preserving all seven earlier balance comparisons and their metadata.

## KIT spectral and temporal diagnostic

`audio/kit_diagnostic.py` provides a deterministic diagnostic for aligned raw
and current processed KIT, with whole-mix reference context. The first saved
report is `_work/kit-diagnostics-2026-10-22-01`, using fresh 20-second node
captures from October 11, 40–60 seconds. The app's **October 11 · KIT diagnostic**
has separate KIT and Bethel/Maverick context views, interactive normalized
1/3-octave spectra, broad-band power, sample health, stereo/mono measurements,
and fixed raw-selected attack/tail windows. Five local scalar-only listening
copies are verified at −24 LUFS and below −3 dBTP. No edits are applied.

The actual KIT chain is a 2:1 compressor, threshold −6 dB, attack 15 ms, release
100 ms, Output Gain −5.88 dB and 100% wet. In this passage the best-fit raw/post
gain is −5.8781 dB; the residual after fitting gain is only 0.02675% relative
RMS. Crest factor changes from 18.4969 to 18.4893 dB. Normalized spectral changes
are below 0.001 dB, and paired median early/tail ratios remain essentially
unchanged. This chain is operating almost as a level reduction here; it does
not establish how the compressor behaves on a louder passage.

Raw KIT has 55.82% of measured power at 30–100 Hz, 33.01% at 100–250 Hz,
4.63% at 250–500 Hz, 2.17% at 2–5 kHz and 0.50% at 5–12 kHz. No full-scale,
above-full-scale or flat-top sample indicators are observed in this excerpt.
These distributions prompt bounded weight/definition experiments, rather
than a predetermined low-mid notch or a claim that drum energy must follow a
fixed percentage target. Local PSD peaks are listed as concentrations, not
identified unwanted resonances.

The temporal measure uses raw-selected 20 ms energy rises of at least 3 dB,
with at least 150 ms spacing and a relative energy gate. It compares the same
early −10–50 ms and tail 80–200 ms windows in raw and processed audio, excluding
windows with another selected candidate in the following 200 ms. There are 49
isolated 40–150 Hz candidate windows and 59 in 150 Hz–5 kHz. These windows can
contain multiple drums; this is not kick/snare identification, decay time or
RT60. Analysis filters are offline zero-phase filters, not live processors.

Bethel and Maverick use the previously saved, checksummed 12-second whole-mix
references. Their spectra, crest and width include other instruments and
mastering. WCFI's context capture is the current RED/KIT/BASS foundation, with
Master −6 dB included, not a finished band mix. The report supplies no isolated
reference-drum EQ target, true SNR estimate, perceptual masking score or quality
grade. Different reference durations and arrangements remain visible.

Diagnostic packages have their own three-job retention budget, authenticated
audio whitelist, checksums, bounded stereo formats, legacy-client opt-in and
no apply action. All eighteen prior studies and their metadata are preserved.
Computation is limited to one logical CPU at idle priority, with no GPU,
audio model, cloud request, transport command or desktop control.

## KIT 120 Hz shelf comparison

`_work/kit-shelf-2026-10-22-01` captures the unchanged October 11, 40–60 second
KIT/RED/BASS/Master signals and compares the current compressor chain with
−1 and −2 dB low shelves at 120 Hz. The cuts use stereo RBJ minimum-phase DSP
with slope 1 and repeated-period warmup, after the captured compressor.
They are local listening previews; no EQ is installed or applied in Live.
The low-frequency shelf gains are −1/−2 dB; the transition at 120 Hz has
approximately half those gains.

The app's **October 11 · KIT 120 Hz shelf test** has KIT solo and current-mix
views, each with three versions, loudness matching enabled initially, and
optional original-level listening. All solo copies are verified at −24 LUFS.
The matched mix versions equalize KIT integrated loudness and retain the
same RED/BASS contributions, with one common whole-mix audition gain. All
three happen to measure −24.0 LUFS here. This comparison does not independently
normalize each complete mix or move the other instruments to compensate for EQ.

The 30–100 Hz / 2–5 kHz power ratio decreases by 0.948 dB and 1.895 dB for
the −1/−2 dB shelves. KIT matching adds 0.4/0.8 dB relative to the current
solo version at the scanner's 0.1 LU precision. These are measured changes,
not an established listening preference. The context is still the three-source
foundation, not a completed band mix.

KIT's fader +6 dB and Master −6 dB give unity contribution gain through the
passive INSTRUMENTS group. The builder validates the path and reconstructs
the captured Master within 0.00574% relative RMS error before substituting KIT.
Unknown Master/group processing, sends, panning or fader automation reject
the preview. The matching helper also supports explicit non-unity path gains;
its regression test verifies that the other source contributions remain fixed
within float32 output precision.

Twelve checksummed stereo listening files are published through the diagnostic
package route, preserving all nineteen previous jobs and their metadata.
The feature has no apply action. No audio model, GPU, cloud upload, transport
control, mixer write or desktop control is used.

## Separate KIT 3 kHz presence comparison

`_work/kit-presence-2026-10-22-01` captures the same unchanged October 11,
40–60 second KIT/RED/BASS/Master signals for **October 11 · KIT 3 kHz presence
test**. The candidates are separate +1 and +2 dB RBJ minimum-phase bells at
3 kHz, Q 0.7, after the captured current compressor. Neither contains a shelf
cut. The actual KIT chain was verified to contain only the compressor between
its analysis nodes. The existing shelf experiment remains a separate app job.

The three solo listening copies are verified at −24 LUFS. The mix copies match
KIT's integrated loudness while keeping RED/BASS contributions fixed and using
one common audition gain; all three complete mixes also measure −24.0 LUFS in
this passage. The KIT 2–5 kHz / 30–100 Hz energy ratio increases by 0.903 and
1.808 dB. These are band ratios, not predicted intelligibility improvements or
professional-quality grades. Listening should test attack definition against
cymbal/snare harshness and whether RED remains clear.

The bounded builder and validator share the existing EQ-comparison pipeline,
with separate fixed presence prescriptions, variant allowlists and native versus
local-preview provenance. A new frontend opt-in hides presence reports from
already-open clients that only understand the earlier shelf/diagnostic views.
Twelve checksummed listening files cover solo/mix and matched/original modes.
The third diagnostic slot preserves all twenty preceding app jobs and their
metadata. No processing, faders, routing, transport or desktop controls change.
No GPU, model inference or audio upload is used.

## Combined KIT shelf and presence comparison

`_work/kit-combined-2026-10-22-01` contains **October 11 · KIT combined EQ test**:
the unchanged chain plus all four Cartesian combinations of −1/−2 dB shelves
at 120 Hz and +1/+2 dB bells at 3 kHz, Q 0.7. Each candidate starts from the
same native compressor output and applies shelf then bell with periodic filter
warmup. No candidate starts from a previously matched audition file.

The app provides five versions in KIT solo and in the current RED/KIT/BASS mix,
with KIT loudness matching enabled initially and optional original-level files.
Twenty checksummed stereo files cover both scopes and matching modes. The solo
copies are verified at −24 LUFS; matched mix copies also measure −24.0 LUFS,
using one common audition gain and fixed RED/BASS contributions.

| Shelf at 120 Hz | Bell at 3 kHz | Change in KIT 2–5 kHz / 30–100 Hz power ratio |
| --- | --- | --- |
| −1 dB | +1 dB | +1.851 dB |
| −1 dB | +2 dB | +2.756 dB |
| −2 dB | +1 dB | +2.798 dB |
| −2 dB | +2 dB | +3.703 dB |

These are measured spectral contrasts, not listening-preference labels.
The baseline reconstructs from its aligned sources within 0.00574% relative
RMS error. The actual chain, faders and routing remain unchanged. The combined
recipe has a fixed five-variant prescription, separate asset allowlist and
legacy-frontend opt-in; duplicate, missing or altered combinations are rejected.
Four diagnostic slots retain the raw diagnostic, shelf-only, presence-only and
combined studies. All twenty-one preceding app jobs and their metadata are
preserved. No model inference, GPU, audio upload, Play/Record command or desktop
control is used.

Upstream model and methods: [Audiobox Aesthetics](https://github.com/facebookresearch/audiobox-aesthetics),
[paper](https://arxiv.org/abs/2502.05139),
[ITU-R BS.1770 loudness and true-peak measurement](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I).

## Light KIT compression with the selected EQ fixed

`_work/kit-compression-2026-10-22-01` contains **October 11 · KIT light
compression test**. The same October 11, 40–60 second recorded loop was captured
through the native Ableton compressor at three thresholds. Ratio 2:1, attack
15 ms, release 100 ms, output gain −5.877 dB, wet 100%, knee 3 dB and zero
lookahead stayed fixed. Both threshold trials were restored to the original
−6 dB setting, verified against the entire host-exposed session snapshot.

| Threshold | Strong 10 ms windows, median / P90 reduction | Maximum 1 ms reduction |
| --- | --- | --- |
| Current, −6 dB | 0.000 / 0.003 dB | 0.015 dB |
| Gentle, −16 dB | 0.603 / 1.078 dB | 1.677 dB |
| Light, −18 dB | 1.044 / 1.628 dB | 2.331 dB |

These estimates use energy-weighted input/output scalar fits after removing the
known compressor Output Gain, before EQ. Strong windows are the loudest 10%
of active raw 10 ms windows; they do not identify individual kick/snare hits.
Within-window waveform residuals are below 0.27% relative RMS for all versions.
The estimates are not native gain-reduction meter telemetry.

Every listening version receives exactly the same local EQ after its native
capture: −1 dB low shelf at 120 Hz, slope 1, then +2 dB bell at 3 kHz, Q 0.7.
The EQ remains a preview, not an installed Ableton processor. Native Master
captures reconstruct from KIT/RED/BASS within 0.00574% relative RMS error;
raw inputs and captured RED/BASS outputs are unchanged between trials. A single
baseline mix remainder keeps RED and BASS fixed in the local EQ substitutions.

The app provides three versions, KIT solo and the RED/KIT/BASS mix, with
loudness matching on by default and optional original relative levels. All
matched solos and mixes measure −24.0 LUFS. Twelve files have checksums verified
through the app's audio endpoints. Five diagnostic slots preserve the four
preceding diagnostic/EQ studies; all 22 preceding jobs and their metadata were
preserved. Headless browser checks mock playback and never use desktop controls.

The light trial meets the requested reduction range. It does not materially
lower matched sample peaks with this 15 ms attack; its solo crest factor rises
from 18.83 to 19.16 dB. This is a test of envelope behaviour, not proof of better
musical quality or a peak-limiting solution. No candidate is left applied.
No model inference, GPU, upload, Play/Record command or desktop control is used.

## KIT/BASS body and overlap comparison

`_work/kit-bass-overlap-2026-10-22-01` contains **October 11 · KIT/BASS body
and overlap test**, using the same 40–60 second passage. All versions reuse
the saved native −16 dB threshold compressor capture (2:1, 15 ms attack,
100 ms release), followed by the fixed local −1 dB shelf at 120 Hz and +2 dB
bell at 3 kHz, Q 0.7. No Ableton parameters are written for this study.

Four mix versions compare that baseline with a −2 dB BASS bell at 200 Hz,
Q 0.8, a −2 dB KIT bell at 180 Hz, Q 1, and both cuts. These are local
minimum-phase EQ previews with periodic warmup. The saved native Master
reconstructs from the source contributions within 0.00574% relative RMS error,
allowing source-specific substitution without changing RED or other paths.

The KIT body cut lowers its 140–250 Hz / 2–5 kHz power ratio by 1.932 dB.
The BASS cut lowers BASS/KIT passage-average body power from +1.455 to
−0.402 dB while leaving KIT untouched. These describe the intended tonal
contrasts, not a preference or professional-quality score.

Channel-power bandpass analysis uses 10 ms windows and a relative −35 dB
gate. The strongest 10% of active baseline KIT windows are selected separately
in each band and reused across all versions. BASS is 20.0 dB below KIT in
the strongest 40–90 Hz windows (median), and never within 6 dB of KIT in those
selected windows. In 140–250 Hz it averages 1.455 dB above KIT across the whole
passage but is 13.3 dB below KIT in strong KIT windows (median). This supports
testing body balance rather than assuming strong kick attacks need bass ducking.
The energy tests do not establish psychoacoustic masking or identify kicks.

Separate source powers in 140–250 Hz are KIT 22.73%, BASS 31.78%, RED 45.49%.
RED supplies 85.56% of 250–500 Hz source power. These percentages exclude
phase-dependent cross terms in the summed mix and are not perceptual shares.
KIT/BASS cuts alone therefore cannot account for all low-mid energy in this
three-source foundation. RED has not been processed in this experiment.

The app offers four whole-mix loudness-matched files and four original-level
files. All matched copies measure −24.0 LUFS; true peaks range from −10.7 to
−11.1 dBTP. One scalar per entire mix preserves each candidate's intended
relative balance; KIT and BASS are not separately matched. RED scales with
the whole mix in matched mode (candidate gain differences at most 0.3 dB);
its contribution is identical in original-level mode. Six diagnostic slots
preserve all prior diagnostic/EQ/compression reports, and all 23 preceding
app jobs and metadata are retained. No new audio capture, model, GPU, upload,
Play/Record command or desktop control is used.

## RED low-mid tone comparison with a vocal-level control

`_work/red-lowmid-2026-10-22-01` contains **October 11 · RED low-mid tone
test**, using the same saved 40–60 second passage. The working KIT choice is
the KIT-only −2 dB body bell at 180 Hz, Q 1, after the saved native gentle
compression and fixed −1/+2 EQ; BASS stays unchanged. This is a provisional
choice for continuing the experiment, not an established listening winner.
The KIT/BASS bed is identical across all three versions before audition gain.

RED contributes 46.93% of its measured spectral power at 250–500 Hz. Averaged
peaks near 393 Hz and 492 Hz shift across two-second windows, with accompanying
lower-frequency peaks around 175–250 Hz. This is consistent with musical
fundamentals/harmonics, but neither pitch tracking nor a fixed room resonance
has been established. No deep automatic notch is justified by that observation.
The original saved pre-effects RED node peaks at −4.613 dBFS, with zero
full-scale, above-full-scale or possible flat-top samples. These indicators
do not prove absence of upstream distortion or require an analogue gain change.

The EQ candidate is a local −2 dB bell at 400 Hz, Q 1, using stereo RBJ
minimum-phase DSP and periodic warmup. It reduces RED's 250–500 Hz / 1–4 kHz
power ratio by 1.619 dB, and the foundation mix ratio by 1.375 dB. The same
cut also lowers RED's integrated solo loudness from −22.4 to −23.6 LUFS.
A third version therefore applies −1.2 dB gain to unfiltered RED, matching
the EQ-treated vocal's integrated loudness before mixing. Its normalized
vocal spectral ratio is unchanged, and the mix ratio changes only −0.104 dB.
Comparing EQ to this level-only control distinguishes tone from vocal prominence.

Each complete mix receives one scalar to match −24.0 LUFS; true peaks are
−10.9, −11.0 and −11.2 dBTP for unchanged RED, EQ and level-only control.
The EQ and control use the same −1.8 dB whole-mix audition gain, versus
−2.5 dB for baseline. Original-level mode keeps KIT/BASS exactly fixed;
in matched mode they scale with the complete mix. The saved source reconstruction
residual is 0.00591% relative RMS. These are tonal descriptors and listening
controls, not speech-intelligibility, musical-preference or quality grades.

The app publishes the report under RED, with six authenticated checksummed
listening files, its own legacy-client opt-in and no apply action. The common
diagnostic importer supports both RED and KIT while retaining their separate
asset allowlists. Seven diagnostic slots preserve all earlier reports;
all 24 preceding app jobs and their metadata are retained. No Ableton parameter,
transport or desktop control, GPU processing or cloud audio upload is used. The technique is
consistent with [iZotope's vocal EQ guidance](https://www.izotope.com/community/blog/how-to-eq-vocals),
but its frequency/depth choice is a bounded test from this recording, not a
universal vocal prescription.

## Updated foundation versus Bethel and Maverick

`_work/updated-reference-2026-10-22-01` publishes **Updated WCFI ↔ Bethel /
Maverick**. The working RED choice is the −2 dB bell at 400 Hz, Q 1, rather
than the level-only control: it changes the intended spectral ratio while
the control primarily lowers vocal prominence. This remains an engineering
choice for continued comparisons, not a verified preference. KIT keeps gentle
compression, the fixed −1/+2 EQ and the KIT-only 180 Hz body cut; BASS stays
unchanged. All these choices remain local previews; Ableton is untouched.

The comparison uses equal twelve-second excerpts: WCFI 40–52 s from the
existing October 11 analysis passage, Bethel's saved Sunday service at
930–942 s, and Maverick's saved released live performance at 192–204 s.
The exact existing reference WAVs are reused and their source checksums
verified. Four whole-excerpt scalar-only listening copies include WCFI before
RED EQ, updated WCFI, Bethel and Maverick. All measure −24.0 LUFS; no new
download, audio upload or model listening is performed.

| Descriptor | Before RED EQ | Updated WCFI | Bethel | Maverick |
| --- | --- | --- | --- | --- |
| Stereo side energy | 0.246% | 0.285% | 24.415% | 18.969% |
| L/R correlation | 0.995 | 0.994 | 0.512 | 0.621 |
| 200–500 Hz / 1–3 kHz | 10.04 dB | 8.86 dB | 1.05 dB | −1.77 dB |
| Crest factor | 15.17 dB | 15.07 dB | 12.01 dB | 9.10 dB |

The largest structural difference is the narrow, incomplete WCFI foundation:
only RED/KIT/BASS are up, while references contain complete arrangements.
The next experiment is restoring and balancing intended band sources, then
testing placement/filtered ambience and mono compatibility. Side energy does
not identify reverb quality or establish a target percentage. The largest
tonal difference remains low mids versus presence, approximately 7.8–10.6 dB
above the references in this excerpt; this is not a Master EQ prescription.
The measured RED change is smaller than the reference difference and cannot
establish vocal intelligibility or explain the entire mix-quality gap.

Findings screen a side-energy deficit of at least eight percentage points
or a low-mid/presence excess of at least three dB against both references.
These are explicit descriptive rules, not validated perceptual gates.
Identical-audio and scalar-gain tests produce no screened gap. Descriptor
validation recomputes gain-invariant metrics from saved float32 listening
samples, including procedural cases with nearly empty bands; this avoids
false rejection from normalization/quantization noise while rejecting forged
spatial or tonal claims. Short excerpt LRA and peak/RMS differences are not
used to choose broadcast loudness or a Master compressor.

The app report is under MIX, with four authenticated checksummed files,
legacy-client opt-in, explicit foundation-versus-full-mix labels and no apply
action. Eight diagnostic slots preserve earlier reports; all 25 preceding
jobs and metadata are retained. Headless checks mock playback and verify all
four audio responses without real playback, desktop interaction or mutations.
Ableton controls, stopped transport and emulator loop/channel settings are
verified unchanged. No GPU or model inference is used.

## Full-band starting balance with RED/KIT fixes retained

`_work/full-band-2026-10-22-01` contains **October 11 · Full-band balance ↔
Bethel / Maverick**. The unchanged selected RED/KIT/BASS foundation receives
four synchronized additions from the same 11-channel take. Read-only bridge
inspection confirms that the added channels contain only passive Analysis Nodes
and their groups have no processors. The raw files are checksum verified, receive
the existing emulator loop envelope, and use the same 40–52 second passage.
No new capture or host changes are required.

The local preview preserves RED's −2 dB 400 Hz bell; KIT's saved native gentle
compression, −1 dB 120 Hz shelf, +2 dB 3 kHz bell and −2 dB 180 Hz body cut;
and the existing BASS level. RED/KIT/BASS and Master fader equivalents remain
0/+6/+3/−6 dB. Starting additions are:

| Source | Raw 20 s loudness | Preview fader equivalent | Placement |
| --- | --- | --- | --- |
| EGUITAR | −17.7 LUFS | −6 dB | 35% left |
| AC GUITAR | −5.6 LUFS | −24 dB | 35% right |
| KEYS | −27.1 LUFS | 0 dB | Recorded stereo preserved |
| GREEN | −36.2 LUFS | +6 dB | 20% right |

The mono additions use center-normalized equal-power placement, preserving
their stereo-channel power; this is not claimed to reproduce Live's pan law
exactly. GREEN's fader cap gives unity raw-to-mix gain after Master. Its own
performance versus bleed is unconfirmed, so it is not normalized to the lead.
YELLOW and LAPEL are effectively silent, LAPTOP is near-silent and talkback is
excluded from music. No AMBIENT stem exists in this take. Existing returns are
not reconstructed, so this is a dry seven-source full-band preview.

AC GUITAR has 52,292 full-scale channel samples (2.724%) in the raw 20 s stereo
analysis representation. It peaks at 0 dBFS and its high level would dominate
an equal-fader reconstruction. The low preview fader handles balance but does
not restore possible clipped peaks. At the next rehearsal check instrument
output and the physical analogue mixer trim; no analogue control is claimed
available at home. No declipping or new source EQ is applied.

All four twelve-second listening files (foundation, full band, saved Bethel,
saved Maverick) are verified at −24.0 LUFS. Full-band true peak is −11.5 dBTP.
The 200–500 Hz / 1–3 kHz ratio changes from 8.857 to 7.265 dB, side energy from
0.285% to 1.312%, mono fold-down energy from −0.012 to −0.057 dB, and crest
factor from 15.066 to 14.810 dB. Added sources increase the complete arrangement
and change tone/placement; these measurements do not establish an optimum
balance or professional-quality preference. The source recordings remain
narrow and no room/ambience is included yet. Different reference songs and
unknown mastering remain confounds.

The reference writer and saved-sample asset validator are shared with the
preceding reference study. A separate full-band kind, immutable channel plan,
fixed-processing guard and legacy-client opt-in prevent mislabeling the preview
as native output. Nine diagnostic slots preserve all previous reports; all
26 preceding app jobs and metadata are retained. Playback QA uses a separate
muted headless browser with mocked audio, and no Play/Record, mouse, keyboard,
GPU, model inference, new download or cloud audio upload is used.
