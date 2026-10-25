# Combined full-mix evaluation, hum check and selection audit

This workflow retains the original full-mix candidates and every original
control, adds the source-balance veto and its entire fresh-control suite, then
tests a new source-aware hum check. No recorded take, native effect, fader,
transport, sound device or desktop window is controlled by the benchmark.

```
wcfi audio full-mix-benchmark prepare-combined ORIGINAL_BENCHMARK --balance-directory BALANCE_BENCHMARK --output NEW_BENCHMARK --ffmpeg PATH_TO_FFMPEG
wcfi audio full-mix-benchmark run NEW_BENCHMARK --output NEW_RUN
wcfi audio full-mix-benchmark report NEW_BENCHMARK --run-directory NEW_RUN --package APP_PACKAGE
```

Local parents are `_work/full-mix-benchmark-2026-10-22-01/benchmark` and
`_work/source-balance-2026-10-22-01/benchmark-02`. The valid new preparation/run
are `_work/full-mix-evaluator-v3-2026-10-22-01/benchmark-02` and `run-02`.
Policy, core evaluator/renderer fingerprint, opaque inputs and private answer
key are sealed before scoring. Inference never reads the key or renderer gains.

## Hum evaluation

`audio/mains_hum.py` replaces the old mixed-spectrum-only hum veto. The style
weights and costs are unchanged. Every other technical and source-balance veto
remains. Hum cannot be bought back by a closer reference match.

The new check resamples synchronized audio to 2 kHz and examines 25–420 Hz.
Raw-source waveforms with three short lags form a linear prediction basis for
small phase/processing differences. Alternating one-second regions fit a ridge
regression which predicts the held-out regions. In the residual, two-second Hann
windows with one-second hops test 50/60 Hz and their second/third harmonics.
At least two prominent lines, combined energy above the frozen threshold, and
three consecutive flagged windows are required. A short tone is insufficient.

This detects **newly unexplained mains combs**. An already source-explained tone
can be an instrument or upstream hum; identical waveforms cannot be classified
by intent alone. This layer does not certify that an instrument input is clean,
nor remove hum. The temporal support is several seconds, not zero latency.

Development used only the earlier hum/room/hall controls. New challenges include
50.12/59.82 Hz quieter combs, stronger 50 Hz hum, slow 60 Hz drift and a final
six-second hum episode. Negative controls add 49 Hz, 61.735 Hz or exact 60 Hz
content to both the BASS raw input and its mix contribution. The alternate raw
context is supplied as audio, not a good/bad label. It tests whether a source can
explain a tone; it is not evidence that every such tone is musically desirable.

The first preparation omitted the anchor's whole-mix loudness scalar when
constructing those alternate raw BASS controls. Code review caught this before
unblinding/grading. `run-01/excluded-from-validation.json` records its exclusion.
No thresholds were changed. The valid preparation reconstructs the original
unmatched anchor, verifies its scalar fit, and includes that gain in raw context.

## Combined gate and automatic engineering selection

There are eight retained candidates, ten original controls, sixteen balance
controls and eight new hum/negative controls per date: **126 versions plus two
saved Bethel/Maverick references**. All complete listening files are scalar
matched to −30 LUFS below −3 dBTP. This is an audition level, not a broadcast goal.

The **112-check control gate** requires all fault/null/benign tests, clean anchors,
repeats, input-order invariance and agreement between separate reference choices.
Balance faults require a real balance violation; hum faults require the new hum
flag. Technical/style controls retain their original rejection/margin rule.

After blind scoring and unblinding, a separate predeclared selection audit
checks the winning candidate against the saved baseline on each six-second half.
An experimental recommendation requires the full control gate, eligibility,
reference agreement, at least 0.05 full-excerpt cost improvement, at least 0.025
gap to the next eligible candidate, and at least 0.025 improvement with valid
checks in both halves. These margins are engineering thresholds, not calibrated
preference confidence. Failing any requirement withholds that recommendation.

Passing can support an **experimental engineering choice**, not a claim that
the mix sounds professional. These are three existing development recordings and
two different-song references. No same-stem professional target, independent
expert preference labels or unseen-session validation is available. The halves
check temporal consistency within a passage, not independent generalization.
`professional_quality_judgment_validated` remains false and no host apply action
is provided. CPU workers stay on one logical CPU at idle priority; no GPU, audio
language model, cloud upload, new download or real playback is used.

The app report is **Full-mix benchmark · balance + hum + selection**. It exposes
all failures, hum/balance counts, optional listening copies and selection checks.
Earlier reports and their failed gates remain intact. Older frontend bundles
hide the new kind unless they opt into `combined_benchmarks=1`.

## Valid combined run: October 22

**112/112 control checks passed.** Across the three dates all 72 deliberate fault
controls were vetoed or ranked below their clean anchor by the required margin,
including explicit hum vetoes for all 18/18 old/new hum cases, with 0/30 false
alarms on benign/null controls. Nulls, repeats, clean anchors, order invariance
and separate-reference choice agreement passed. The original failed October 11
hum case is now explicitly vetoed rather than relying on style-score separation.

Blind inference took **91.219 seconds wall / 86.11 seconds process CPU** on one
logical CPU. The separate temporal selection audit took about 10.9 seconds.

| Date | Experimental choice | Full cost improvement over saved baseline | First 6 s improvement | Second 6 s improvement | Gap to next eligible |
|---|---|---:|---:|---:|---:|
| October 4 | Shaped mix + more hall | 0.475 | 0.476 | 0.542 | 0.054 |
| October 11 | Shaped mix + more hall | 0.585 | 0.629 | 0.594 | 0.049 |
| October 18 | Shaped mix + short room | 0.251 | 0.130 | 0.162 | 0.033 |

All six half-passage checks passed and both references selected the same choice
per date. Recommendations are now `experimental_engineering_candidate`, with no
host apply action. The accepted result is **automatic selection within this
eight-candidate engineering benchmark**, not validated listener preference or
proof of matching Bethel/Maverick production. The old saved baseline is an
earlier chain; this is not a comparison with every later manually chosen preview.
