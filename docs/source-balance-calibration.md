# Source-balance false alarms and joint calibration

This follow-up investigates the fourteen unchanged MUSDB excerpts flagged in
the first source-aware experiment. All work uses saved files on CPU. It never
plays audio, controls Ableton, opens audio devices, operates the desktop, or
uploads church recordings. User listening is not part of this experiment.

## What the fourteen alerts mean

An unchanged original is a **baseline control**, not a human-certified flawless
mix. We can explain which measurements fired, but cannot conclude that every
alert was perceptually false or identify audible causes from the numbers alone.

Eight cases were spectral-only: Carlos Gonzalez, Detsky Sad, Little Chicago's
Finest, M.E.R.C. Music, Signe Jakobsen, Speak Softly, Timboz and Triviul. None
had an out-of-range full-band source ratio. Six of fourteen cases had no
threshold crossing greater than 1 dB; these sets overlap. The old evaluator
applied independent band limits across roles, including weak bass presence or
low-frequency energy in the heterogeneous OTHER stem. Those are not universal
requirements for a balanced feed.

The remaining full-band flags were Motor Tapes, PR (two songs), Punkdisco, Side
Effects Project and Tom McKenzie. PR had notably low vocal and high drum ratios;
we have not established whether that reflects arrangement, original source
balance or performance activity. Raw energy alone does not identify singing.
Widening every threshold would hide some real gain faults as well.

The dataset authors explicitly list Detsky Sad's OTHER audio as mixed into its
drums stem. That limits source-role attribution; it is not evidence of a faulty
drum EQ. The authors also document a PR left-channel mixture/sum mismatch. Our
benchmark rebuilt mixtures from sources, so it did not use that independently
encoded mixture as an exact reconstruction target.

The full case list and crossing magnitudes are preserved in
`_work/source-aware-v2-2026-10-22-01/diagnosis-14.json`.

## Revised evaluator

`audio/balance_calibration.py` separates full-band feed balance from spectral
colour. Band measurements remain available in the existing report, but no
longer independently declare a balance fault. Raw activity/context stays fixed
when comparing candidate contributions, so a muted source cannot disappear
from its own evaluation. Activity still means energy, including bleed, not
confirmed performer activity.

Training uses original source energies only; source IDs, gain labels and raw
**relative** levels are not supplied to the predictor. Candidate ratios are
actual post-mixer role power versus the sum of other role powers. Source power
updates for a constant gain are exact: `P_new = P * 10**(gain_dB/10)`. Direct PCM
mutations independently check that shortcut. Common output gain cancels.

To account for sparse arrangements, training and calibration include all
subsets with at least two supported roles. These simulate absent sources in
the RAW context. A turned-down candidate retains its original context. A
context with no calibration support returns unknown, never clean. Missing
instrument support must remain visible in coverage/miss reporting.

Development compared three compact geometries: robust per-role deviations,
shrunken covariance of centred log source shares, and three-neighbour distance.
For each, one song-level calibration score takes the maximum across roles or
the joint multivariate distance. Thresholds use the finite-sample order statistic
`ceil((n+1)*(1-alpha))`, not an interpolated tail. Nominal split-conformal coverage
requires exchangeability; artist dependence, intentional arrangements and new
datasets can break those assumptions. The reported p-value is not confidence
that a mix sounds bad.

The covariance model uses 20% shrinkage toward a spherical covariance, with a
bounded variance floor. Its role directions describe association with the
outlier, not a confirmed causal source or an automatic fader target.

## Fitting, selection and genuinely new data

The original ninety training songs were split by a deterministic artist hash:
61 fitting songs and 29 calibration songs. The previous fifty test songs are
now **development validation**, because their errors have been examined. They
do not become new held-out results by changing the split.

The development grid covered three geometries and alpha 0.05, 0.10, 0.15, 0.20,
0.25. No model met the initial combined requirement of <=20% unchanged alerts
and no loss in legacy detection/attribution/ranking sensitivity. A separately
documented exploratory selection chose the lowest unchanged-alert rate below
the legacy 28% while preserving those aggregate sensitivity measures:
covariance, alpha 0.25. On the reused fifty songs, alerts fell from 14 to 11,
strict ranking of +/-12 dB controls rose from 87% to 93.75%, any-alert recall
stayed 89.5%, and correct affected-role recall rose from 84.25% to 84.75%.
Identical aggregate recall does not imply identical misses; paired new misses
and recoveries must also be reported on the final set.

Weights, selection, role mappings, excerpt rules and final gates were frozen
before extracting/scoring final audio. A pre-grading protocol amendment fixes
coverage accounting: unsupported legacy cases also count as unknown, not clean;
unchanged-alert fractions use assessed originals. It changes no source selection,
model, threshold or gate. Both protocol versions and the initial failed
development constraint remain saved.

The complete public MUSDB sample had already been used. The public alternatives
reserved for external validation are twenty BabySlakh MIDI-synthesized
instrumental recordings and two real MedleyDB sample recordings (Liz Nelson
and Phoenix). They are not twenty-two new full-band vocal services or a new
human-rated preference dataset. Synthesized choir patches count as OTHER,
not recorded human vocals. BabySlakh lacks singer recordings. Phoenix's
instruments all fall inside OTHER in the four-role model, so instrument balance
inside that bucket is unsupported rather than judged good.

Each recording supplies one 6.5-second window from its first common 24 seconds.
Among fixed candidate starts, selection maximizes the weakest supported role's
normalized energy. Selection never uses evaluator scores or perturbation labels.
Processed MedleyDB stems are aligned with the published mix; nonnegative least
squares recovers its constant source gains, and the reconstruction residual is
reported. That reconstruction step is not preference-model fitting. No clean
reference mix is supplied to the detector at inference.

MedleyDB's full public sample matches its published MD5. BabySlakh metadata uses
full ZIP CRC checks; audio is retrieved through validated byte ranges and bounded
decompressed WAV prefixes. Full stem-entry CRC checks are unavailable for those
partial downloads; byte-range and decoded-excerpt hashes are retained. These
research assets are not redistributed with the app.

## Commands

Inspect saved, aligned raw/post-mixer sources using the existing manifest:

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference inspect-sources path/to/sources.json --model _work/source-aware-v2-2026-10-22-01/sensitivity-model.json --output _work/source-measurements.json
```

The original detailed measurements remain. Joint evaluation explicitly maps
backing vocals into VOCALS and guitar/keys into OTHER; it cannot separately
diagnose their balance. One-role contexts return unknown. Master reconstruction
and timing requirements from the original source manifest remain in force.

Run the frozen saved-file final benchmark in a fresh output directory:

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio mix-preference test-balance-calibration _work/source-aware-v2-2026-10-22-01 --protocol _work/source-aware-v2-2026-10-22-01/final-protocol-02.json --output _work/source-aware-v2-2026-10-22-01/final-run-01
```

Unknown conditions remain visible, including in misses and coverage. Fixed gates
require <=20% unchanged alerts, no worse severe-control detection, attribution
and ranking than legacy, and complete coverage. All source-gain mutations, direct
PCM checks, new misses/recoveries, resource use and failed gates are retained.
No result here alone establishes professional church mix quality. Automatic
edits remain disabled.

## Frozen external result: not promoted

The fresh test completed with 295 mutations across 22 new recordings. Twenty-one
originals were assessable; Phoenix is an unsupported one-role context. Report
that as unknown, not a clean result. The new real vocal example remains flagged,
so the aggregate improvement is largely on synthesized instrumentals.

| Measure | Legacy | Revised |
| --- | ---: | ---: |
| Unchanged originals flagged, assessed only | 21/21 | 2/21 |
| +/-12 dB changes triggering any alert | 117/118 | 85/118 |
| Correct affected-role alerts | 87/118 | 83/118 |
| Strict original-versus-altered ranking | 82/118 | 105/118 |
| Near-removal controls detected and ranked correctly | 59/59 | 59/59 |

The revised detector newly missed 33 controls previously flagged and recovered
one previous miss. New misses were OTHER 13, bass 10, drums 9, vocals 1. The
legacy detector's high recall came with a 100% original-alert rate on this
external domain; it cannot be interpreted as strong useful discrimination.
Ranking improved, but the frozen no-sensitivity-loss/coverage gate failed.
No thresholds or models were changed after examining the final results.

Post-test inspection identified another representation limitation: role ratios
are pooled over role-specific activity times. In Liz Nelson's example, vocals
use 3.25 seconds while OTHER uses 6.5 seconds, so the two role ratios are not
reciprocal (-7.49 and +16.45 dB). Treating those independently pooled numbers as
a coherent simultaneous balance vector can mislead covariance geometry. Common
co-active events and reliable performer/bleed context need separate development
and new validation; no retrospectively corrected score replaces the frozen test.

The Liz Nelson source-gain reconstruction has relative residual power
1.72e-7. Phoenix's residual is 0.0338, so its source reconstruction is not an
exact copy of the published mix. Neither original baseline is human-certified
flawless, and these controls have no listener preference labels.

Evaluation took 28.64 seconds wall/26.98 seconds Python CPU, one logical CPU at
idle priority, one numerical thread, 124.2 MiB peak Python working set, no GPU
runtime, and zero evaluation network/device/DAW/desktop calls. Public-source
preparation is separate from that timing. Fifty-five focused tests passed,
including source timing, leakage checks, common-gain invariance, muted-source
context, unknown-coverage accounting and CLI manifest integration.

The complete report is
`_work/source-aware-v2-2026-10-22-01/assessment.html`; frozen predictions and
every missed control remain in `final-run-01/predictions.json`.

Primary sources:

- [MUSDB annotation errata](https://sigsep.github.io/datasets/musdb.html#errata)
- [Official BabySlakh public sample](https://zenodo.org/records/4603844)
- [Official MedleyDB public sample](https://zenodo.org/records/1438309)
- [MedleyDB audio structure](https://medleydb.weebly.com/description.html)
