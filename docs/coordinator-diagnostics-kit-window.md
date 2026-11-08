# Coordinator diagnostics and the KIT observation-window study

This is an additive, offline experiment. `mix_coordinator.py`, its default
policy, `home-shadow`, the existing CLI command tree, and all native plug-ins
are unchanged. Nothing enables automatic mixing or connects to Ableton.

## Diagnostic step: same gains, better explanations

`DiagnosticMixCoordinator` subclasses the existing coordinator and adds an
explanation after each completed-bin decision. Its inherited PCM processing,
manual controls, gap handling, targets and ramps are unchanged. Original event
fields, including `reasons`, remain intact for compatibility. New consumers
should read `diagnostics.<track>.reason` instead of interpreting the old
`hold_deadband` label as evidence that a mix is balanced.

The added fields distinguish:

- a rejected observation from enough evidence for a balance decision;
- nonpositive drift (no automatic boost permitted) from a small positive drift;
- holding an existing correction within the recovery hysteresis;
- a lead, support, or joint accent, with both signed level changes;
- actual and target correction, activity margins, and retained evidence counts.

Reasons are the first selected condition, not an exhaustive list of all
simultaneously failing gates. Retained history alone does not make the current
observation eligible. Null accent attribution means the previous valid
observation is unavailable; it does not mean that no accent occurred.

Review an existing trial without reading any recordings:

```powershell
python -m wcfi_tools.audio.coordinator_diagnostics `
  _work/gain-coordinator-2026-10-25/trial-03/decisions.json `
  --report _work/gain-coordinator-2026-10-25/trial-03/report.json `
  --output _work/coordinator-diagnostics-new.json
```

The output is a new JSON file containing per-channel reason counts, maximum
evidence, correction ranges, and hashes of the supplied evidence. It never
rewrites historical logs. Use a new output filename on every invocation.

## KIT step: an explicitly measurement-only candidate

This step compares eligibility, not gain trajectories or audible mixes. It does
not replace the coordinator. Integrating a new gain policy is a later step,
after reviewing these measurements and a separate offline control experiment.

The baseline uses the saved 100 ms KIT/RED observations and settings. The
candidate updates every 100 ms using the mean linear power of the last FIVE
complete bins, then converts to dB. Both KIT and RED use the same trailing
window. No dB averaging, future samples, zero-padding, or partial-window
qualification is used.

Only the earlier calibration window determines the candidate's activity
thresholds and KIT-to-RED reference. Static baseline gains remain frozen. The
candidate retains the original immediate 100 ms RED activity and RED accent
vetoes, as well as the paired smoothed-window checks. This is deliberately more
conservative than smoothing away a vocal pause. Gaps reset both the window and
accumulated evidence. No support above-baseline boost is introduced.

Overlapping 500 ms windows are correlated. An evidence counter describes
accepted endpoints in time, NOT independent statistical samples or confidence.

The frozen scenarios are:

- the unmodified development passage;
- a +10 dB KIT power-equivalent level fault during evaluation seconds 20-50;
- one +10 dB KIT 100 ms bin at second 20;
- a RED pause during seconds 20-25;
- one missing observation at second 20.

All disturbances occur AFTER calibration and affect only in-memory powers.
They are engineering controls, not newly recorded performances or musical labels.
A zero or positive result is reported without retuning or automatic promotion.

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
python -m wcfi_tools.audio.kit_observation_trial `
  --report _work/gain-coordinator-2026-10-25/trial-03/report.json `
  --decisions _work/gain-coordinator-2026-10-25/trial-03/decisions.json `
  --output _work/kit-window-study-new
```

The script reads ONLY the exact RED and KIT file paths from `source_provenance`
in that report, over its saved calibration/evaluation frame ranges. It verifies
headers and SHA-256 hashes of the decoded native-channel float32 windows against
the old trial before comparing measurements. There is no file discovery,
resampling, playback, cloud request, host access, or audio export.

Before scoring the candidate, the 100 ms measurement replay must reproduce
KIT's saved first guard, retained evidence count, timestamps, and relative
error (1e-8 dB tolerance). A mismatch stops the study; it is not silently repaired.
If the source paths no longer resolve, relocate/restore the exact verified
sources explicitly rather than scanning unrelated recordings.

A new output directory contains a frozen `protocol.json`, ten JSONL traces,
and `summary.json`. An error can leave the protocol or partial traces but never
creates a success summary. Use a new output directory after inspecting a failure.
`cut_evidence_ready` means only that the current and median drift exceed the
cut threshold with sufficient evidence. It is NOT a gain command, a successful
correction, a peak guarantee, or a perceptual-quality judgment.

## Verification

```powershell
python -m pytest -q -p no:cacheprovider `
  tests/test_coordinator_diagnostics.py tests/test_kit_observation_trial.py
```

The tests compare diagnostic and original PCM sample-for-sample at 8/48 kHz,
mono/stereo and float32/float64, including manual ownership, gaps and invalid
samples. They also cover signed-reason explanations, accent attribution,
causality, power averaging, warmup, a sustained synthetic KIT fault versus an
isolated hit, immediate pause protection, exact source hashes, CLI operation,
baseline-mismatch refusal and output-overwrite refusal.

These tests establish implementation properties, not a better-sounding mix.
The previously examined October 4 material remains development/regression data.
`live_eligible` and `perceptual_improvement_established` remain false.
