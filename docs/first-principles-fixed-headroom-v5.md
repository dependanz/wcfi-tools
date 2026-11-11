# WCFI v5 — fixed master-trim fault headroom (offline only)

## Motivation

In v4, all four previously nondominated candidate mixes failed an unnormalized
-3.5 dBTP fault guardband even with an idealized, preemptive 3 dB KIT cut.
This **does not** prove that a dedicated limiter is needed. V3's clean dry sums
had integrated loudness around -18 LUFS. A *single* fixed output trim, chosen
from each clean mix before the synthetic fault, may provide sufficient headroom.

## Experiment

For each v3 frontier candidate, compute the clean-recording output trim

```
master_trim_db = -24.0 - clean_integrated_lufs
```

The -24 LUFS number is an **experimental** static test target. It is not an
accepted church sound-level, PA or live-stream specification. If an input mix
already requires a boost or over 24 dB of reduction, the study refuses.

Apply this **identical gain** to each sample of the *same* v4 scenarios:
- uncontrolled KIT +10 dB for 20–50 s,
- clairvoyant 0.5 dB/s controller with max 3 dB KIT cut,
- unrealistic instantaneous max 3 dB KIT cut, and
- one 100 ms +10 dB KIT hit.

Master trim remains frozen across fault scenarios, and is *never* calibrated on
the faulty audio. For a linear scalar master stage, true peak (dBTP) shifts by
exactly the scalar gain in dB up to FFmpeg reporting precision. The study does
not merely assume this: it regenerates 16 complete 120-second dry sums and
sends the trimmed float32 stereo PCM into FFmpeg's EBU R128 **null sink**.
Afterwards, it compares new measured true peaks and integrated loudness against
the fixed-scalar predictions. If disagreement exceeds reporting tolerances, it
fails before writing a report. The source WAVs, DAW, and mixer are not modified.

Results include guardband margins to -3.5 dBTP, first/number of 100 ms
sample-peak exceedance bins, and clean versus fault integrated loudness.
**Sample peak bins are not measurements of true-peak exceedance in each bin.**

## Required inputs

- `trial-03/report.json` and the exact seven-stem audit manifest.
- v3's unmodified `summary.json`.
- v4's unmodified `summary.json`.
- All seven original recorded WAVs still at the original paths.
- FFmpeg executable installed on the church laptop.

V5 checks v4's SHA-256 of v3 and both prior reports' matching source/protocol
hashes. It re-verifies the original recorded sources and all v3 candidate gains.
A preexisting output folder is refused.

## Church-laptop command

```powershell
$repo = 'C:\dev\wcfi-tools'
$work = 'C:\dev\wcfi-tools-kit-audition'
$py = "$repo\.venv\Scripts\python.exe"
$trial = "$repo\_work\gain-coordinator-2026-10-25\trial-03"
$audit = "$repo\_work\gain-coordinator-2026-10-25\input-audit-seven.json"
$v3 = "$repo\_work\mix-candidate-explorer-v3-20261111-063128-f5313d43\summary.json"
$v4 = "$repo\_work\kit-fault-authority-v4-20261111-070439-7f16e14a\summary.json"
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$output = "$repo\_work\kit-fixed-headroom-v5-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$previous = $env:PYTHONPATH
$env:PYTHONPATH = "$work\src"
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
try {
    & $py -m wcfi_tools.audio.mix_fixed_headroom_v5 `
      --report "$trial\report.json" --audit $audit `
      --v3-summary $v3 --v4-summary $v4 --ffmpeg $ffmpeg --output $output
    if ($LASTEXITCODE -ne 0) { throw 'V5 verification failed' }
    Write-Host "REPORT: $output\summary.json"
}
finally { $env:PYTHONPATH = $previous }
```

V5 produces only a `summary.json` of simulation evidence, never output WAVs,
host commands, or musical/automatic gain selections.

## Interpretation

A pass on these 16 fixed disturbance scans means only that **these** recorded
stems and **these** synthetic gain faults remain within this dry-sum guardband
after the measured clean-level fixed trim. It does **not** certify worst-case
future signal peaks, prevent pre-existing source saturation, justify live gain
updates, or account for the actual installed Ableton processing graph, external
sources, PA, room acoustics, dynamic compression or audio mastering.

If all candidate/scenario combinations pass, the measured evidence supports
keeping music-balance control separate from an output gain/headroom stage, with
the future task of adapting to unexpected live inputs through bounded safety
policies and held-out tests. It does not prove that a limiter is unnecessary in
all other contexts.
