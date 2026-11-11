# WCFI v4 — offline KIT fault-authority stress audit

## Question, scope, and negative conclusion boundaries

V3 measured nine static-gain candidates on the existing October 4 2026
seven-stem development passage and returned a physically screened Pareto
frontier without choosing a winner. V4 tests a different proposition:
**Does the existing KIT cut authority (at most -3 dB relative to its
starting gain, 0.5 dB/s) protect the reconstructed dry mix against an
artificial +10 dB KIT input-level fault?**

This is a deliberately artificial, **non-live** stress test, not a recording
of a real gain mishap, a source-quality repair, or a perceptual judgment.
The already-saturated KIT source remains unmodified. There is no DAW/plugin
interface or audio output; this reads only the verified seven stems and pipes
hypothetical 32-bit float stereo PCM into FFmpeg's R128 null sink. Only the
final JSON summary is saved. No live gains are applied or proposed.

There is no theorem that a -3 dB uniform cut is the best possible policy under
arbitrary interference and phase cancellation. The "clairvoyant" scenario
already knows the fault's start/end and is favorable to the controller; it is
**not** an actually implementable detector. Failure in the experiment is
**failure of the tested policy under the tested fault**, not proof of universal
infeasibility.

## Frozen input contracts

Inputs are the existing `trial-03/report.json`, `input-audit-seven.json`, and
the exact v3 `summary.json` that already validated source hashes. V4 rechecks
all seven decoded source hashes and source metadata, the trial / manifest hashes,
and the exact nine-candidate v3 gain grid. It considers only v3's four
raw-safe, Pareto-nondominated candidates. It refuses any altered v3 gains,
missing or mismatched source data, pre-existing output folder, or unexpected
live eligibility flag.

## Counterfactuals

For each frontier candidate, v4 measures four fault variants on the complete
120-second evaluation window (no sample values are clipped before FFmpeg):

- `uncontrolled_10db_step`: KIT +10 dB from 20 to 50 seconds; no correction.
- `clairvoyant_slew_limited_maxcut`: same input fault, with a correction
  ramping from 0 to -3 dB at 0.5 dB/s starting exactly at 20 seconds, held to
  50 seconds, then released at 0.5 dB/s. This knows the disturbance timing.
- `instant_maxcut_noncausal_upper_authority`: KIT corrected by -3 dB
  immediately from 20 to 50 seconds. This **violates the 0.5 dB/s constraint**
  and is for comparison only, never a controller.
- `isolated_100ms_hit_uncontrolled`: one +10 dB KIT hit at 20.0–20.1 seconds;
  no gain change. No false "catch" is claimed.

All variants keep other five track gains unchanged and honor the initial v3
RED/KIT candidate gains. V4 compares unnormalized true peak against -3.5 dBTP
(-3.0 ceiling with 0.5 guardband); it also counts 100 ms bins exceeding the
same numeric value **in sample peak**, which is *not* itself a true-peak test.
Measures global integrated LUFS, true peak, largest overall, fault-window, and
bridge-window sample peaks, and the first sample-peak exceedance. It does **not**
perform loudness normalization, limiting, compressor emulation, or perceptual
quality assessment. There is no automatic mix selection.

## Run on the church laptop

The ZIP installer is additive to the existing separate worktree
`C:\dev\wcfi-tools-kit-audition`. It refuses an unexpected branch, missing
v3 source file, or existing v4 destination files. No git commit or push.

```powershell
$repo = 'C:\dev\wcfi-tools'
$work = 'C:\dev\wcfi-tools-kit-audition'
$py = "$repo\.venv\Scripts\python.exe"
$trial = "$repo\_work\gain-coordinator-2026-10-25\trial-03"
$audit = "$repo\_work\gain-coordinator-2026-10-25\input-audit-seven.json"
$v3 = "$repo\_work\mix-candidate-explorer-v3-20261111-063128-f5313d43\summary.json"
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$output = "$repo\_work\kit-fault-authority-v4-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$previous = $env:PYTHONPATH
$env:PYTHONPATH = "$work\src"
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
try {
    & $py -m wcfi_tools.audio.mix_fault_authority_v4 `
       --report "$trial\report.json" --audit $audit `
       --v3-summary $v3 --ffmpeg $ffmpeg --output $output
    if ($LASTEXITCODE -ne 0) { throw 'V4 fault-authority audit failed' }
    Write-Host "REPORT: $output\summary.json"
} finally { $env:PYTHONPATH = $previous }
```

The tool performs 16 full-length FFmpeg R128 scans (four frontier candidate
gain configurations times four fault variants). It may be more computationally
expensive than the earlier nine-candidate v3 audit; it does not run in the
background. Only upload the resulting `summary.json` for analysis.

## Interpretation

A sustained +10 dB KIT input excursion combined with a max -3 dB KIT
correction leaves **at least +7 dB extra KIT path gain** while that fault is
active, relative to the candidate's prior KIT level. This is an algebraic
statement about KIT's gain, *not* a bound on the complete mix true peak, which
also depends on phase and simultaneous source content.

If the tested preemptive max-cut scenario still exceeds the true-peak guardband,
then that particular authority strategy cannot certify headroom for that
recorded passage and injected fault. We should not increase automation authority
or attenuate the live bus without separate evidence about source dynamics and
system-wide master protection. The next phase needs held-out recordings and a
separate safety path, not a claim of musical optimality from proxy metrics.
