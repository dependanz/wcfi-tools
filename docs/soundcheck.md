# WCFI morning soundcheck

This first version calibrates **individual raw inputs from a full-band rehearsal** and writes
a separate Live 11 set. It prepares fixed settings for the service; it does not ride faders
during a performance. It has no cloud dependency and does not require `wcfi setup`.

## What it does

- Reads external audio track names, IDs and routing from a saved `.als` file.
- Measures each mono/stereo raw recording independently. Everyone can play together.
- Chooses an absolute input gain from active 250 ms frames, with a 12 dB maximum boost,
  24 dB maximum cut and a default -12 dBFS input sample-peak ceiling plus 3 dB reserve.
- Adds a source-role high-pass filter and, in `gentle_cuts` mode, at most two broad cuts
  of no more than 3 dB. These are starter heuristics, not standards or a learned professional mix.
- Blocks silent, very quiet, near-full-scale, insufficient, badly imbalanced stereo and
  strongly antiphase inputs. Missing or ambiguous recordings are listed as blocked.
- Creates an HTML report, a JSON plan and optionally a calibrated `.als` copy.

The gain targets are **active-frame RMS dBFS**, not LUFS and not broadcast output targets.
The estimated peak is a sample peak after the input gain only. It is not a true-peak prediction
for the final mix. Existing effects, group gains, sums and Master processing still matter.

## Before tomorrow's rehearsal

```powershell
pip install -e ".[soundcheck]"
wcfi soundcheck init "C:\path\Service.als" --profile "C:\path\soundcheck.json"
```

Review the generated profile once. Track names provide role guesses, not certainty. Supported
roles: `vocal`, `speech`, `electric_guitar`, `acoustic_guitar`, `keys`, `bass`, `drums`, `generic`.
Ambience, talkback, laptop and unknown names are disabled by default. Disable anyone who will
not perform during this rehearsal segment, especially a speech microphone during music.

The saved set must contain a stock Utility and EQ Eight somewhere to provide version-matched
device templates. Third-party EQs do not substitute for those templates. This tool currently
supports Live 11 only and treats the `.als` structure as a version-specific format.

## Capture the band

1. Save the current Live set. The tool cannot see unsaved changes in the open session.
2. Record approximately 60–90 seconds with varied material and everyone enabled in the profile
   actually playing/singing. Full-band recording is supported; solo checks are unnecessary.
3. Use the **raw files recorded from each track's external input**, before Ableton effects.
   Record onto fresh empty space; avoid overwriting existing takes. Stop recording, then save.
4. Use the files in the project's `Samples/Recorded` folder. Do not export processed stems,
   a stereo Master, frozen tracks, or a YouTube recording as calibration input.

The tool cannot prove that a recording is pre-effects. The `capture_kind: "raw_pre_fx"` setting
is an explicit input contract. Post-effects material would cause the wrong correction.

If a player only plays during a later section, use `--start SECONDS` to select that rehearsal
section. `--seconds` defaults to 90, with a 10–300 second range. At least 8 seconds of usable
signal is needed. These options currently apply to all tracks; use explicit trimmed raw files
if tracks need different excerpts. Originals should be retained.

## Calibrate and create the copy

On Windows, the rehearsal launcher finds `Samples/Recorded` beside your source set and
creates uniquely named results and set copies:

```powershell
.\scripts\morning-soundcheck.ps1 -Profile "C:\path\soundcheck.json"
```

It does not change PowerShell execution policy. If scripts are disabled on the machine,
use the Python/CLI commands below instead. The launcher accepts `-RecordedOn`, `-Latest`,
`-Seconds`, `-Start`, and an explicit `-Recordings` folder.

```powershell
wcfi soundcheck calibrate "C:\path\soundcheck.json" `
  --recordings "C:\path\Samples\Recorded" `
  --output "C:\path\soundcheck-sunday" `
  --write-set "C:\path\Service-sunday-calibrated.als"
```

Automatic file binding matches the exact track name or Ableton's numbered recording names
(for example `RED.wav` or `RED 0001 [2026-10-04 090000].wav`). It uses local file modification
dates and defaults to today. Add `--recorded-on 2026-10-04` for a different day. Files copied
between computers may have misleading modification dates: set their exact `file` paths in
the JSON profile instead. Paths there are relative to the profile's folder unless absolute.

With multiple matching takes, choose exact files in the profile or explicitly pass `--latest`.
The latter selects the latest file **per track**, so check the report to confirm that all files
come from the intended rehearsal. Two tracks cannot share the same recording.

Omit `--write-set` to review first, then run:

```powershell
wcfi soundcheck apply "C:\path\soundcheck-sunday\plan.json" `
  --output "C:\path\Service-sunday-calibrated.als"
```

Only `ready` tracks are modified. Blocked/disabled tracks remain unchanged; the report lists
them. `ready` means the technical checks passed, not that the mix has been approved by ear.
An entirely blocked run returns exit code 2. Validation failures return exit code 1.

The copy must be a new `.als` beside the source, preserving its relationship to sample folders.
The source set and recordings are hashed; changing either after analysis invalidates the plan.
Output files and run directories are never overwritten. A failed application validation writes
no set. The process does not save, stop, load or modify the running Live session.

## What changes inside Live

The first Utility is adopted as **WCFI Input Gain**, and its gain is set to an absolute value
derived from the raw recording. Its other stereo settings are preserved. If there is no Utility
first, a neutral Utility is inserted. **WCFI Soundcheck EQ** is inserted immediately after it.
Subsequent passes update these two devices rather than stacking more gain or EQ.

An automated/macro-controlled or muted/bypassed input Utility is rejected rather than silently
overridden. Managed devices must remain in positions one and two. Existing later effects,
third-party plugin state, tracks, clips, faders, sends, group processing, return tracks and
Master routing are preserved. In particular, the tool does not alter outputs 23/24 or configure
the TouchMix or ATEM. Existing later EQ/gain/compression can counteract input calibration;
inspect and audition the whole chain before accepting a new baseline.

## Audition and restore

During rehearsal, save any remaining edits in Live before opening the calibrated copy.
Compare the full-band result through the actual broadcast path:

`Ableton Master 23/24 -> TouchMix Aux 3/4 stereo (only 23/24 sent) -> ATEM line input`

Check each stage's meters, vocal intelligibility and worship-to-speaking balance. A channel
that passed the checks may still contain mostly bleed; disable it and recalibrate if its
performer was absent. Keep the original set to restore immediately if needed.

No algorithm here can recover upstream clipping, create a missing performance, reliably
separate microphone bleed, or guarantee a professional artistic balance. The deliverable is
a bounded, repeatable input-calibration starting point. A future iteration can learn an
approved channel reference and add measured live feedback once rehearsal recordings exist.

## Development and validation

### Compare changes through the complete session

`prepare-session-comparisons` creates a baseline and two small lead-fader alternatives
for offline Master rendering in Live. This is a validation step toward rehearsal automixing;
it does not select a winning mix or implement live control.

```powershell
wcfi soundcheck prepare-session-comparisons "C:\path\Saved-rehearsal.als" `
  --output "C:\path\balance-trial-01" --lead-track-id 8 --start-bar 97 --bars 40
```

Use the lead track's actual numeric ID, not its position in the track list. The new trial
directory must be beside the source set; the three `.als` copies are also written beside
the source to preserve relative sample references. Existing files are never overwritten.
Default alternatives move only the lead fader by -3/0/+3 dB. `--step-db` accepts 0.5–3 dB.
Automated/mapped lead faders and external hardware effects/instruments are refused.

In these offline copies, audio monitoring is set to Off and solos are cleared so that
recorded clips supply the full mix. Track effects, groups, sends, returns and Master state
are preserved. The running session is not accessed. A return routed to an external output
instead of Master will not appear in a Master render. Inspect `trial.json` for exact render
paths and settings. Render each Master without normalization, then verify alignment,
activity and headroom. Use loudness-matched derivatives for blind listening, retaining the
original renders. Summing processed track stems is not an equivalent replacement for
rendering through nonlinear group/Master processors.

### Measure the finished mix

Input calibration alone does not validate the existing compressor, EQ, gain, group, and
Master chains. Export the rehearsal's **Master** from Ableton with normalization off,
stereo output, and the session sample rate. Use a separate inspection set for playback:
switch each recorded input to Monitor Off and verify the Master falls silent after stopping
and allowing effect tails to decay. Selecting tracks inside one group may not select tracks
inside other groups. Current live playing must not contaminate the inspection.

```powershell
wcfi soundcheck inspect-mix "C:\path\rehearsal-master.wav" --output "C:\path\mix-review"
```

Requires FFmpeg on PATH, or `--ffmpeg "C:\path\ffmpeg.exe"`. This read-only command uses
FFmpeg's EBU R128 scanner, writes an HTML report, JSON metrics and the underlying scanner log.
It measures integrated loudness, loudness range, sample peak and true peak. The minute table
summarizes overlapping three-second loudness readings above -70 LUFS; these are not separate
integrated measurements. Times start at the exported file's beginning. A -1 dBTP headroom
warning is a review threshold, not a guarantee against every encoder's overshoot.

This command makes no gain changes and does not assess musical balance or the analog/ATEM
stages. Its output is deliberately not a calibration plan and cannot be passed to `apply`.
Do not give a rendered Master or processed stems to `calibrate`.

### Machine listening to an excerpt

`listen` supplements meters with observations from an audio-input model. It sends actual
WAV audio through OpenAI Chat Completions, not a transcript or an image of a spectrum.
It does **not** change Ableton, touch the source recording, or produce an `apply` plan.
The initial integration uses `gpt-audio-1.5`; `--model` can select another compatible
audio-input model. Model access depends on the API account.

```powershell
wcfi soundcheck listen "C:\path\rehearsal-master.wav" `
  --start 1020 --seconds 30 --output "C:\path\listening-review" `
  --context "Full-band church rehearsal; review vocal audibility and instrument balance."
```

This command uploads the selected excerpt and context to **OpenAI** and incurs API usage.
It uses `OPENAI_API_KEY` (including the project's `.env` support) or the existing OS-keyring
credential from `wcfi setup`. Never put a key in a command argument or a report. Other
soundcheck commands still require no API key. Run with `--offline` to prepare the same
audio and measurements locally without reading credentials or contacting a model:

```powershell
wcfi soundcheck listen "C:\path\rehearsal-master.wav" `
  --start 1020 --seconds 30 --offline --output "C:\path\listening-preparation"
```

The excerpt must be 10–60 seconds of mono/stereo audio at 8–48 kHz; the default is 30 seconds.
The command rejects an interval extending beyond the file rather than silently shortening it.
Start times are **file-relative**, which can differ from Arrangement time. FFmpeg is required
on PATH or supplied with `--ffmpeg`. Use an active performance segment. This first version
reviews one file per call: a rendered mix or a separately exported individual track. Separate
calls do not share listening context, and this is not yet a synchronized multi-stem comparison.

Each new results directory contains:

- `excerpt-original.wav`: the selected samples in float WAV, measured locally.
- `excerpt-upload.wav`: the exact PCM16 WAV prepared for the model, with stereo and polarity
  preserved. Only samples exceeding PCM16's range cause protective attenuation, documented
  in the report. No EQ, compression, resampling or mono summing is applied.
- `measurements/`: independent FFmpeg loudness/peak results for the original excerpt.
- `request.json`: prompt, source/excerpt hashes, selected interval and intended destination;
  no credentials or base64 audio. The full source recording, local filename/path, and meter
  readings are not sent to the model. Context text is sent as entered.
- `listen.json` and `report.html`: explicit `prepared_offline`, `reviewed`, or `failed` status,
  plus measurements separated from any model observations and returned usage information.

The request uses text output, `store=False`, and no automatic retries. This does not promise
zero provider retention; provider data policies still apply. An API failure leaves local
evidence and a failed report, returns a nonzero exit code, and never fabricates observations.
Choose a new results directory to retry; a timeout may still represent a billed request.
An offline report explicitly says that no model listened.

The model receives no meter results or diagnostic claims from this tool, helping avoid
prompting it toward a predetermined diagnosis. It is instructed to distinguish uncertain
listening observations from measurements, avoid invented EQ/compressor numbers, and suggest
further listening checks. These instructions cannot guarantee reliable musical judgment.

Before using observations to drive automatic mixing, run a blind listening evaluation:

1. Use aligned exports of a real mix and its individual sources. Keep an untouched reference.
2. Create labeled ground truth privately: for example, lower only the lead vocal, boost a bass
   source, or add known clipping. Include unchanged controls and more than one musical section.
3. Randomize neutral clip labels; send no filenames, expected issues, or meter values in context.
   For tonal/balance comparisons, loudness-match comparison copies using a fixed method and
   retain conversion logs so overall level does not supply the answer.
4. Check whether the model identifies the introduced issue and avoids inventing issues in
   controls, across repeated trials and different sections. Have a listener assess the same clips.
5. Record errors and disagreement. A plausible report or passing software tests alone does
   not establish listening accuracy. The initial implementation has no validated quality score
   and never automatically applies its opinions to the live mix.

API reference: [OpenAI audio inputs](https://developers.openai.com/api/docs/guides/audio-chat-completions).

#### First real listening check (October 11, 2026)

The API integration successfully sent 30 seconds of the October 4 Master export
(file time 17:00–17:30) to `gpt-audio-1.5`. A second, explicitly authorized request
used the same prompt and an altered copy: +18 dB, hard clipping at +/-1, then reduction
to the original whole-excerpt RMS. About 68.44% of samples hit the clipping operation.
The modification and source/upload hashes were verified. The model was not told the
alteration, filenames, or measured levels.

The model reported no obvious distortion in both clips. It **failed to flag the introduced
fault in this trial**. The two clips measured -7.8 and -7.4 LUFS, respectively; RMS matching
was used, not a formal perceptual loudness match. This is one test, not an aggregate accuracy
estimate, but it does not support using the model's reassurance as mix approval or automatic
mix control. The original observations remain unvalidated. No Ableton settings were changed.

Local evidence: `C:\Users\Danze\Documents\ChatGPT\wcfi\soundcheck\2026-10-11-listening-validation`
contains `ground-truth.json`, `validation-result.json`, the altered clip, and its `review-b`
report. The original listening report is in the adjacent `2026-10-11-listening-trial-01` folder.

#### Repeatable blind test while Ableton is live

`scripts/listening-benchmark.py` reads a **finished WAV**, creates comparison copies, and
optionally submits them through the existing listening integration. It does not open an
audio device, control Ableton, or apply settings. Use an existing export while broadcasting;
creating a new Ableton export from the active live set is a separate operation.

Prepare two file-relative sections, each with an unaltered control, hard-clipped copy, and
a deliberately muffled copy. The preparation uses one processing thread and below-normal
process priority on Windows. No model is downloaded or run on the local GPU.

```powershell
.\.venv\Scripts\python.exe scripts/listening-benchmark.py prepare "C:\path\finished-master.wav" `
  --output "C:\dev\wcfi-tools\_work\listening-benchmark-01" `
  --starts 1020 1140 --seconds 15 --seed 20261011 `
  --ffmpeg "C:\path\ffmpeg.exe"
```

The six copies receive shuffled neutral IDs. Their integrated loudness is matched to a
common target at or below -18 LUFS, verified within 0.21 LU, with at least approximately
3 dB of true-peak headroom. The saved PCM16 files preserve the source channels and polarity.
The clipping operation occurs before matching: a low final peak does not erase distortion.
The muffled copy uses a sixth-order zero-phase low-pass at 1500 Hz. These are diagnostic
faults, not proposed EQ or gain settings for the service.

`blind-manifest.json` contains only neutral clip IDs, paths, durations and hashes. The separate
`private-answer-key.json` records source provenance, transformations and local measurements;
`private-measurements` preserves scanner logs and the copies before matching. Preparation
never reads credentials or uploads audio. Only a fully prepared manifest can be submitted:

```powershell
.\.venv\Scripts\python.exe scripts/listening-benchmark.py submit `
  "C:\dev\wcfi-tools\_work\listening-benchmark-01" `
  --model gpt-audio-1.5 --ffmpeg "C:\path\ffmpeg.exe"
```

Submission sends six independent, serial requests to OpenAI using the existing saved key.
Only each short WAV and the same neutral listening prompt are sent; the runner does not
read the answer key or supply filenames, meter readings or expected faults to the model.
This incurs API charges and network traffic. Processing limits reduce local load but are
not a guarantee against computer or network resource contention during a livestream.

`reviews/<clip-id>` holds each listening report. `submission.json` lists completed requests
and token usage. A failed request stops the remaining requests and saves a redacted failure;
an attempted submission cannot be automatically repeated. No model-generated judgment is
applied to the live mix.

Compare the returned descriptions with the private key after submission. A control means
**no added fault**, not a certified clean recording. Two excerpts from one recording and one
review per clip do not establish general accuracy. Quiet lead vocals, excessive bass-player
level, and channel EQ need separate tests using aligned processed stems. The benchmark does
not synthesize those channel faults from a stereo Master or automatically score vague reports.

The October 11 six-clip trial used 15-second sections at 17:00 and 19:00 from the October 4
Master export. All six upload hashes matched the prepared copies, all measured -18.0 LUFS,
and all six API requests completed. The model reported no obvious distortion in both
hard-clipped versions (67.9% and 41.8% of channel samples affected before matching). Tonal
comments on the low-passed versions were ambiguous or partial. This trial also fails to
support automatic mixing decisions. Human audible validation remains outstanding.
Local evidence and the text-only summary are in
`C:\dev\wcfi-tools\_work\listening-benchmark-2026-10-11-01\report.html`.

#### Local machine listening

An isolated `.venv-listen` supports two local engines. `audiobox` uses Meta's
[Audiobox Aesthetics](https://github.com/facebookresearch/audiobox-aesthetics) to predict
production-quality ratings. `music-flamingo` uses NVIDIA's
[Music Flamingo](https://huggingface.co/nvidia/music-flamingo-2601-hf) for descriptions.
These are separate evidence types: learned ratings/descriptions are not signal measurements.
Neither engine applies settings or certifies a professional mix.
The CLI and benchmark default to the CPU `audiobox` engine. Silent, constant/DC,
non-finite and strongly cancelling stereo excerpts are rejected before model inference.

Install on a compatible NVIDIA Windows machine **outside broadcast time**. The tested
runtime uses Python 3.12 and the official CUDA 12.6 PyTorch wheels; it does not need a new
driver or system CUDA toolkit. Keep the existing `.venv` for ordinary tools.

```powershell
.\.venv\Scripts\python.exe -m venv .venv-listen
.\.venv-listen\Scripts\python.exe -m pip install torch==2.7.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu126
.\.venv-listen\Scripts\python.exe -m pip install -r requirements-listening-local.txt
.\.venv-listen\Scripts\python.exe scripts/local-listener.py download-quality --output _work/models/audiobox-aesthetics
```

The downloader pins the public model revision, downloads safetensors and configuration,
and checks the weight file against its official SHA256. No private recordings or API keys
are involved. It can resume completed HTTP chunks after interruption; it refuses to replace
a completed checkpoint. Audiobox weights are approximately 416 MB and use CC-BY-4.0.
Inference is CPU-only float32, reads local files, and never invokes an auto-downloading
predictor or pickle checkpoint.

```powershell
.\.venv-listen\Scripts\python.exe -m wcfi_tools.cli soundcheck listen-local "C:\path\finished-master.wav" `
  --engine audiobox --checkpoint _work/models/audiobox-aesthetics/https-checkpoint `
  --start 60 --seconds 30 --output _work/local-review-01 --ffmpeg "C:\path\ffmpeg.exe"
```

The original stereo excerpt is retained. Both engines require mono 16 kHz, so the model copy
uses an arithmetic channel mean and `scipy.signal.resample_poly`; conversion gain and exact
input hash are recorded. Strong mono cancellation is refused. The engines cannot judge
stereo width or frequencies above 8 kHz. Reports include separate original-excerpt meters,
model provenance, exact model input, timing and limitations. Existing results are immutable;
no audio device is opened, no playback occurs, and nothing is uploaded.

Run the same blind trial locally, without reading its private answer key:

```powershell
.\.venv-listen\Scripts\python.exe scripts/local-listener.py benchmark _work/listening-benchmark-01 `
  --engine audiobox --checkpoint _work/models/audiobox-aesthetics/https-checkpoint `
  --output _work/audiobox-trial-01 --ffmpeg "C:\path\ffmpeg.exe"
```

On October 11, the local Audiobox model ranked all four introduced faults below their
matching controls: control PQ 7.940/7.742, clipping 3.430/3.874, low-pass 4.399/5.207.
All six exact mono-input hashes were verified. This is four expected directional comparisons
on two sections of one recording, **not a general accuracy estimate** or a channel diagnosis.
An additional level-sensitivity check used the same controls at 0, -6 and -12 dB; their PQ
scores changed by at most 0.419. Continue to loudness-match before/after comparisons, and
do not optimize a mix solely to maximize this learned score. Human validation, source-balance
tests and sibilance tests are still outstanding. Evidence is in
`_work/listening-audiobox-2026-10-11-01/report.html` and
`_work/audiobox-level-check-2026-10-11-01/result.json`.

Music Flamingo is an experimental descriptive backend. Download its approximately 16.5 GB
checkpoint with `download-http --output _work/models/music-flamingo`; select its
`https-checkpoint` directory with `--engine music-flamingo`. It uses an NF4 decoder and a
float16 audio encoder/projector to fit a smaller GPU, with SDPA attention. This adaptation
must be tested on the actual machine and separately validated for mix-quality observations.
NVIDIA's weights have a noncommercial research license; the present use is evaluation.
Do not run this large GPU model during a livestream.
On October 17, the laptop unexpectedly restarted during a local GPU trial. Windows
recorded unexpected shutdowns, but the cause has not been established. The GPU trial
is incomplete and is not evidence of validated listening expertise. Further GPU work
on this laptop is suspended; the experimental backend requires explicit `--allow-gpu`
before it can load CUDA. The CPU-only Audiobox engine remains the default.

On October 21, a separate local Music Flamingo NF4 trial was interrupted by another
unexpected laptop restart. One response survived and claimed tonal differences in an
identical-audio control; it also violated the JSON response contract. The research runner
now exits before loading inference libraries while its `gpu-inference-paused.json` marker
exists. Windows restart/firmware performance-limiting events do not establish the cause.
Do not automatically repeat that GPU workload on this laptop.

A separately approved NVIDIA/Hugging Face public-demo trial uploaded four 41-second
comparisons. Three returned malformed responses; the fourth returned an error with no
details, leaving eight files unsubmitted. No cause or quota failure was established.
The hosted checkpoint, preprocessing and generation settings could not be verified;
this incomplete endpoint test is not a numerical benchmark of a pinned checkpoint.

The existing Audiobox model completed an additional numerical experiment on all twelve
frozen KIT/full-mix pairs: 24 independent side inferences, one logical CPU at idle priority,
one Torch thread, deadlines and pauses, with no GPU initialization. All four identical
pairs had equal PQ ratings and all four filtered controls changed PQ. Raw KIT ranked
slightly higher than processed KIT in both orders: isolated 6.824269 vs 6.789362, full mix
6.714256 vs 6.624045. These small numerical differences have no calibrated audibility or
human-preference significance and do not justify a processing change. This numerical
assay does not answer the language-model protocol's brightness/reasoning questions.
Zimtohrli also completed all twelve distance comparisons, returning zero on identical
audio; distance is not a quality preference. Neither component certifies professional
mixing or diagnoses a complete channel chain.

All source files, raw responses, approval scope, resource bounds and results are preserved
under `_work/music-listener-2026-10-21-01/`; `review.html` distinguishes the interrupted
language-model runs from the completed numerical experiments. No model was trained,
no processing settings were changed, and no Ableton transport or desktop controls were used.

```powershell
pip install -e ".[dev,soundcheck]"
pytest -q
ruff check src/wcfi_tools/soundcheck src/wcfi_tools/commands/soundcheck.py tests/test_soundcheck.py
```

Tests cover audio faults, gain normalization, bounded EQ, exact file selection, stale plans,
repeatability, immutable outputs and preservation of unrelated set state. The writer was also
exercised on a copy of the church's saved Live 11.3.35 set using synthetic audio, checking
unique global IDs and preservation of routing, clips, faders, groups, returns and Master.
The generated copy has not yet been opened and auditioned in Live with real rehearsal material.

### Source-specific listening validation

`prepare-comparisons` creates a reproducible research trial from aligned processed stems.
It does **not** approve a mix, run a model, or apply a calibration plan. Known source
transformations and human judgments are separate evidence: an EQ boost can improve a poor
baseline, and a quieter lead can improve an overly loud baseline.

Export aligned stereo float WAVs at the session rate, normalization off, with
**Include Return and Master Effects off**. Ableton's individual-track export is post-fader;
it does not automatically recreate the full downstream processing when stems are summed.
See the [Live 11 export documentation](https://www.ableton.com/en/live-manual/11/managing-files-and-sets/#exporting-audio-and-video).
Never sum a group and its children together. Do not subtract a separately rendered vocal
from a processed Master: nonlinear group and Master effects invalidate that assumption.
Check solos and track activators before exporting in an offline inspection workflow.
The October 17 open set had RED soloed, producing silent backing exports despite visible
recorded waveforms. The preparer rejects a silent backing mixture instead of scoring it
as a vocal-balance test. Clearing a solo on a live broadcast is a separate live action.

Declare an explicit test mix in JSON. This example combines RED with the other vocal
track outputs and the already summed INSTRUMENTS and MISC group outputs. It excludes the
VOCALS group output, all INSTRUMENTS/MISC children, shared returns, and Master processing.
Consequently this is a source-aware **test mix**, not the finished broadcast Master.

```json
{
  "kind": "wcfi_stem_validation",
  "mix_basis": "Processed RED and other vocal track outputs plus INSTRUMENTS/MISC group outputs; VOCALS bus, shared returns and Master processing omitted.",
  "lead": {"file": "stems/RED.wav", "role": "lead_vocal"},
  "backing": [
    {"file": "stems/YELLOW.wav", "role": "backing_vocal"},
    {"file": "stems/GREEN.wav", "role": "backing_vocal"},
    {"file": "stems/INSTRUMENTS.wav", "role": "instruments_bus"},
    {"file": "stems/MISC.wav", "role": "misc_bus"}
  ],
  "starts": [120, 280],
  "seconds": 15,
  "seed": 20261017
}
```

```powershell
wcfi soundcheck prepare-comparisons "C:\path\validation-profile.json" `
  --output "C:\path\source-trial" --ffmpeg "C:\path\ffmpeg.exe"
```

All stems must have the same rate, channel count and frame count. Duplicate source paths,
non-finite samples, insufficient source signal and excerpts past the end are refused.
The two sections each produce an unchanged control, RED at -6/-12 dB, a +9 dB peaking EQ
at 300 Hz on RED, a 4.5–7.5 kHz boost during RED's HF-rich events, and an identical sham
control. These intentionally exaggerated probes are **not suggested settings**.

Every comparison is matched to one common integrated-loudness target at or below -18 LUFS,
with at least approximately 3 dB true-peak headroom. The private key records transformations,
matching gain, exact hashes and verified output measurements. HF events are selected by
band-energy ratios, not phoneme recognition: breaths and instrument bleed can also qualify.
Human confirmation of actual S/SH sounds remains necessary, particularly for sermon material.

Open `human-review.html` before the model ratings or private key. Its players use the exact
mono 16 kHz PCM16 inputs prepared for the model, so both judge the same audio domain.
It has no autoplay or network access and lets the listener save A/B/Same/Cannot-assess
judgments as JSON. The original stereo comparison files are retained separately.
`private-human-pairs.json` holds the randomized pair mapping separately. The same neutral
`blind-manifest.json` is accepted by the existing CPU benchmark runner:

```powershell
.\.venv-listen\Scripts\python.exe scripts/local-listener.py benchmark "C:\path\source-trial" `
  --output "C:\path\source-model-reviews" `
  --checkpoint _work/models/audiobox-aesthetics/https-checkpoint --ffmpeg "C:\path\ffmpeg.exe"
```

Evaluate model preference against human judgments, check identical-copy consistency, and
inspect whether the intervention survives the model's exact mono 16 kHz input. A signal
test or a directional PQ change alone does not establish useful mixing judgment. None of
these results is an `apply` plan; live gain riding, automatic EQ and de-essing remain unvalidated.

### First human source-comparison review (October 17, 2026)

One listener reviewed eight pairs from two 15-second sung passages. Both clearer-lead
choices selected the unattenuated vocal, and both identical-copy checks were marked Same.
Notes identified the added HF sharpness in both passages. In pair 07 the raw answer selected
the preferred, less harsh version despite the question asking for the harsher one; the raw
answer is preserved and its explicit note interpreted separately. Future forms use plain
"sharper, harsher" wording and bind saved judgments to the trial manifest hash.

EQ preferences differed by passage: original tone in the first, the 300 Hz-boosted test
version in the second. These are preferences, not pass/fail answers. The listener explicitly
distinguished clearer vocals from a better overall mix; neither control is an approved
reference mix.

The local model's overall production-quality score moved by less than 0.008 for the EQ
probes and less than 0.046 for the HF probes. Reducing the lead by 12 dB changed it by
+0.012 in one passage and -0.479 in the other. Overall quality and the human questions
about clarity or harshness measure different things, so an aggregate accuracy score would
be misleading. This trial does not validate automatic balance, EQ or de-essing from that
score, or establish sermon/lapel behavior from sung material.

The original submission, pair-level comparison and report are retained under
`_work/mix-validation-2026-10-17-01/human-comparison-01/`. The next calibration design should
use explicit source roles and independent signal measurements, keep the learned quality
score advisory, and establish a preferred full mix before using reference-based matching.
