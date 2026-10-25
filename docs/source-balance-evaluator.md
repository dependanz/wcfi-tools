# Source-balance veto and fresh-control benchmark

The full-mix selector now has a source-balance veto. Reference spectral/style
similarity cannot compensate for a configured balance violation. This is a
role-aware engineering policy for lead-led worship, not a universal ideal mix.
Instrumental solos, sermons, uncertain source roles and other arrangements need
their own policy and validation.

`audio/source_balance.py` estimates contributions from synchronized raw tracks
and a candidate mix. It receives audio and declared source roles, never renderer
faders, effect settings, candidate labels, expected answers or fitted mix gains.
The normal nonnegative fit is extended to independent left/right fits in 250 ms
windows over 40–250 Hz, 150–650 Hz and 700–3500 Hz. This makes brief acoustic
surges and low-mid dominance visible even when passage averages are benign.

Rules cover acoustic/electric/keys versus lead, bass versus KIT, buried lead/KIT,
and excessive lead/KIT. Evidence is gated on the **raw** reference source's
activity, so burying the performer cannot disable its test. A sustained 0.75 s
excess, or repeated excess with sufficient duration/coverage, triggers a veto.
One short attack is insufficient. The full numeric policy is included in reports.

Source confidence uses the amount of raw-source variation not explainable by
the other tracks. Below 4% unique variation, attribution is indeterminate and
selection abstains. This handles a demonstrated duplicate/strong-bleed case; it
does not establish general source separation or calibrated perceptual confidence.
Band fits still have errors under noise, clipping, effects, timing offsets and
bleed. Their energy ratios are not intelligibility or psychoacoustic masking.

The original style weights, technical/hum rules and cost calculation remain
unchanged. Balance affects eligibility, not the reference cost. Earlier technical
failures remain vetoes even when balance passes. The previous full-mix benchmark
and its failed gate are preserved.

## Reproduce

```
wcfi audio full-mix-benchmark prepare-balance PREVIOUS_BENCHMARK --output NEW_BENCHMARK --ffmpeg PATH_TO_FFMPEG
wcfi audio full-mix-benchmark run NEW_BENCHMARK --output NEW_RUN
wcfi audio full-mix-benchmark report NEW_BENCHMARK --run-directory NEW_RUN --package APP_PACKAGE
```

Use `_work/full-mix-benchmark-2026-10-22-01/benchmark` as the prior local inputs.
Successful artifacts are `_work/source-balance-2026-10-22-01/benchmark-02`,
`run-01`, and `package`. Policy, core evaluator/renderer implementation hashes,
opaque input hashes and a separate private answer key were frozen before scoring.
The scoring runner does not read the key. The old evaluator and new veto evaluate
the exact same files in the paired comparison.

Development used only the earlier room, acoustic +18 dB, buried lead and buried
KIT cases. The fresh test has different perturbations on the same three dates:

* Four acoustic faults: +14 dB, +21 dB, a +18 dB bell at 350 Hz/Q 0.7, and a
  two-second +24 dB surge with 10 ms ramps.
* Electric +18 dB, keys +20 dB, bass +18 dB, RED/KIT each reduced 12 dB,
  RED raised 14 dB and KIT raised 18 dB.
* Three benign changes: acoustic +2 dB, −3 dB, and swapped pan with equal power.
* Identical audio and whole-mix ×0.37 gain controls, loudness matched.

Eight existing candidates per date and the existing Bethel/Maverick references
retain their processing and are scalar rematched alongside all fresh controls.
Every complete listening file is verified at **−30 LUFS**, below −3 dBTP. This
quieter common audition level preserves the extreme controls' peaks without
limiting them; it is not a broadcast loudness prescription. The first preparation
attempt stopped on peak headroom at −24 LUFS before any controls were scored.
The successful preparation, policy freeze and blind run used −30 throughout.

These are fresh synthetic faults on development recordings, not unseen sessions
or independent expert preference judgments. A deliberately altered control's
expected behavior is a test hypothesis. No claim of professional musical quality
follows from detecting those faults. All automatic mix recommendations remain
withheld, even when this source-balance gate passes. The earlier hum and overall
quality gate have not been revalidated by this narrower test.

## October 22 results

**55/55 source-balance checks passed.** Inference took 40.125 wall seconds,
38.36 process CPU seconds, on one logical CPU. No learned audio model, GPU,
network upload, sound device, new capture or Ableton operation was used.

| Session | New faults detected | Old rule rejected | False alarms on benign/null | Indeterminate |
|---|---:|---:|---:|---:|
| October 4 | 11/11 | 10/11 | 0/5 | 0 |
| October 11 | 11/11 | 8/11 | 0/5 | 0 |
| October 18 | 11/11 | 10/11 | 0/5 | 0 |

All **12/12 acoustic fault controls** were detected. All nulls, anchors, repeated
evaluations and order-invariance checks passed. Reference choice agreement is
disclosed, not used as evidence that balance detections are musically correct.
The old rule's rejection count uses its original technical eligibility or 0.05
cost separation, whereas the new rule requires an actual balance violation;
uncertainty alone never counts as successful detection.

The October 11 acoustic +14/+21 dB cases still look better under the old cost
(about 0.633/0.700 versus the anchor's 0.801). The new balance veto rejects both.
This demonstrates the intended precedence without altering style weights to
make those cases score worse. Subtle preference and real-session generalization
remain unvalidated.

The app report is **Source balance · fresh controls across three sessions**.
It includes paired counts, full rule evidence, optional matched listening files,
and no apply action. Existing studies are retained and older frontend bundles
hide the new kind unless they opt into `balance_benchmarks=1`.
