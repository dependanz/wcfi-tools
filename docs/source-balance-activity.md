# Shared-activity balance evaluation

The v3 saved-source evaluator fixes the inconsistent time populations in v2.
It is available through `audio mix-preference inspect-sources` with an explicit
v3 model. The previous evaluators and their frozen experiment results are
preserved. This is an experimental anomaly measurement, not a professional mix
quality score or an automatic fader controller.

## What changed

Previously, each role's source/rest ratio was pooled over that role's own active
frames. For example, vocals could describe the sung phrase while OTHER described
the whole excerpt. Those independently pooled coordinates were then treated as
one simultaneous composition. Even taking coordinate-wise medians on common
frames would not guarantee a coherent composition with three or more sources.

For context C and raw activity flags a[t,r], v3 uses the same frames for every
role: S = {t: all a[t,r] are true for r in C}. At least one second of 250 ms frames
must be available. Otherwise the result is **unknown**, not clean. Support is
always derived from raw input. Turning down a candidate cannot remove a source
from its own evaluation.

On S, candidate powers P[t,r] are normalized to shares q[t,r]. Their geometric
means are closed to one composition:

```
q[t,r] = P[t,r] / sum_j P[t,j]
g[r]   = exp(mean_t(log(max(q[t,r], 1e-12))))
c[r]   = g[r] / sum_j g[j]
x[r]   = 10 log10(c[r] / sum_{j != r} c[j])
```

The relative floor preserves common-gain invariance and finite measurements for
muted sources. Final ratios are not independently clipped. Two-source ratios
are reciprocal, and all contexts correspond to shares summing to one. If all
candidate powers are zero in a supported frame, the result is unknown rather
than a fabricated uniform mix. Frame order and audio outside shared support
cannot change the balance vector.

The report exposes shared duration, fraction of the excerpt, per-role activity
duration, composition, and abstention reason. Activity means energy support;
it does not prove that the singer is singing or distinguish bleed from a
performer. Guitar, keys and other instruments still share the OTHER bucket.

## Controlled comparison

This change uses the previous covariance geometry, alpha 0.25, and the same
61 fitting / 29 calibration songs. All eleven supported role contexts retain
61/29 songs. There is no new parameter search. The weights and thresholds are
recomputed for the corrected representation. Alpha 0.25 does not guarantee a
20% alarm target; calibration also depends on exchangeability across datasets.

The old 50 MUSDB validation recordings, 20 BabySlakh recordings and two MedleyDB
recordings are now regression data. They cannot serve as fresh test songs.
On the reused 50, v2 flagged 11 originals and v3 flagged 9; both detected
358/400 severe +/-12 dB perturbations. Correct affected-role alerts changed
339 to 342; strict original ranking changed 375 to 373. Equal aggregate recall
does not rule out swapped misses.

For the previously diagnostic Liz Nelson example, the two measurements become
-4.79/+4.79 dB over the same 3.25 seconds, replacing -7.49/+16.45 dB from different
windows. The original still triggers an anomaly; mathematical consistency does
not imply that its original balance is correct or wrong to a listener.

## Evaluation policy

`_work/activity-window-2026-10-22-01/preanalysis-plan.json` freezes the hypothesis,
model hashes, controls and acceptance criteria before new public audio is scored.
One take per composition is selected from public JazzSAMBA recordings. Shared
performers and instrumental jazz limit independence and vocal/church relevance.
No user listening, GPU, playback, DAW operations, audio device access or audio
uploads are part of the evaluation.

The gate requires at most 20% unchanged-control alerts, complete coverage of
eligible multi-role recordings, no severe-control loss in detection, affected
role/direction or strict ranking versus both earlier models, and no newly missed
severe any-alert cases. Unknown controls count as misses. Separate reporting on
originally unflagged recordings prevents an always-alert detector from appearing
useful merely through high recall. Identical/common-gain controls and actual PCM
gain mutations check the numerical shortcut.

Unchanged originals are not certified flawless. Introduced gain changes are
engineering controls, not human-validated bad mixes: a change can improve an
already unbalanced original. These results cannot certify professional quality.
Automatic editing remains disabled independently of control performance.

## Reproduce

Inspect an aligned saved-source manifest with the new model:

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference inspect-sources path/to/sources.json --model _work/activity-window-2026-10-22-01/model-v3.json --output _work/new-source-inspection.json
```

Use a new output path on every run. Frozen validation checks source, model,
manifest and code hashes. Replaying an examined test does not create a new
held-out test.

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference test-balance-activity _work/activity-window-2026-10-22-01/public-data/fresh-audio.json --protocol _work/activity-window-2026-10-22-01/final-protocol.json --output _work/activity-window-2026-10-22-01/replay-01
```

The suite of source, calibration, artifact and preference tests passes: 78 tests.
The new tests explicitly check four-role composition closure, two-role
reciprocity, support duration, gain invariance, silent candidates, song/artist
leakage, frozen-file checks, incomplete acquisition, and swapped detection misses.

## Frozen external result

All 20 planned JazzSAMBA recordings were acquired with no substitutions. The
independent offline source audit recomputed every excerpt and exactly matched
all 60 saved float32 role files. Published-mixture reconstruction residual power
was 1.72e-8 to 1.03e-7. Source matrices had full column rank; maximum measured
cross-role zero-lag correlation was 0.0841. This does not exclude delayed or
frequency-specific bleed. Recordings share performers, and include no vocals.

| Measure | Legacy spectral limits | Previous joint v2 | Shared-activity v3 |
| --- | ---: | ---: | ---: |
| Unchanged originals flagged / assessed | 20/20 | 9/20 | 6/19 |
| Eligible recordings assessed | 20/20 | 20/20 | 19/20 |
| +/-12 dB controls detected | 114/120 | 84/120 | 93/120 |
| Correct affected role and direction | 82/120 | 75/120 | 84/120 |
| Original ranked less anomalous | 69/120 | 91/120 | 95/120 |

**The frozen gate failed.** V3's 31.6% unchanged-alert rate exceeds 20%, coverage
is incomplete, and it newly misses two severe controls detected by v2. Both new
misses belong to the abstained Blue Skies excerpt: all three sources overlap for
only 0.25 seconds, despite each having more separate activity. All six severe
controls on this recording count as missed, not correct or clean. The earlier
v2 original was unflagged, so abstention did not remove a previously flagged
original from the comparison.

V3 recovered eleven severe detections missed by v2. On the same nineteen
assessable recordings it loses no v2 severe detections, but that descriptive
subset cannot erase the coverage failure. On the ten recordings both models
originally considered unflagged, detection improves from 43/60 to 50/60 and
strict ranking from 50/60 to 54/60. Neither baseline judgment is a human label.
The legacy method flagged every original, which limits the meaning of its high
any-alert recall. Async and sync results are separately reported in the artifact.

Evaluation checked all 300 gain mutations against actual PCM, plus identical
and common-gain controls. Maximum PCM shortcut discrepancy was 5.0e-7 dB. It
took 34.22 seconds wall / 30.95 seconds CPU, with 128.2 MiB peak working set,
one logical CPU at idle priority, one numerical thread, no GPU runtime and zero
network, playback, device, DAW or desktop calls. Acquisition and the independent
13.12-second preparation audit are separately recorded.

Keep the mathematically corrected representation, but do not promote the
detector as a reliable professional-quality judge. Partial-overlap analysis and
learned balance targets require further development and new validation. All
twenty recordings have now been examined; they are regression data for future
changes, not a fresh test pool.

Full report: `_work/activity-window-2026-10-22-01/assessment.html`.
Source: [official JazzSAMBA release](https://zenodo.org/records/22943963).
