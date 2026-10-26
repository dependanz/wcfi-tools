# Offline phrase gain candidate

The first phrase-level candidate improves the recorded LAPEL level-error metric,
but **does not pass its frozen development usefulness criterion**. It remains an
offline Python policy. The installed Control Node v0.2 still uses the earlier
native policy and remains Auto Off / Trim 0 dB.

## Behavior

The policy reads completed, non-overlapping 100 ms raw RMS bins. It uses the median
of active bins in the preceding two seconds, requires at least five active bins,
and adjusts only when the current bin also supports the proposed direction of
change. This can hold a boost during a loud accent or veto a stale cut after the
voice becomes quieter; it does not recognize musical intent or semantic speech.

The recording-specific settings are fixed Trim +18 dB, target −26 dBFS, raw
activity threshold −55 dBFS, automatic offset ±6 dB, combined gain ±24 dB,
1 dB/s symmetric correction and a 1 dB deadband. These are experimental settings
for this post-fader export, not recommended live values.

Inactive observations hold the offset. One second of consecutive inactivity
clears the estimator, requiring fresh support on resumption. Missing, repeated,
out-of-order or nonfinite observations discard estimation evidence and hold gain.
They never replay pending changes. A completed bin can affect only subsequent
audio; partial final bins do not fabricate evidence.

`phrase_automix.py` uses the existing workspace native DLL for smoothing and peak
protection. It sets native automatic boost/cut limits to zero and sends the
external policy's combined gain as Trim, retaining the existing 20 ms smoothing
and linked sample-peak guard. It does not modify or load the installed VST3.
External policy offset and native applied gain are logged separately; native
`auto_gain_db` is deliberately zero in this adapter.

## One frozen recorded evaluation

The 179.904-second October 25 worship-to-sermon export was already examined.
The first 120 seconds are development material. The remainder is explicitly
diagnostic reuse, not fresh validation. Only one candidate was rendered, without
retuning after seeing these results.

| Median absolute active-window error from target | Fixed +18 dB | Previous native Auto | Phrase candidate |
| --- | ---: | ---: | ---: |
| Development, 0–120 s | 5.08 dB | 6.53 dB | 3.95 dB |
| Exposed diagnostic, 120–179.904 s | 7.41 dB | 8.38 dB | 3.43 dB |

Development improvement is 1.13 dB, below the frozen 2 dB requirement. The 3.97 dB
diagnostic improvement cannot override that failure or establish generalization.
The candidate ends at +5 dB automatic offset. Some improvement may come from
learning a better static gain; the comparison does not establish superiority to
a well-calibrated static baseline.

A subsequent, explicitly post-hoc check fitted a constant +5.06 dB offset from
development windows only. Its calculated diagnostic-tail error was 3.44 dB,
almost identical to the candidate's 3.43 dB. Development error was 3.40 dB for
that in-sample fit versus 3.95 dB for the causal candidate; this is not a fair
startup test because the static fit uses the entire development excerpt. The
check shifts saved RMS measurements and is not another audio render. It supports
testing whether adaptation adds value beyond simply finding the right initial
gain. Details are in `render-01/posthoc-static-comparison.json`.

All technical gates passed: exact frames, finite samples, bounded gain and slew,
inactive-bin holds, no learned gain during the initial below-threshold minute, no
added full-scale samples in LAPEL or the reconstructed mix, and unchanged input,
source and native-DLL hashes. Candidate LAPEL sample peak was −6.59 dBFS; the mix
peak remained −1.98 dBFS. The guard was tested synthetically but was not needed
in this recording. Existing effect returns were retained, so reconstructed audio
does not simulate changed send levels.

Held positive gain raises pause background: median inactive-window level rose
3.90 dB over fixed Trim. Holding prevents further gain movement, not noise
amplification. Sustained noise above the activity threshold can qualify. No
listening or intelligibility judgment has been made, and sample-peak protection
does not establish true-peak limiting.

## Verification and evidence

59 focused tests passed, including independently derived scalar cases, future
prefix independence, arbitrary block partitions, partial final bins and an
explicit test of the real native DLL's smoothing/guard continuity. No host,
playback device, emulator or January recording was accessed.

Implementation: `src/wcfi_tools/audio/phrase_gain.py` and `phrase_automix.py`.
The existing `NativeControlDsp` wrapper now exposes validated `configure()` at
process boundaries without resetting the native state.

Evidence is under `_work/phrase-gain-2026-10-25/`: `config-frozen.json`,
`test-results-01.txt`, `prepared-02/freeze.json`, and `render-01/report.json`.
`prepared-01` is retained; revision 2 of the freeze adds provenance and runner
checks without changing the policy or executing an extra candidate.

The render folder contains `decisions-100ms.jsonl`, `windows-100ms.json`,
`lapel-phrase-candidate.wav` and `mix-phrase-candidate.wav`. The original fixed
and failed native-Auto renders remain in the previous experiment folder. The
three original input stems match its old receipt; the fixed-gain rendered WAV
was separately hashed into this new freeze.

In the trace, `native_after_observed_bin` contains a status read after the next
configuration was submitted: `target_gain_db` is already the new next-sample
target, while measured levels and `applied_gain_db` describe rendered samples.
The external decision's before/after offset fields make that timing explicit.

The next review should address causal startup acquisition and background behavior,
use the calibrated static comparator, and arrange fresh speech evidence.
Any native policy integration requires parity testing before deployment. This
candidate does not authorize enabling Auto or changing the installed plug-in.

## Activity-based fixed-reference calibration

`activity_calibration.py` now computes a causal, one-time gain suggestion from
completed 100 ms input observations. It waits for 50 energy-active bins within
the most recent 300 bins, freezes their median, and bounds target-minus-median
gain to +/-6 dB. Quiet time cannot satisfy the quorum. Invalid or discontinuous
observations clear unfinished evidence; a completed result remains immutable.
The result is available only for following samples. This is an offline scalar
component, not a change to the installed Control Node or the phrase policy.
41 focused tests pass, including delayed activity, rolling expiry, pauses,
invalid sequences, exact quorum boundaries and future-independent results.

The earlier two-minute RED trial had no active input during its fixed 30-second
calibration period and was inconclusive. A new three-minute passive RED capture
uses the activity-based rule, with the existing Utility +5 dB already in its
post-effects input. Protocol and candidate settings were frozen before capture;
the new calibration implementation and evaluator were reviewed and frozen before
any captured PCM was examined. No January material was accessed.

Calibration reached 50 active bins at 16.0 seconds. Its median was -39.71 dBFS,
so the fixed reference selected the additional +6 dB ceiling. The same native
gain stage rendered unity through the deciding bin and applied that fixed gain
only afterward. The predefined comparison covers seconds 26-116, containing
22.2 seconds of energy-active input and 67.8 seconds below the activity threshold.

| Median absolute active-window error from the -26 dBFS target | Result |
| --- | ---: |
| Existing RED signal | 15.84 dB |
| Calibrated fixed gain | 10.17 dB |
| Unchanged phrase controller | 11.09 dB |

All technical checks passed, including capture continuity, causal calibration,
gain bounds, inactive holds, exact frames, finite output and provenance hashes.
Neither render clipped: fixed-reference sample peak was -2.81 dBFS and the
phrase candidate peaked at -3.91 dBFS. The continuous candidate was 0.92 dB worse
than fixed gain on the frozen error metric, so it failed the required 1 dB
improvement over that reference. Its pause levels were typically 1 dB below the
fixed reference because its correction sat near +5 dB; both amplified background
relative to the original signal. No listening or intelligibility claim is made.

The new calibration mechanism worked on this recording. Continuous adjustment
did not improve on it. Calibration reached its gain ceiling, so this result does
not establish that the target can be reached using gain alone. It also does not
qualify audible startup: the offline fixed reference uses the existing 20 ms
native smoothing and is not a deployed startup controller. Ableton's complete
inspected state remained unchanged, with transport stopped.

Evidence: `_work/red-activity-calibration-2026-10-25/`, including the prospective
`trial-frozen.json`, `capture-freeze.json`, `source-freeze.json`, capture receipt,
`render-01/report.json`, complete calibration/decision traces and WAV renders.
