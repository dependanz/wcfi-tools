# Source-aware evaluation on saved audio

The new layer accepts explicit roles and synchronized source contributions. It
does not infer that a frequency band is a singer, guitar or drum. It does not
play audio, contact Ableton, access audio hardware, control the desktop or upload
recordings. Commands restrict the worker to one logical CPU at idle priority,
one numerical thread, no GPU runtime, an offline socket guard, and a 2 GiB
working-set check. The benchmark has a 40-minute deadline. Its ffmpeg child
decodes local files with software AAC, one decoder/encoder thread and inherited
CPU affinity. Resource measurements are for the Python worker, not a systemwide
measurement of other applications.

## What is measured

`audio/source_evaluation.py` accepts raw inputs and **actual post-mixer source
contributions**: output after processing, channel fader, pan and routing. A node
after effects but before the fader does not supply this without mixer gain/pan.
Role labels support vocals, backing vocals, drums, bass, guitar, keys and other.
Combine only channels genuinely belonging to that role; a combined vocal stem
cannot separately describe the lead and backing singers.

For each role, it computes stereo mean-square energy in 250 ms frames, in the
full band and three overlapping bands: body 40–250 Hz, low mids 150–650 Hz and
presence 700–3500 Hz. Relative level is

```
10 log10(role energy / sum of other-role energies)
```

The median and 10th/90th percentiles use frames active in the **raw** input.
Turning down a candidate does not disable its test. These are energetic feed
ratios, not a psychoacoustic masking calculation or a desired vocal-to-band
target. Different roles can be correlated; summed role powers are not the
power of the final waveform.

Activity means **raw signal energy**, not confirmed performer activity. A vocal
mic with only drum bleed can still look active. The layer needs role/activity
context from a reliable classifier or an operator before diagnosing that case.
It warns for very strongly correlated feeds, but that does not detect every
form of bleed. It cannot distinguish every physical instrument inside a mic.

Supply an aligned Master pre-effects recording to check that the contributions
sum to it (relative residual power <=1e-5). Missing returns, different pan laws,
incorrect fader gains or latency can cause rejection. A shared nonlinear Master
processor does not have a unique per-source decomposition. These measurements
can be context for Master post-effects evaluation; they are not measurements
of individual instruments after that processor.

## Two different validation questions

1. **Human mix preference:** MED saved mixes/ratings joined with MixParams
   annotations. Estimates use raw source LUFS plus fader gain, summed as powers
   by role. This omits bus processing, plugin gain, phase, automation and timing;
   source LUFS is not the measured loudness of the exact excerpt. It must not be
   called measured post-effects balance. The study compares 32 global audio
   descriptors, 16 annotation estimates, and both on the same 66 mixes/10 songs.
   Two ambiguous duplicate-track mixes are excluded, and the `MgC`/`McG` spelling
   normalization is recorded. Previously examined songs are held out of fitting
   and tuning, but do not constitute a new pristine final research test.
2. **Actual source-balance measurements:** the official MUSDB18 seven-second
   sample supplies real vocals, drums, bass and OTHER stems. All 50 official test
   songs are held out. Four overlapping-artist training songs are excluded,
   leaving 90 training songs. OTHER does not identify guitars or keys separately;
   there are no calibrated guitar/keys/backing-vocal ranges in this model.
   Twenty controls per fully active test song change one source by -60, -12, -6,
   +6 or +12 dB. Original and changed mixes have identical RMS, and unchanged raw
   signals provide activity context. Additional identity and common-gain
   controls test invariance. These perturbations have **no human quality labels**.
   They test preservation of original balance, not professional quality.

Human fitting uses paired ratings excluding either mix author. All mixes from a
song stay in the same outer fold. Inner whole-song folds choose a regularized
linear or concave quadratic ranker by selection regret, then pair log loss.
Standardization, weighting and hyperparameter choices see training songs only.
At least three shared listeners are required; the two lower-rater songs are
exploratory. Results for eight or more listeners are also saved (some song rows
then have no evaluable pairs and must not count in the denominator).

The actual-stem model fits broad training ranges: 5th–95th percentiles with a
3 dB margin, requiring at least five supported training songs per feature.
Outside-range values produce signed dominance/burial alerts. The squared
deviation cost is **anomaly**, not quality; intentional arrangements can be
unusual and correct. Unsupported role ranges stay uncalibrated. Clean-song
false alarms and control misses are reported instead of adjusting thresholds
after seeing test results.

Neither annotation preference weights nor range costs are enabled for automatic
changes. They are separate feature schemas and cannot be substituted for one
another. Signal-health checks from the previous study remain separate; this
work does not establish hum removal, EQ targets or safe compression settings.

## First run: neither validation gate passed

- On 400 held-out +/-12 dB source controls, the range score strictly preferred
  the original balance 348 times (87%), tied 33 times and reversed 19 times.
  Half credit for ties gives 91.1%; that is not 91.1% strict detection. Strict
  original wins by role: vocals 97/100, drums 78/100, bass 83/100, OTHER 90/100.
- All 200 near-removal (-60 dB) controls preferred the original and alerted on
  the affected role. Identity error was zero; common-gain feature error was
  below 0.000001 dB. Candidates were matched within 0.000001 dB mixture RMS.
- Fourteen of 50 unchanged original excerpts triggered range alerts (28%),
  exceeding the frozen 20% limit. These are original-control alerts, not human
  confirmation that those mixes are faulty. Broader training ranges or a
  different activity model need a new prospective validation, not retuning on
  these same outcomes and relabelling them a fresh test.
- In the 66-mix/10-song human preference comparison, global-only accuracy was
  71.9%, source proxies alone 66.7%, and combined 71.4%. Conditional paired
  song-bootstrap improvement interval: -14.44 to +11.59 percentage points.
  Songs with decisively better alternatives than the selection fell from 6/10
  to 4/10; mean maximum paired rating regret fell from 0.131 to 0.089. Selection
  improved descriptively, but improved accuracy was not established.
- Runtime: 160.73 seconds wall, 127.92 seconds Python CPU, 145 MiB peak Python
  working set. One logical CPU, idle priority, no GPU modules or benchmark
  network attempts. Forty-five focused tests passed, including manifest reads,
  routing rejection, muted-source support, common-gain invariance and prior
  preference/source-balance/physical-health regressions.

The descriptive audit/report is `_work/source-aware-2026-10-22-01/assessment.html`.
It preserves both failed gates, the 14 unchanged-control alerts and fold/split
audits. Automatic edits stay disabled; source measurements are useful context,
but a reliable professional-quality judge is not yet established.

## Reproduce the frozen benchmark

The public inputs and protocol for this run are under
`_work/source-aware-2026-10-22-01`. `acquire_sources.py` downloads public metadata
and the official sample; it never uploads church recordings. Downloads and
decoded stems remain local research assets and are not bundled with the app.
Follow the source datasets' terms for any reuse or redistribution.

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference prepare-source-aware _work/source-aware-2026-10-22-01 --previous _work/mix-preference-v2-2026-10-22-01 --assets _work/songbench-external-2026-10-22-01 --output _work/source-aware-2026-10-22-01/protocol-02
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference test-source-aware _work/source-aware-2026-10-22-01/protocol-02 --output _work/source-aware-2026-10-22-01/run-02 --ffmpeg _work/reference-audit-2026-10-18-01/runtime-bin/ffmpeg.exe
```

Use fresh output directories. Input/code hashes must match the frozen protocol;
later code changes require a new protocol. The primary completed artifacts are
`run-01/report.html`, `human-results.json`, `source-controls/results.json`,
`source-controls/test-predictions.json` and `audit.json`. Detailed model folds,
original measurements, mutated conditions, decoded hashes and uncertainty are
retained. Automatic application stays disabled regardless of a gate outcome.

## Inspect an aligned saved-source window

```json
{
  "kind": "wcfi_source_contributions_v1",
  "stage": "master_pre_fx",
  "alignment_verified": true,
  "start_seconds": 0,
  "duration_seconds": 7,
  "master_pre": "master-before.wav",
  "sources": [
    {"id": "RED", "role": "vocals", "raw": "red-raw.wav", "contribution": "red-after-mixer.wav"},
    {"id": "KIT", "role": "drums", "raw": "kit-raw.wav", "contribution": "kit-after-mixer.wav"}
  ]
}
```

Paths are relative to the manifest. All files must share sample rate, time
origin and processing-latency compensation. Mono files become dual mono;
candidate stereo pan is retained. Include every contributing source and return
in the reconstruction, not just the two illustrated entries.

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference inspect-sources path/to/manifest.json --output _work/my-source-report.json
```

An optional `--model` reads a compatible training-range model, without fitting
on the inspected song. Outputs retain source hashes, raw activity support,
measured role ratios, optional reconstruction error and uncertainty warnings.

Primary dataset documentation:

- [MixParams annotations](https://huggingface.co/datasets/mclemcrew/MixParams)
- [MixParams methodology and omissions, Appendix H](https://arxiv.org/html/2507.06329)
- [MED browser and expert ratings](https://github.com/rd-conx/med-web)
- [Official MUSDB sample API](https://sigsep.github.io/sigsep-mus-db/)
- [Official source-stream mapping and sample URL](https://github.com/sigsep/sigsep-mus-db/blob/master/musdb/configs/mus.yaml)
