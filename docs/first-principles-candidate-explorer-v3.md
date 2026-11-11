# WCFI unattended mix candidate explorer v3 (offline only)

This module is **not** a professional mix engineer, a calibrated intelligibility
estimator, an automatic controller, or an Ableton plug-in. It is an inspectable,
conservative *static gain experiment* using the previously examined 2026-10-04
seven-stem development recording. It writes only JSON. **No WAV export, host
calls, live gain changes, or network calls.** No gain setting is approved.

## Why v3 exists

v2 recovered bridge transient candidate coverage: 140 low and 170 high
candidates in 50–82 s. Of those, 101 low and 126 high were energy-supported;
weak-energy candidates must not have equal status in an optimizer. v2 also
identified possible KIT input saturation; YELLOW/GREEN were not identified as
independent singers. The five v2 presets were all Pareto-nondominated even
though some had insufficient unnormalized peak headroom. This means a wider
search needs explicit safety gates before comparing signal proxies.

## Fixed research search space

This experiment holds YELLOW, GREEN, electric guitar, acoustic guitar and keys
at the *unchanged* v2 static baseline. It combines:

- RED static gain change: **0, −1.5, or −3 dB**.
- KIT static gain change: **0, +1.5, or +3 dB**.

Total: **nine** static gain combinations. They are not recommended levels.
No automatic backing-channel boost and no modifications to `MixCoordinator`.

The explorer re-verifies all seven exact WAV provenance hashes, the trial
report's audit hash, and the v2 report's source hashes. It regenerates v2's
source-only onset indices and compares the full per-band index lists before
proceeding. It recomputes KIT saturation in the evaluation window.

For each candidate, it computes:

- A lower-tail (p10) RED speech-band energy ratio on the same source-driven
  activity mask, not a validated lyric intelligibility measure.
- Section-local 40–180 Hz and 800–7000 Hz KIT attack-band energy ratios using
  **energy-supported** v2 transients only. The v2 weak-energy candidate set is
  preserved in the prior report rather than silently reclassified.
- Exact dry sample-peak measurements of the reconstructed stereo sum.
- FFmpeg R128 integrated LUFS and true peak of the **unnormalized float dry
  sum** (streamed to FFmpeg, no audio files or host effects).
- The *theoretical* scalar attenuation needed to target −24 LUFS, and the
  corresponding true peak after that scalar; no waveform is rendered.

The hard *exploratory* raw-mix screening gate uses the previously established
−3 dBTP ceiling and an **extra 0.5 dB margin** (≤ −3.5 dBTP). This margin is
an engineering screening choice, not a universal law or a guarantee under a
real Ableton effects chain. A candidate failing the raw gate but passing
hypothetical scalar normalization is flagged separately; it is **not**
misreported as physically safe at the original gain. Loudness measurement
precision, source saturation and nonlinearity of plugins still matter.

Within the *raw-safe* and evidence-complete subset only, the algorithm tests
Pareto dominance on three **descriptive** objectives: RED lower-tail ratio and
worst-section energy-supported KIT low/high attack ratios. Headroom is a hard
gate, not a substitute reward for mix quality. The output is a nondominated
set, not an automatic choice; changing a spectral ratio is not evidence of
improved musical impact or speech recognition.

## Installation and run

Use the included `install_candidate_explorer_v3.ps1` once. It refuses to run if
required v1/v2 code hashes differ, the separate worktree is on a different
branch, or any destination would be overwritten. It preserves untracked v1/v2
work, the live repository and the devbrain. Tests run after file installation.

Run the new tool from PowerShell once the installer tests pass:

```powershell
$repo = 'C:\dev\wcfi-tools'
$work = 'C:\dev\wcfi-tools-kit-audition'
$py = "$repo\.venv\Scripts\python.exe"
$trial = "$repo\_work\gain-coordinator-2026-10-25\trial-03"
$audit = "$repo\_work\gain-coordinator-2026-10-25\input-audit-seven.json"
$v2 = "$repo\_work\mix-audit-v2-20261109-070610-39e517c3\summary.json"
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$output = "$repo\_work\mix-candidate-explorer-v3-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$old = $env:PYTHONPATH
$env:PYTHONPATH = "$work\src"
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
try {
  & $py -m wcfi_tools.audio.mix_candidate_explorer_v3 `
    --report "$trial\report.json" `
    --v2-summary $v2 `
    --audit $audit `
    --ffmpeg $ffmpeg `
    --output $output
  if ($LASTEXITCODE -ne 0) { throw 'V3 explorer failed' }
  Write-Host "REPORT: $output\summary.json"
}
finally { $env:PYTHONPATH = $old }
```

Run with a fresh directory each time. Only `summary.json` is created.

## Interpretation and remaining research

- A candidate can be raw-headroom-infeasible but normalization-feasible.
- A candidate can be Pareto-nondominated and still be a poor mix.
- The 500 ms dynamic KIT controller is **not** enabled or evaluated in this v3
  *static* experiment.
- Source clipping cannot be undone by downstream gain adjustments.
- The best parameter values, intelligibility weights, and genre policy remain
  underdetermined. No self-validating preference metric is claimed.
- Before any live use, use held-out performances, perceptually justified
  objectives, and tests against the real audio processing chain. Until then,
  `live_eligible=false` and `automatic_selection=null`.
