# WCFI unattended, first-principles mixing audit — checkpoint 1

**Scope:** a reproducible, read-only, offline physical-signal audit. It is **not** an automated mixing policy or a validated predictor of how listeners perceive music. It never touches Ableton, the user's live host, VSTs, or the church's recording files.

The new `mix_objective_audit.py` reads exactly the seven previously authorized October 4 stems from an immutable trial `report.json` (and optionally the linked `input-audit-seven.json`). It rejects any unexpected track, source date, mismatched metadata, decoded audio hash, source audit mismatch, nonfinite samples or altered frame window. No scanning `SampleRefs`, takes, excluded January material, or BASS. The prior report is the source of the frozen static gains and channel activity thresholds.

## What is measured

- **Speech-band competition proxy.** Given source energies \(P_{i,b,t}\) in a 300–4500 Hz time-frequency band, report \(10\log_{10}(P_{RED,b,t}/\sum_{j\ne RED}P_{j,b,t})\). An additional measure excludes the two backing vocal stems from the denominator. It is an **approximate spectral energy ratio**, not STOI, audibility, consonant accuracy, word intelligibility or a validated masking model.
- **Percussion attack prominence proxy.** Identify strong candidate drum onsets from *source-only*, positive multiband spectral flux, restricted to bins passing the previously frozen KIT activity threshold. Use the strongest 10% of eligible positive-flux observations as a **descriptive sampling rule**, not a claim of a psychoacoustic detection threshold or beat identity. At each selected event, report the 80–7000 Hz KIT energy relative to all other source powers. This assesses the presence of potential attacks relative to accompaniment, but not whether a kick or snare is truly audible.
- **Backing-to-lead spectral ratio.** Report YELLOW+GREEN energy relative to RED in the speech band, conditional on RED passing the frozen activity threshold. A high ratio cannot establish that backing singing exists; the stems might contain bleed or background noise. This is an explicit unresolved semantic label.
- **Exact sample-sum peak/RMS.** Reconstruct the seven-stem stereo sum **in memory** for each static diagnostic probe and measure its sample peak and RMS, so phase cancellation is not confused with an incoherent-power approximation. The resulting peak is **not true peak**; the reported optional scalar headroom adjustment is not a live-master safety certification. No WAV files are saved.

The STFT uses 100 ms Hann windows and Parseval-normalized energy per band. Source levels use mean squared power; these proxies are computed deterministically, not fit against labels. The source activity thresholds, however, came from an earlier data-driven calibration, and are **not physical constants**.

The fixed static gain probes are:

| Probe | RED change | KIT change | Meaning |
|---|---:|---:|---|
| `frozen_static` | 0 dB | 0 dB | Original experimental starting balance |
| `lead_minus3db_probe` | −3 dB | 0 dB | Vocal level sensitivity |
| `kit_plus3db_probe` | 0 dB | +3 dB | Drum level sensitivity |
| `both_probe` | −3 dB | +3 dB | Competing vocal/drum sensitivity |
| `unity_dry_probe` | all gains become 0 dB | all gains become 0 dB | Dry unity reference, not a good-mix target |

These are **measurement probes**, not prescriptions. The 3 dB step is an explicit twofold power sensitivity scale; there is no subjective listening-derived target or implicit objective-weight choice. Reported segment windows are 0–20 s, 20–50 s, an approximate 50–82 s bridge region (the precise musical boundary is unannotated), and 82–120 s. The audit analyzes the unmodified original recording: **no +10 dB artificial drum fault is injected.**

## How to install on the church laptop

The installer expects the **existing separate**, uncommitted audio-review worktree from the earlier experiment: `C:\dev\wcfi-tools-kit-audition`, branch `feature/automix-kit-audition-renderer`, at pinned commit `d61c5479be4a720800493260fcaa6b60af655b42`. It copies only the two *new* Python files and this documentation, refusing to overwrite anything. The existing renderer, checkpoint, home-shadow and live integrations remain untouched. The installer then runs the eight new tests.

1. Download this ZIP from Google Drive to your `Downloads` folder.
2. In PowerShell, use a new extraction folder:

```powershell
$zip = "$env:USERPROFILE\Downloads\wcfi_first_principles_mix_audit_v1.zip"
$folder = "$env:USERPROFILE\Downloads\wcfi_first_principles_mix_audit_v1"
if (Test-Path $folder) { throw "Extract folder exists: $folder" }
Expand-Archive -LiteralPath $zip -DestinationPath $folder
& "$folder\install_objective_audit.ps1"
```

3. Run a new **read-only** audit on the original source stems:

```powershell
$repo = 'C:\dev\wcfi-tools'
$work = 'C:\dev\wcfi-tools-kit-audition'
$py = "$repo\.venv\Scripts\python.exe"
$trial = "$repo\_work\gain-coordinator-2026-10-25\trial-03"
$audit = "$repo\_work\gain-coordinator-2026-10-25\input-audit-seven.json"
$output = "$repo\_work\first-principles-mix-audit-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$work\src"
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
try {
    & $py -m wcfi_tools.audio.mix_objective_audit `
        --report "$trial\report.json" `
        --audit $audit `
        --output $output
    if ($LASTEXITCODE -ne 0) { throw 'Offline audit failed; do not reinterpret partial outputs as success' }
    Write-Host "`nObjective report: $output\summary.json"
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
```

## Autonomous development rule

The audit is intended to replace repeated human audition as the **default engineering feedback loop**. An automatic controller must still be tested against objective *counterexamples*: intentionally loud but appropriate drums, quiet bridge, absent lead, background-vocal bleed, sparse percussion, musical rests, intermittent transport, uncorrelated source gains, clipping and out-of-domain recordings. Do not optimize a single RMS error or a single ratio. The next iteration should build a **Pareto/constrained candidate generator** from the multi-objective report, including explicit constraints and reporting uncertainty. True peak and LUFS remain separately verified with FFmpeg/ITU-R BS.1770 for rendered candidates. An absence of human listening labels means we cannot scientifically claim a unique artistically optimal mix.

## End-to-end verification of this package

The authoring environment ran eight focused tests and a synthetic 7-stem × 120-second end-to-end audit that emitted `summary.json`. The test waveforms were synthetic and did not access church recordings. The package has **not** yet run against the church files or the broader existing repository suite. The generated report should be reviewed before expanding the policy; until then the live authorization and gain policies remain unchanged.
