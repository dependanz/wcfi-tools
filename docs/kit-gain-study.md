# Offline KIT virtual-gain study

This is a **gain-envelope counterfactual**, not a live audio controller or a
completed mix. It extends the separate 100 ms versus 500 ms KIT observation
study without modifying `MixCoordinator`, any VST3, Ableton, or the `wcfi` CLI.

The original October 4 passage and all its gain-injection variants are
**development/regression data**, not fresh validation or human preferences.
The experiment requires the prior trial's `report.json`, `decisions.json`, and
the same exact RED/KIT WAV files. It verifies the decoded source-window SHA-256
hashes and replays KIT's saved 100 ms decisions before proceeding.

## Causal gain contract

Each completed 100 ms observation can set a *virtual target for the following
bin only*. The current bin moves only toward the target chosen by a prior
observation. The simulated KIT gain is bounded to [-3, 0] dB from its **frozen
static baseline** and moves at most 0.05 dB per 100 ms (0.5 dB/s). Other tracks
are not controlled. An invalid or ineligible observation freezes the gain
already reached; a gap clears measurement evidence, never forcing a reset to
zero. No automatic boost above the fixed baseline exists.

Both candidate and baseline use the same target-selection rule. A qualified
positive KIT/RED error must exceed the 2 dB deadband in both the current window
and the retained rolling median. A target requests no more cut than the lesser
of those two excesses, up to 3 dB. Returning toward zero requires sufficient
qualified evidence below the 1.5 dB recovery threshold. The original 100 ms
baseline must remain zero on the saved passage (the original legacy KIT trace
was zero); otherwise this deliberately scoped comparison refuses the input.

**Important:** This module does *not* compute the complete stereo mix, dry sum
peak, true peak, loudness, or host-side headroom. Its recovery operation is
therefore a **virtual** hypothetical recovery, not a certified safe request to
a real mixer. There is no actual PCM gain application, WAV export, audio output,
network call, or host/DAW operation. An envelope is evidence about a simulation
only; it cannot qualify automatic audio application.

## Run on the original verified recording

```powershell
$repo = 'C:\dev\wcfi-tools'
$py = "$repo\.venv\Scripts\python.exe"
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'

& $py -m wcfi_tools.audio.kit_gain_trial `
  --report "$repo\_work\gain-coordinator-2026-10-25\trial-03\report.json" `
  --decisions "$repo\_work\gain-coordinator-2026-10-25\trial-03\decisions.json" `
  --output "$repo\_work\kit-virtual-gain-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

Use a fresh output directory. The experiment writes a `protocol.json`, a
`summary.json` only after successful evaluation, and **ten** `*-gain.jsonl`
traces (100 ms and 500 ms across five frozen scenarios). Scenarios are the
original, a +10 dB KIT level disturbance from 20 to 50 seconds, one +10 dB
100 ms KIT hit, a five-second RED pause, and one missing input observation.
Changes are in memory in the **power** domain after calibration, never writes
to recorded sources.

The trace exposes gain at the beginning/end of each 100 ms bin, the target
for the next bin, the reason, and the source evidence status. The summary
reports maximum cut, timing of the first proposal/actual gain movement, first
recovery command, final gain, slew checks and nonzero bins inside/outside the
injected-fault interval. A short source does not create a manufactured fault
scenario: control results then fail visibly rather than silently passing.

The virtual gain trajectory is one integration step beyond measurement-only
eligibility. The next independent milestones are a **peak-aware full-mix
renderer**, a blinded listening comparison versus the frozen static mix, and
held-out rehearsal recordings. Until then `live_eligible = false` and
`perceptual_improvement_established = false`.

## Regression tests

```powershell
& $py -m pytest -q -p no:cacheprovider `
  "$repo\tests\test_coordinator_diagnostics.py" `
  "$repo\tests\test_kit_observation_trial.py" `
  "$repo\tests\test_kit_gain_trial.py"
```

Tests cover causal one-bin separation, intervention, bounds, cessation on gaps,
recovery, exact source fingerprints, saved-baseline refusal, immutable output
paths and CLI operation. These are technical tests, not audible validation.
