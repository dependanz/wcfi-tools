# Offline KIT fault audition renderer (development-only)

This module reconstructs **three dry, seven-source stereo comparison WAVs**
from the saved October 4 development trial, using the previously generated
100 ms and 500 ms virtual KIT gain traces. It cannot connect to Ableton,
change source WAVs, operate a fader, or qualify live mixing. It uses **only**
RED, YELLOW, GREEN, EGUITAR, AC GUITAR, KEYS and KIT, with the earlier frozen
static gains. LAPEL, MDMIC Talkback and BASS are excluded. Returns, master Glue,
installed legacy automix effects and unselected channels are *not simulated*.

The three auditions receive the **same injected +10 dB KIT fault over 20–50 s**:

- `audition-static.wav`: frozen static starting mix (no dynamic KIT correction).
- `audition-legacy-100ms.wav`: original 100 ms KIT controller trace, proven to
  remain zero on this passage. Must be *sample-identical* to static before
  loudness matching and byte-identical after the same scalar master gain.
- `audition-candidate-500ms.wav`: the unmodified, previously recorded 500 ms
  virtual KIT gain envelope, applied as a linear-in-dB ramp inside each 100 ms
  bin. All other source gains are unchanged.

Every output is a new PCM24 stereo WAV. Unnormalized float dry sums are only
staged in a temporary directory. The renderer first scans them with FFmpeg
EBU R128 (integrated LUFS and **true peak**); then chooses one target LUFS that
all three can attain without exceeding -3 dBTP. It saves no compressor or
limiter output. It scans each final WAV again and rejects an out-of-budget file.
The summary includes the full-track R128 measurements, dry-sum RMS/sample peak
for bounded temporal segments, and separate R128 scans of the fault, prolonged
recovery hold, later recovery and after-recovery segments. Segment R128 values
may be unavailable for near-silence; in that case the summary records the
missing value, not a fabricated measurement.

This is an **offline audio engineering comparison**, not a validated music
preference, live-eligible policy, or exact Ableton master recreation. Only
seven dry stems are reconstructed. The +10 dB fault is deliberately synthetic.
Neither the static balance nor the recovered correction has been accepted by
listeners. The recording is previously examined development data. The frozen input report
already identifies full-scale KIT samples; rendering below a true-peak ceiling
cannot repair clipping present in the recorded source.

## Run on the church laptop

First ensure a clean worktree, fetch the review branch, and run the new tests.
The renderer uses the exact source paths captured in the saved trial report.
The virtual-gain directory must contain its original `summary.json` and both
`100ms-kit_step_plus10-gain.jsonl` and `500ms-kit_step_plus10-gain.jsonl`.

```powershell
$repo = 'C:\dev\wcfi-tools'
$py = "$repo\.venv\Scripts\python.exe"
$trial = "$repo\_work\gain-coordinator-2026-10-25\trial-03"
$audit = "$repo\_work\gain-coordinator-2026-10-25\input-audit-seven.json"
$gainRun = "$repo\_work\kit-virtual-gain-20261108-062051-1b6331a3"
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'

& $py -m pytest -q -p no:cacheprovider tests/test_kit_offline_renderer.py
if ($LASTEXITCODE -ne 0) { throw 'Renderer tests failed' }

$output = "$repo\_work\kit-auditions-$(Get-Date -Format yyyyMMdd-HHmmss)"
& $py -m wcfi_tools.audio.kit_offline_renderer `
    --report "$trial\report.json" `
    --audit $audit `
    --decisions "$trial\decisions.json" `
    --gain-run $gainRun `
    --ffmpeg $ffmpeg `
    --output $output
if ($LASTEXITCODE -ne 0) { throw 'Offline render refused' }
Write-Host "Audition summary: $output\summary.json"
```

The preflight checks the audit/report/decisions digests, exact seven recorded
source paths and **decoded sample hashes** across calibration and evaluation,
source sample rates and frame ranges, the frozen 100 ms replay qualification,
scenario checks, candidate gain bounds and one-bin causality. It refuses to
create a new output directory when these inputs do not match. Inputs are
opened read-only; sizes and modification times are checked again afterward.
The newly created output path must not already exist. A failure can leave a
partial output directory, but no successful `summary.json` is written.

## Listening and interpretation

Compare the **static** and **candidate** matched audition files blind, at the
same listening level. The original 100 ms file is an intentionally redundant
control. Focus on 20–25 s (fault onset), 40–50 s (sustained fault), 53–75 s
(stale −2.46 dB KIT hold), and 75–82 s (late recovery). The renderer reports
full-track and segment LUFS / true peak, but objective measurements alone do
not establish that the changed mix is musically preferable.

Do not assume that attenuation is peak-safe due to summing/cancellation.
Only the **final reconstructed audition's measured true peak** is checked;
this does not certify a future live host processing graph. After this
experiment, independently held-out recordings and human preference judgments
are required before changing the policy, much less authorizing live writes.
