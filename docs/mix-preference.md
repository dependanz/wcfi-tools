# Targeted mix preference and artifact evaluation

The experimental `wcfi audio mix-preference` commands train two small, separate
CPU models using existing human ratings. No new listening judgments are requested.
They read saved audio only: no Ableton bridge, transport, desktop, audio device,
emulator service, cloud API, GPU runtime or audio upload is involved.

## Data and targets

The October 22 study expands the previous metadata-selected MED cohorts to all
eligible human/reference mixes: **73 mixes from nine Creative Commons songs**.
Eligibility uses license, audio availability, institution and rating coverage;
neither preference values nor model outputs select the examples. Source MP3s,
download provenance, licenses and metadata hashes remain under the ignored
`_work/mix-preference-2026-10-22-01` directory. Previously downloaded sources are
reused; additional downloads use a pinned MED repository revision.

Within each song, compare mixes A and B using their shared listeners, excluding
either mix engineer's own ratings, including `_unknown` participant-year suffixes.
Require at least eight paired listeners. For listener rating differences d,
the training target is:

    y(A,B) = mean(1[d > 0] + 0.5 * 1[d == 0])

Uncertain pairs remain soft training targets. A numerical difference is not
treated as unanimous agreement. No artist, song ID, mix engineer ID, institution,
text comment or mean human score enters the audio feature vector.

ODAQ is separate: **48 conditions from six distinct reference contents**, covering
DE, LP, PE, SH, TM and UN. Its paired human quality ratings supervise a small
**full-reference fidelity ranker**. The provided clean target must be aligned with
the candidate. An original, flawed WCFI mix is not automatically a clean target.
This model must not veto an intentional EQ, phase or spatial change merely
because its waveform differs from the original mix.

Public sources: [MED browser repository](https://github.com/rd-conx/med-web),
[MED paper](https://www.dafx.de/paper-archive/2017/papers/DAFx17_paper_49.pdf),
[ODAQ repository](https://github.com/Fraunhofer-IIS/ODAQ).

## Models

MED uses 32 deterministic descriptors: relative spectral band power and its
variation, crest/dynamic range, spectral flux/flatness/centroid and stereo
correlation, side energy and channel balance. Audio is decoded in stereo and
polyphase-resampled to 24 kHz, so frequencies above 12 kHz are not represented.
Overall RMS is removed before learned features. Absolute level and sample peak
remain diagnostics and an independent higher-RMS baseline, not preference inputs.
The method is not a learned source separator and cannot reliably identify which
instrument caused a spectral imbalance from these features alone.

The ranker learns a scalar score s(x) = beta dot z(x), with pair probability:

    P(A preferred to B) = sigmoid(s(A) - s(B))

Training minimizes soft-label logistic cross entropy plus lambda/2 times the
squared coefficient norm. Each training song has equal total weight, independent
of its number of mix pairs. There is no intercept or neural network. This is a
preference ranking function, not a calibrated MOS or probability that a mix
meets a professional standard.

ODAQ uses eight reference-relative errors: log spectra, spectral distributions,
temporal envelopes, waveform, stereo correlation, side energy and broad spectral
shape. Global gain is removed. Error coefficients are constrained nonnegative
and quality is their negative weighted sum: larger errors cannot be rewarded.
ODAQ does not contribute musical preference labels to the MED model.

## Genuine held-out fitting

The outer loop holds out **one complete song/reference content**. All its mix
versions stay together. No audio windows are treated as independent examples.
Within the remaining content, another leave-content-out loop chooses lambda from
the frozen grid 1, 0.1, 0.01, 0.001 by average soft-label log loss. Feature means
and scales use only each fold's training clips. Standardized features are bounded
at +/-8; a constant training feature uses scale 1. No post-hoc feature selection
or hyperparameter rescue uses outer-test performance.

`fold-models.json` retains every outer model, scaler, training/test source ID and
inner tuning result. `models.json` contains separate final all-data fits for future
experiments. **Those final fitted weights do not generate the reported test
predictions.** They retain `experimental: true` and `auto_apply_allowed: false`.

These songs were previously inspected while testing SongBench. Thus this is
content held out from **model fitting**, not a pristine never-seen research test
set. Mix engineers and raters may recur across songs; they are not held out.
There is no independent church-specific validation or WCFI preference label set.

## Frozen assessment

Before feature extraction/training, `prepare` writes an immutable source manifest
and protocol, including code hashes, the full fold plan, feature names, training
policy, regularization grid, metrics and engineering screening gates. `train`
refuses changed code, changed policy or changed audio and exclusively creates a
new run directory. Interrupted evidence is preserved rather than overwritten.

Decisive evaluation pairs have a paired-listener 95% bootstrap interval for mean
rating difference excluding zero (10,000 draws). This confidence filter has no
multiple-comparison correction. Ranker ties receive half credit. Compute pair
accuracy within content, then average content equally. Report 95% content
bootstrap intervals, higher-RMS baseline and paired content-level improvement
over that baseline. These intervals condition on the out-of-fold predictions;
they do not capture the full uncertainty of refitting on a new population.

Engineering screening gates, fixed before running:

- Both tasks: at least six decisive content groups, twelve decisive pairs and
  accuracy's lower 95% content interval strictly above chance (50%).
- MED: accuracy at least 75%; improvement over higher-RMS baseline must have a
  strictly positive lower 95% content interval.
- ODAQ: accuracy at least 80% overall, at least 70% between processed conditions
  alone, and processed-only lower 95% content interval above 50%. Reference and
  LP35/LP70 anchors cannot conceal weak discrimination between processed levels.

Repeat feature extraction, global-gain invariance, stereo sensitivity,
antisymmetric comparisons, author exclusion, monotonic artifact scoring and
held-out feature/label mutation are tested. The actual run repeats independent
MED and ODAQ feature calls and audits disjoint training/test IDs in every fold.
This is a first small-data experiment: passing gates does not establish the
ability to make WCFI sound like Bethel or Maverick City.

## Resource limits and reproduction

The worker sets one logical CPU, idle priority and one numerical thread before
numeric imports. FFT scratch memory is batched. A 30-minute deadline and 2 GiB
working-set ceiling bound the run. CPU affinity and priority are measured, not
just requested. Outbound connections are denied during training. No GPU-capable
runtime is imported; no pretrained weights or remote model code are needed.

Use the existing `soundcheck` dependencies (NumPy, SciPy, soundfile). Commands:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m wcfi_tools.cli audio mix-preference prepare _work\songbench-external-2026-10-22-01 --expanded-root _work\mix-preference-2026-10-22-01 --output NEW_PROTOCOL
.\.venv\Scripts\python.exe -X utf8 -m wcfi_tools.cli audio mix-preference train NEW_PROTOCOL --output NEW_RUN
```

Run directories include `report.html`, `results.json`, `features.json`, all fold
models, final experimental weights and an evidence hash ledger. Reports contain
no audio players and require no user listening. The executable commands fit only
from local files; the separate retained acquisition script downloads public
research audio and never sends church recordings.

## October 22 result

The first frozen run completed in **113.1 seconds** wall time (83.4 CPU seconds),
using one logical CPU at idle priority and **249.3 MiB** peak working set. No GPU
runtime was imported; outbound connection attempts, audio-device operations,
uploads and Ableton operations were all zero. The 32 regression tests passed.

| Task | Clear pairs / held-out contents | Macro accuracy | 95% content interval |
| --- | --- | --- | --- |
| MED preference, nested held-out songs | 157 / 9 | 79.1% | 71.5–87.0% |
| ODAQ artifacts, separate full-reference model | 157 / 6 | 90.0% | 82.6–97.3% |
| ODAQ processed versions alone, same reference model | 57 / 6 | 88.3% | 75.0–100% |

MED's higher-RMS baseline scored 60.5%; the paired content-bootstrap interval for
the ranker's improvement is **+6.5 to +32.5 percentage points**. Baseline RMS is
measured after the same 24 kHz input conversion. Both models passed their frozen
screening gates. An independent audit rehashed all 121 sources, reconstructed
all 15 outer-fold scalers/predictions and checked 436 listener-pair targets and
ranking arithmetic. On actual MED audio, reducing level by 20 dB changed the
final preference score by only about 0.00000011; an identical ODAQ comparison
had zero reference-error score.

Passing the paired-ranking screen does **not** establish a dependable automatic
winner. The highest model-scored mix matched the highest descriptive mean human
rating on only one of nine MED songs. Six selected mixes had at least one
alternative decisively preferred by shared listeners. Descriptive means can use
different listener subsets, so the latter paired-listener finding is the more
useful selection warning. For example, the selected `PouringRoom/McG-V` lost
decisively to two alternatives despite the overall 79.1% pair-ranking result.

A **separate, secondary cross-dataset stress test** froze the trained MED model
and scored all ODAQ clips using only its 32 no-reference descriptors. No fitting,
tuning or feature changes used ODAQ, and no clean-reference error features entered
this test. Results: **64.8%** overall (95% interval 47.2–83.8%) and **46.0%** between
processed versions alone (18.8–75.0%). This is not a pass. It does not change the
original primary gates; it establishes a deployment limitation that those gates
alone cannot resolve. The successful reference-based artifact head cannot
substitute for a reliable artifact guard when no clean target exists at church.

The useful outcome is a fast, learned MED ranking baseline with stronger
same-recording preference evidence than the prior general-purpose model. It
remains **experimental, with automatic application disabled**. The next data need
is independent, labeled church-mix candidates and artifact checks that work
without a known clean reference; repeatedly tuning to these nine songs would
inflate apparent progress.

Inspect the complete evidence without listening:

- `_work/mix-preference-2026-10-22-01/assessment.html`: combined assessment and
  selection caveats, including the secondary stress test.
- `protocol-01/protocol.json`: original frozen plan and code/source hashes.
- `run-01/report.html`, `results.json`, `fold-models.json`, `models.json`,
  `features.json`, `evidence.json` and `audit.json`: immutable primary run.
- `cross-dataset-stress-01/protocol.json` and `results.json`: frozen secondary
  test with no tuning and no effect on the primary fit or gate.

The follow-up [selection-aware v2 study](mix-preference-v2.md) preserves these
results and tests changes with reference-free ODAQ assessment and three separate
final MED songs.
