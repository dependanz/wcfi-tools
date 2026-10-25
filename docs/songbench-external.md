# SongBench against external human judgments

This benchmark tests a fixed SongBench model against existing human ratings in
MED and ODAQ. It never opens a sound device, accesses Ableton, starts the emulator,
plays audio, captures new recordings or uploads audio. The user does not need to
listen or supply ratings. Downloading public research assets is a separate step;
inference blocks outbound socket connections and only reads local safetensors.

MED tests preferences between mixes of the same recording. ODAQ tests technical
quality under known audio degradations. These tasks are reported separately;
neither is relabeled as a professional church-mix certification.

## Frozen external screening protocol

The initial acquisition is `_work/songbench-external-2026-10-22-01`:

- MED: four human/reference mixes from each of nine Creative Commons songs.
  Choose the mixer-institution cohort with most eligible mixes (at least eight
  raters), with seeded-hash ordering/tie breaks; select four by seeded hash.
  Require eight shared raters per pair before author exclusion. Do not select
  using preference values, best/worst tags or model scores.
- ODAQ: one seeded-hash trial from each of DE, LP, PE, SH, TM and UN; prefer
  distinct source names. Include all eight human-rated conditions, including the
  reference and two anchors. Fetch selected entries using validated HTTP ranges
  and ZIP CRC checking, rather than downloading the entire 1 GB archive.
- Score complete published excerpts, not hand-picked subsegments. Retain source
  levels, because normalization would change the human-rated conditions. Report
  source RMS as a simple baseline; this does not validate level-invariant taste.
- Follow the published loader: soundfile float32 decoding, mono channel average,
  24 kHz soxr HQ resampling. Preserve float32; do not add PCM16 quantization.
- Use only SongBench's Mixing axis for the primary gate. All seven outputs are
  retained, but no other axis can rescue a failed Mixing result.
- Separately report ODAQ processed conditions without reference and low-pass
  anchors, so obvious extremes cannot conceal failures between processing levels.
- Two duplicate inputs receive separate model calls without caching/deduplication.
  All seven axes must repeat within 1e-6. Independent single-item scoring makes
  reversed subtraction algebra, not an additional order-reasoning experiment.

Before inference, `prepare` exclusively creates the blind manifest, policy and
separate answer key, recording model weights, source code, source metadata and
input hashes. The runner never reads the answer key, human ratings or comments.
Reporting refuses incomplete runs or changed protocol, code and predictions.

For each same-content pair, use shared listeners and exclude either mix engineer
in MED. A decisive human preference has at least eight paired raters and a 95%
paired-rater bootstrap interval excluding zero for mean rating difference (10,000
draws). This is a confidence filter without multiple-comparison correction, not a
claim of individually confirmed significance for every generated pair.

Predicted numerical ties earn half credit. Average decisive-pair accuracy within
each song/trial, then average those content groups equally. Bootstrap content
groups for the reported 95% interval; versions, pairs and repeat calls do not
increase the independent recording count. Per-content rank correlations,
human-rating regret of the model's selection and an RMS-ranking baseline are
secondary descriptions. Ratings remain on their native dataset scales.

Prospective screening requirements (engineering choices, not standards): at
least six content groups and twelve decisive pairs per dataset; MED macro
accuracy >=75%, ODAQ >=80%; both lower 95% interval bounds >50%; all repeat checks
pass. Passing only ODAQ cannot establish useful mix preference judgment.
`professional_church_mixing_validated` remains false even if screening passes.

## Resource budget and dependency isolation

The existing listening environment supplies Torch 2.7.1. Additional audio-only
dependencies are isolated in the working directory: MuQ 0.1.0, librosa 0.11.0,
transformers 4.46.3, tokenizers 0.20.3, huggingface-hub 0.36.0, nnAudio 0.3.3,
easydict 1.13, einops 0.8.1 and audioread 3.0.1. Existing Audiobox dependencies are not replaced.
Import MuQ's unmodified audio-only subpackage without its unrelated eager
text-model imports. Instantiate the published architecture directly, with strict
local safetensors state loading; no pickle checkpoints or remote-code loaders.

The runner sets resource limits before numerical imports: one logical CPU,
idle priority, one Torch intra-op/inter-op thread, CPU float32, evaluation mode
and deterministic algorithms. CUDA is hidden and initialization/tensor devices
are checked on every call. RAM monitoring stops the worker above 7 GiB RSS or
below 3 GiB free physical memory. A 120-second load deadline, 180-second prediction
deadline and 90-minute whole-run deadline prevent unbounded execution. Each
prediction is followed by its duration of cooldown, capped at 15 seconds.
Only the benchmark worker is constrained; other apps/settings are untouched.

The released SongBench loader drops stereo side information. Spatial changes
preserving the mono sum are therefore invisible. MED browser audio is MP3, and
the model's foundation pretraining overlap is unknown. No transfer to subtle
EQ/compression, church bleed, or live operation follows automatically from an
external benchmark result. CPU feasibility is separate from judgment quality.

## Reproduce

The working directory retains acquisition scripts, download provenance, selected
original audio and licenses. Source audio and weights are research assets kept
out of Git; original attribution and upstream noncommercial terms remain in
force. The added commands accept already acquired local assets:

```powershell
.\.venv-listen\Scripts\python.exe -m wcfi_tools.cli audio songbench-benchmark prepare _work\songbench-external-2026-10-22-01 --output NEW_BENCHMARK
.\.venv-listen\Scripts\python.exe -m wcfi_tools.cli audio songbench-benchmark run NEW_BENCHMARK --assets-root _work\songbench-external-2026-10-22-01 --output NEW_RUN
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio songbench-benchmark report NEW_BENCHMARK --run-directory NEW_RUN
```

Use fresh directories for every preparation and inference run. Interrupted
evidence is preserved; predictions are never silently retried or overwritten.

## October 22 result

All 86 calls completed: 36 MED mixes from nine songs, 48 ODAQ conditions from
six distinct reference recordings, and two independent duplicate calls. The
complete excerpts total 47.61 minutes. The Mixing axis failed both screening
gates. Accuracy below is averaged equally across content groups with decisive
human preferences, with content-cluster bootstrap 95% intervals:

| Task | Decisive pairs / content groups | Accuracy | 95% interval |
| --- | --- | --- | --- |
| MED mix preference | 30 / 8 songs | 55.0% | 42.9–69.6% |
| ODAQ technical quality, all conditions | 157 / 6 trials | 75.2% | 56.6–93.1% |
| ODAQ processed versions without reference/anchors | 57 / 6 trials | 56.1% | 24.6–85.9% |

One MED song had no decisive human pair and remains visible in the full report.
The simple higher-source-RMS baseline scored 62.7% on MED, versus SongBench's
55.0%; this is a point-estimate comparison, not proof of a significant difference.
Both duplicate calls matched exactly on every axis. Repeatability and CPU
feasibility therefore passed, but useful general mixing judgment did not.

The per-content failures matter: on `PouringRoom`, the model chose `McG-S`, whose
mean human rating was 0.364, over `McG-pro1` at 0.737. On ODAQ's UN violin trial,
it preferred the strongly noise-processed `UN3k` (human mean 15.35/100) to the
reference (100/100); processed-only pair accuracy was 0/10. Conversely, it did
well on the selected spectral-hole and tonality-mismatch trials. This is uneven
task competence, not a dependable automatic full-mix judge.

Inference used 1,901.454 seconds; total wall time with loading and cooldowns was
2,847.485 seconds (47.46 minutes). Median inference was 9.96 seconds, with a
59.55-second maximum for an 80.06-second excerpt. Final measured peak working set
was 3,499.8 MiB (3.42 GiB). All parameters and buffers remained CPU-only, one CPU
core/thread at idle priority; CUDA remained uninitialized, with zero network
connection attempts, audio uploads, playback or DAW operations.

The first independent arithmetic audit caught a grading bug: the author-ID
normalizer handled numeric year suffixes but missed `_unknown` suffixes on PXL
participants. The declared policy excludes either mix author. This was corrected
for `IdLikeToKnow` and `Vermont`, with a regression test. Both original grading
and original frozen source are preserved. The explicit amendment changes only
author pairing, not selection, primary axis, thresholds, model inputs or
predictions. Primary accuracies and the failed verdict were unchanged. No
inference calls were repeated. The corrected result is authoritative:

- `_work/songbench-external-2026-10-22-01/report-author-corrected-01/report.html`
- Same directory: `results.json` and independent `final-audit.json`.
- Original immutable inference: `run-cpu-01/`.
- Amendment ledger: `grading-amendment-01.json`.

To regenerate an amended report, use a fresh output directory:

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio songbench-benchmark report _work\songbench-external-2026-10-22-01\benchmark-01 --run-directory _work\songbench-external-2026-10-22-01\run-cpu-01 --amendment _work\songbench-external-2026-10-22-01\grading-amendment-01.json --output NEW_REPORT
```

Conclusion: do not enable SongBench as the sole autonomous mix selector. These
results do not establish above-chance MED preference judgment, do not establish
reliable discrimination between ODAQ processing levels, and do not validate
professional church mixing. No processing was applied to any session.
