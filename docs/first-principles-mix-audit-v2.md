# WCFI First-Principles Mix Objective Audit v2 (offline only)

This is the next measurement milestone after `mix_objective_audit.py`. It **does not** alter v1, `MixCoordinator`, Ableton, a VST, a live stream, or the mix. All of its results are **descriptive engineering evidence**. No learned listener score or reliable 'correct mix' objective is asserted.

## Provenance and data scope

- Only the seven exact 2026-10-04 source stems saved by `trial-03/report.json` are used: RED, YELLOW, GREEN, EGUITAR, AC GUITAR, KEYS, KIT.
- The report's calibration/evaluation frame ranges and all seven decoded float32 source-window SHA-256 hashes are verified **before** creating an output directory. The optional original `input-audit-seven.json` is also checked for identity and expected SHA-256.
- The 300–330 s calibration and 330–450 s evaluation sections are fixed by the saved trial. The *approximate* bridge under review is evaluation time 50–82 s. No reference to hidden take lanes or excluded January audio is permitted.
- Every recording is opened read-only. There is no playback, rendered WAV, host control, gain application, or networking.
- The eight v1 tests plus eleven v2 tests must pass on the local installation; the controlled synthetic end-to-end case never accesses the church recordings.

## What v2 measures

1. **Section-local KIT attack candidates.** Every 10 ms, the program measures positive spectral flux from two *causal* windows ending at that hop: 80 ms in 40–180 Hz and 20 ms in 800–7000 Hz. It finds local maxima, selects events relative to their own section (not the loudest events across the song), and enforces an 80 ms minimum spacing per band. This distinguishes possible low-frequency impacts from upper-frequency attacks, but does **not** transcribe kick, snare, or musical tempo. An event can be flagged weak-energy rather than discarded automatically. Zero signal produces zero events.
2. **Onset-aligned comparative band powers.** At candidate events, re-read the exact seven stems on the same end-aligned 80 ms / 20 ms windows and compute KIT-versus-all-other *incoherent power* margins for each of the five frozen v1 gain probes. This is **not** a perceptual masking model or an exact mixture-energy decomposition. Report p10/median/p90 per band and section; missing sections remain unavailable.
3. **YELLOW/GREEN source qualification evidence.** Activity, co-occurrence with RED, activity when RED is inactive, and normalized speech-band spectral similarity. Independent singing, microphone bleed, and singer identity are **not** established; automatic boosting these unverified channels is forbidden. Even high/low correlation is only compatibility evidence, not a classification.
4. **Saturation metadata.** Count full-scale samples and contiguous full-scale frame runs over the evaluation passage for every source, including per-channel counts. It identifies possible pre-existing saturation but does not prove a particular recording stage clipped.
5. **True peak and loudness.** With `--ffmpeg`, stream each reconstructed dry stereo probe as float32 PCM over a pipe to a local `ebur128=peak=true` analyzer. FFmpeg's null output creates no audio file. If FFmpeg is omitted, both quantities are explicitly unavailable; no rankings can be called comparable. These are results for the **dry seven-stem model**, not Ableton's actual processing graph.
6. **Descriptive Pareto comparison.** Measure multiple conflicting proxies: RED speech-band p10 margin, worst-section high-frequency onset p10 margin, true-peak headroom, and L1 distance from the frozen static gains. A probe is labelled nondominated only within this *five-probe* set and only when all needed measurements exist; a missing section makes it insufficient-evidence. Nondominance **does not** constitute a listening preference, a production-ready mix, or permission to adjust a gain.

### Execution from the church laptop

Download ZIP from Drive to Downloads and extract into a fresh folder. The installer checks that the original v1 module is the expected frozen version, then copies only the three new v2 files into the existing separate worktree `C:\dev\wcfi-tools-kit-audition`; it does not touch the main checkout. It runs the original eight tests plus the v2 tests.

```powershell
$repo = 'C:\dev\wcfi-tools'
$work = 'C:\dev\wcfi-tools-kit-audition'
$py = "$repo\.venv\Scripts\python.exe"
$trial = "$repo\_work\gain-coordinator-2026-10-25\trial-03"
$audit = "$repo\_work\gain-coordinator-2026-10-25\input-audit-seven.json"
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$output = "$repo\_work\mix-audit-v2-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$prior = $env:PYTHONPATH
$env:PYTHONPATH = "$work\src"
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
try {
  & $py -m wcfi_tools.audio.mix_objective_audit_v2 `
    --report "$trial\report.json" --audit $audit --ffmpeg $ffmpeg --output $output
  if ($LASTEXITCODE -ne 0) { throw 'Offline v2 audit failed; preserve the error' }
  Write-Host "REPORT: $output\summary.json"
} finally { $env:PYTHONPATH = $prior }
```

Only `summary.json` is written, after source checks and measurements finish. Missing data remains `null` or `unavailable`. Nothing in this experiment enables an automatic fader command. A reasonable next milestone is a constrained *offline* Pareto search with calibrated source roles and held-out rehearsal data; it must not treat these physical proxies as ground-truth musical taste.
