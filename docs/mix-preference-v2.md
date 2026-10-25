# Selection-aware preference evaluation and reference-free artifact checks

This follow-up preserves the first experiment and studies its six selection
failures. It uses saved public research excerpts and existing listener ratings.
The user supplies no new listening judgments. Commands do not open an audio
device, contact Ableton, control the desktop, play audio or upload recordings.

## What went wrong in v1

There were eight decisive losing comparisons across the six selected songs.
They also disagreed with listener majorities, so the problem was not simply the
difference between majority-preference training and mean-rating evaluation.
Exact decompositions retain every feature's contribution to the wrong score:

- `InTheMeantime`: the selected `McG-pro2` gained points for upper-band energy,
  its variation and stereo-correlation variation. Its flatness was over six
  training standard deviations from the training mean. The scoring formula
  treated those descriptors as useful proxies despite clear listener dissent.
- `LeadMe`: crest factor and centroid/flatness variation helped `McG-pro2`
  outrank two alternatives preferred by paired listeners.
- `NewSkin`: mid-band spectral shape and variation gave `DU-O` an advantage over
  the preferred `DU-S`.
- `PouringRoom`: low-mid power, spectral flux, upper-band energy and stereo
  variation helped `McG-V` outrank two preferred alternatives.
- `RedToBlue`: low-band power and upper-band variation contributed to `McG-C`
  outranking `McG-E`.
- `UnderACoveredSky`: bass power and centroid-related terms gave `McG-pro2` an
  advantage over the preferred `McG-P`.

These explain the model's arithmetic, not the audible faults. Correlated
descriptors are not independent causes, and a coefficient is not an EQ target.
The waveform statistics do not establish whether vocals, guitar or ambience
caused a preference. The original model was linear and tuned average pair log
loss, which did not directly penalize picking a candidate with a better rival.

## Revised preference training

The frozen grid contains the original 32 descriptors with linear scoring, an
extended reference-free feature set with linear scoring, and a diagonal concave
quadratic score. Quadratic coefficients are constrained nonpositive; training
can learn an intermediate range rather than always rewarding a larger value.
All scalers use only the training contents, and standardized inputs are bounded
at +/-4. Regularization choices are 1, 0.1 and 0.01.

Paired soft listener targets remain, with author exclusion. Within training
songs, stronger agreement and pairs involving more competitive candidates get
more weight. Every song still has equal total weight. Hyperparameters are
selected by **maximum paired human regret of the selected winner** on inner
held-out songs; average pair log loss breaks ties. Outer held-out songs never
contribute to fitting, scaling, these weights or hyperparameter selection.

Forced winner failures and regret remain visible even when the system abstains.
For the separate final songs, a recommendation additionally requires unanimous
winner agreement among leave-training-song-out models, supported feature range,
and an artifact quality prediction of at least 60/100. This is a caution rule,
not a calibrated statistical confidence interval. Automatic application remains
disabled for every model.

## Artifact assessment without a clean reference

The 70-descriptor artifact head accepts only the candidate waveform. Neither a reference
waveform nor source identity, degradation type, condition name or human rating
enters the feature vector. Native-bandwidth measurements add:

- Bandwise flatness, rolloff, spectral gaps, isolated narrow peaks and their
  temporal instability.
- Short-envelope rise and onset pre/post energy.
- Relative flat-top runs and 50/60 Hz harmonic concentration versus neighboring
  frequencies. Music can imitate mains concentration; this is not a confirmed
  hum diagnosis by itself.

The head learns sigmoid quality regression from ODAQ mean human scores divided
by 100, with equal content weighting and nested content-held-out selection.
Its output estimates ODAQ perceived fidelity, not musical preference or artifact
type. Report both paired ranking (especially processed versions without anchors)
and observed poor-audio recall/clean-audio false alarms at a frozen 60/100
threshold. Clean evaluation conditions have human means >=85; poor conditions
have means <=60. Calibration statistics are descriptive clip-level summaries.
Separate fixed signal alerts flag harmonic concentration >100 times neighboring
spectral density, or relative flat-top runs above 0.1% of samples. These are
suspicious-signal alerts, not confirmed hum/clipping diagnoses. Their false alarms
on the clean ODAQ conditions are measured, and they can cause recommendation
abstention without changing the forced-selection result.

All **240 conditions from the 30 original ODAQ trials** are included. Repeated
music across processing methods, remixes from the same film, the female speech
collection and the two violin excerpts are conservatively grouped, giving 16
content groups. Test pairs stay within their original rated trial; trials sharing
content are averaged before averaging contents. The provided references appear
as ordinary rated training/test examples, never as privileged inputs.

Public source and download provenance:
[ODAQ](https://github.com/Fraunhofer-IIS/ODAQ),
[original archive](https://zenodo.org/records/10405774),
[MED](https://github.com/rd-conx/med-web).

## Development and prospective song tests

The original 73 mixes/nine songs remain the development set. Those songs were
inspected in v1; revised cross-validation is **exploratory development**, even
though each fit excludes its outer test song.

Three previously unused Creative Commons MED songs, `Lolita`, `Lush`, `OldTree`,
provide 15 prospective final-test mixes. They never enter fitting, scaling or
tuning. A separate answer key is opened only after final model weights and both
v1/v2 predictions are saved and hashed. The model is not retrained in response to
those results. These songs only have five original raters; excluding both mix
authors can leave three paired listeners. The final test therefore allows three
and reports its limited power explicitly, rather than presenting it as equivalent
to the development test's minimum eight.

Metrics retain paired-listener bootstrap intervals, decisive-pair accuracy,
selection failures and maximum paired regret. Contents, not generated pairs or
audio windows, are the independent reporting units. Bootstrap intervals condition
on the fitted predictions. A frozen engineering screen requires development MED
accuracy >=75%, no more than three songs with a decisively better alternative,
ODAQ no-reference accuracy >=80%, processed-only accuracy >=70% and processed
lower 95% interval >50%. This is a screening policy, not an industry standard.

One stable-hash-chosen mix per fresh song also receives synthetic sanity checks:
identity, gain-only, driven clipping, hum, white noise and strong low-pass damage.
Damage comparisons are RMS matched. No synthetic examples are used for training;
their assumed degradation labels do not substitute for expert preference ratings.

## Reproduce and inspect

```powershell
.\.venv\Scripts\python.exe -X utf8 -m wcfi_tools.cli audio mix-preference prepare-v2 _work\mix-preference-2026-10-22-01 --assets _work\songbench-external-2026-10-22-01 --acquired _work\mix-preference-v2-2026-10-22-01 --output NEW_PROTOCOL
.\.venv\Scripts\python.exe -X utf8 -m wcfi_tools.cli audio mix-preference train-v2 NEW_PROTOCOL --previous _work\mix-preference-2026-10-22-01 --output NEW_RUN
```

The worker enforces one CPU, idle priority, numerical threads 1, no GPU runtime,
2 GiB working-set ceiling and a 40-minute deadline. Network connections are denied
during feature extraction/training; public downloads are separate. Report files
have no players or executable scripts. Frozen source hashes, every outer model,
inner tuning, fresh predictions before grading, failure attributions and synthetic
controls are retained. Final experimental weights explicitly disable automatic
application.

## Result: partial improvements, failed general reliability

The frozen primary run completed on one CPU in **319.5 wall seconds / 287.0 CPU
seconds**, with **796 MiB** peak working set. The complete targeted regression
suite now passes 48 tests. Independent auditing rehashed all 328 inputs and
reconstructed 25 outer-fold scalers/predictions and 1,168 listener-pair targets,
bootstrap intervals and ranking credits. GPU/runtime, playback, desktop/DAW
operations, uploads and inference network attempts were zero.

| Test | v1 | v2 |
| --- | --- | --- |
| Development selections with a decisively better alternative | 6/9 | 4/9 |
| Development clear-pair accuracy | 79.1% | 73.6% |
| Three fresh-song selections with a decisively better alternative | 3/3 | 2/3 |
| Fresh-song clear-pair accuracy | 47.6% | 39.3% |
| No-reference ODAQ accuracy, all 240 conditions | 62.9% | 74.6% |
| No-reference ODAQ processed-only accuracy | 47.2% | 59.3% |

The ODAQ comparison uses the frozen original MED preference model as the v1
baseline, versus the separately supervised reference-free quality head in v2.
It is not a comparison with the earlier full-reference artifact model.

New-song mean maximum paired regret fell from 0.266 to 0.182, but their v2
accuracy interval is 0–75% and two selected mixes still lose decisively. All three
recommendations abstained; forced choices were still scored. This is not proof
of reliable generalization or a statistically established improvement. The final
fits selected extended linear scoring; the concave option did not win final
inner selection.

The broad ODAQ quality estimate produced **23 false alarms among 46 human-rated
clean conditions** at the frozen 60/100 threshold. Added white noise raised both
preference and quality scores on two of the three initial synthetic examples.
That remains an exploitable blind spot. The engineering screen failed, and these
models are not enabled for automatic selection, EQ optimization or processing.

## Separate physical hum/clipping evidence

`soundcheck.artifact_health.analyze(samples, rate)` accepts only a candidate
waveform, 8–120 seconds, and returns suspicious-signal evidence plus alerts. It
returns no professional-quality score and authorizes no automatic correction.
The saved-audio CLI is usable without playback:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m wcfi_tools.cli audio mix-preference inspect-signal PATH --seconds 10
```

The analyzer combines full-excerpt narrow harmonic-line resolution with coherent
power persistence in 2-second blocks, plus relative flat-peak runs. The first
development version's coarse neighboring bins were masked by nearby bass notes;
long-resolution confirmation of at least two harmonics addressed that mechanism
without changing the fixed power/prominence/persistence thresholds. The original
source and control protocol/results are retained. This change does **not** alter
or rescue the frozen preference or broad-artifact result.

The follow-up chose different mix versions from 12 existing songs, excluding
both earlier control-source sets, and changed injected mains drift to −0.02 Hz.
It detected **24/24 hum cases** with the correct 50/60 Hz family and **12/12 hard
clipping cases**, with **0/36 alerts** on identity, gain-only and nearby 55 Hz tone
controls. There were **0/45 alerts** on supported clean ODAQ conditions across
15 content groups; one clip shorter than eight seconds remained unassessed.
Those clean conditions were reused development checks. These counts are not 45
independent songs or proof of zero future false alarms.

This narrow analyzer does not judge instrument balance, reject every noise type,
or certify a professional mix. Exact mains-frequency musical tones can imitate
hum, while lossy encoding can conceal flat peaks. Run it in a background analysis
worker, not an audio callback. The primary evaluator remains experimental, with
automatic application disabled.

Evidence retained under `_work/mix-preference-v2-2026-10-22-01`:

- `assessment.html`: combined findings, failure attributions and remaining gaps.
- `run-01/`: unchanged primary models, results, protocol-linked evidence and audit.
- `health-checks-01/`: initial protocol; the script aborted on a short ODAQ input.
- `health-checks-02/`: coarse-resolution development results, including missed hum.
- `artifact-health-v1.py`: previous detector source for those development results.
- `health-checks-03/`: refined detector protocol, frozen runner, new control-source
  results and independent audit, with short inputs explicitly unassessed.

The next preference evaluator needs stronger source/balance information and
broader independent listener labels. Repeated tuning of the same songs cannot
establish the professional church-mix judgment required for automatic editing.
