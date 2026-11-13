# WCFI Control Node prototypes

## Offline seven-source mix coordinator

`src/wcfi_tools/audio/mix_coordinator.py` coordinates RED (lead), YELLOW/GREEN
(backing), EGUITAR, AC GUITAR, KEYS and KIT. BASS, LAPEL and MDMIC Talkback are
excluded from its exact input allowlist. It is a file-only Python prototype,
not installed in the VST3 and not connected to the Ableton gain writer.

The coordinator accepts a frozen starting balance, activity thresholds and
support-to-RED reference ratios. It uses completed 100 ms bins, requires three
seconds of qualified observations within a five-second window, and applies at most 3 dB of
support attenuation at 0.5 dB/second. Automatic corrections never boost above
the starting balance and never change RED. Quiet bins, abrupt energy changes,
invalid input and observation gaps cancel unfinished automatic ramps. Brief
activity interruptions preserve bounded evidence; one second of unsupported
input clears it. The current bin must agree with the direction of a proposed
change, so older evidence cannot cut a currently softer source or recover during
a currently excessive ratio. Three seconds of qualified observations does not
mean every observation shows positive drift. Valid
recovery evidence can return a support channel toward its starting gain. Manual
commands take ownership with a bounded, smoothed correction; release requires
new automatic evidence. A released RED manual correction stays held until a
manual zero command, because RED has no automatic policy.

Energy measurements do not establish singing, playing, bleed or intentional
accents. The summed dry peak veto blocks recovery when headroom is low; it is
not a limiter, and attenuation can increase peaks when sources cancel. This
version remains `live_eligible = False`.

`mix_coordinator_trial.py` selects only explicit dated arrangement WAV paths
from a verified metadata manifest. January dates and any extra/excluded track
are rejected. It calibrates on an earlier window, then renders a separate later
window as dry unity, frozen static balance, and static balance plus coordinator.
The static role offsets are exploratory listening candidates, not universal
instrument targets. Inactive calibration sources are never normalized upward;
backing microphones receive no initial boost. A provisional -45 dBFS minimum
for backing calibration medians disables low-energy references that may be
bleed; it does not identify singing. Those sources stay audible at their fixed
baseline, and their dynamic behavior remains unqualified. All three versions are available
with common scaling and with one whole-file loudness-matching scalar each.
No EQ, pan changes, compression, limiting or reverb is added.

The comparison does not reproduce the open Ableton master, which has legacy
Auto-enabled Control Nodes and Glue Compressor. Those processors, returns and
other nonselected tracks are absent from every offline render. Source-window
hashes, exact frame ranges, settings, per-bin reasons, gain envelopes and output
measurements are saved. Source reads do not change clips, set files or transport.

Example for the audited October 4 test set:

```powershell
python -m wcfi_tools.audio.mix_coordinator_trial `
  _work/gain-coordinator-2026-10-25/input-audit-seven.json `
  _work/gain-coordinator-2026-10-25/new-trial `
  --source-date 2026-10-04 --calibration-start 300 `
  --ffmpeg _work/reference-audit-2026-10-18-01/runtime-bin/ffmpeg.exe
```

The default windows are source 300–330 seconds for calibration and 330–450
seconds for evaluation. The source choice is development data, selected from
energy probes; it is not a held-out test. The runner uses one CPU at idle
priority. `test_mix_coordinator.py` and `test_mix_coordinator_trial.py` cover
causality, exact chunk invariance, gain/slew bounds, holds, manual ownership,
invalid input, absent sources and excluded-track rejection. Listening and
Ableton integration remain separate checks.

The 2026-10-25 development run is stored in
`_work/gain-coordinator-2026-10-25/trial-03`. All 36 focused tests passed. The
first seven-source run (`trial-02`) produced no automatic changes because its
consecutive-evidence rule reset frequently; it is retained as the earlier
result. Trial 03 uses the reviewed rolling-evidence revision on that same
already-seen passage, with low-energy backing references disabled. It is not
held-out validation.

The proposed initial gains are RED +6.00, YELLOW/GREEN 0.00, EGUITAR -15.14,
AC GUITAR -3.44, KEYS -6.91 and KIT -15.89 dB. Subsequent automatic movement
reached only -0.05 dB on AC GUITAR; all other automatic corrections stayed at
zero. Therefore the audible comparison primarily demonstrates the static
starting mix. The recording does not establish useful dynamic rebalancing or
accepted musical quality. The frozen static render is included to make this
distinction audible.

The original dry unity sum peaks at +3.25 dBFS; the unscaled static/coordinated
sum peaks at -3.05 dBFS. Audition copies use common gain or loudness matching
with headroom. The source KIT window contains 25,255 full-scale samples; gain
reduction cannot repair that recorded distortion. No plugin was installed and
no Ableton parameter, clip, route, fader or transport command was issued.

The separate `stress-01` regression injects a +10 dB KEYS fault during evaluation
seconds 20–50 using the same frozen settings and seven recorded sources. All
11 checks passed: KEYS began cutting at 21.6 s, reached the -3 dB correction
limit, began recovery at 53.5 s and returned to baseline. RED and disabled
backing channels remained unchanged. This demonstrates response to a deliberate
input fault, not perceptual improvement on the original recording. Its audio
is not substituted into the listening comparison.

## RED single-mic version, 0.3.0

The development VST3 **WCFI Control Node RED** implements automatic one-time
calibration, a bounded startup ramp, a held gain and manual control. It has a
distinct plugin identity and workspace build, so it does not replace the
installed LAPEL plugin during offline testing.

It collects five seconds of energy-active input within a rolling 30-second
window, computes a median-based correction, ramps at 1 dB/s, then holds. Total
base gain is hard bounded to +/-6 dB. Manual mode takes ownership immediately and
uses 20 ms smoothing toward Trim. Sample-peak protection stays on in both modes.
Startup ramps may continue through pauses; Holding freezes the learned setting.
The calibration detector is not speech recognition.

The native panel exposes Start calibration and Manual: use Trim, plus Trim,
Target, Boost/Cut limits and the activity threshold. Trim adjusts the learned
correction while calibration is enabled; Manual directly targets Trim within
the configured limits. A new calibration requires Manual then Start calibration.
Loading any supported preset restores Manual and never restores learned gain.

The host control **Calibrate & Hold** identifies RED's parameter scale to the
agent adapter, alongside the distinct device name. This prevents a renamed
legacy plugin from receiving a command scaled for the new +/-6 dB range.
Read-only host meter availability still requires verification in Ableton.

Build and detailed controls: `native/wcfi_control_node/README.md`.
Offline evidence is kept in `_work/red-single-mic-2026-10-25/`. Its two recorded
RED inputs were already examined, so this is implementation regression and
native/Python calibration parity, not a new listening or live-use qualification.
Installation and any change to meeting audio are held for review.

Offline build review completed on 2026-10-25: 47 Steinberg validator tests,
all three native suites (DSP/host integration, single-mic engine and editor),
38 agent adapter/CLI tests and 9 native-wrapper tests passed. The standalone
panel was also operated through calibration, Ramping, Holding, Manual at 0 dB
and a manual -1 dB step. That preview used synthetic memory-only audio and was
closed afterward.

| Recorded regression | Calibration ready | Holding reached | Output sample peak |
| --- | ---: | ---: | ---: |
| RED with a quiet start, 120 s | 70.6 s | 76.6 s | -5.12 dBFS |
| RED meeting, 180 s | 16.0 s | 22.0 s | -2.81 dBFS |

Both selected the additional +6 dB limit. Native readiness, support and locked
gain matched the Python calibrator. Frame counts, finite output, causal timing,
gain/ramp bounds and fixed Holding all passed, with no clipped samples. A separate
fixed manual scenario on the same meeting recording switched to -3 dB at 120 s
and 0 dB at 135 s. Both commands took manual ownership immediately, transitioned
smoothly and settled within 0.5 s without resuming learning.

The meeting render's later level-error metric matches the previously tested
fixed +6 dB reference. It does not claim a new perceptual improvement. Gain alone
still does not reach the nominal target on all phrases, and held gain also
amplifies background. An audible comparison and a controlled Ableton integration
check remain before changing the meeting feed.

The build is at
`native/wcfi_control_node/build-red-single-mic/VST3/Release/WCFI Control Node RED.vst3`.
The original installed plugin hash and the complete inspected Ableton state
match the previous checkpoint; transport remains stopped. The RED build has
not been installed or inserted. Evidence includes `render-01/report.json`,
`prepared-01/freeze.json`, `build-01.log` and `host-and-install-verification.json`
under `_work/red-single-mic-2026-10-25/`.

## Installed LAPEL prototype history

WCFI Control Node is a Windows x64 VST3 gain processor. Its first use is a dedicated
preacher LAPEL source. It makes bounded level adjustments from audio energy inside
the plugin, without requiring human cues or an agent request for each adjustment.
An agent can configure its six writable host parameters through the existing
Ableton bridge. The native editor uses those same parameters.

**2026-10-25 result: build accepted as a prototype; Auto policy failed the LAPEL
usefulness test. Do not promote this Auto policy to a live trial yet.**

At the user's subsequent request, the validated VST3 was installed into
`C:\Users\Danze\Documents\VST\VST3\WCFI\WCFI Control Node.vst3` and inserted
between the existing Analysis Nodes on LAPEL. Auto remains Off and Trim is 0 dB.
Ableton exposes all six writable parameters; the agent adapter passed a real-host dry-run
without writing a parameter. Mixer settings and existing devices were verified
unchanged except for this insertion; transport remained stopped. Initial 0.1.0 installation evidence is in
`_work/control-node-install-2026-10-25/verification.json`.

Version 0.2.0 is now installed and reloaded on LAPEL. Its native editor opens and
shows changing RMS and peak measurements, with output matching input at Auto Off
and Trim 0 dB. The Live set was saved as a new copy at
`_work/rehearsal-automix-2026-10-18-01/preserved-session Project/wcfi-control-node-ui-2026-10-25.als`.
The six agent controls remain available, but Live does not expose the read-only
meters through the current scripting bridge in this instance.

The system plugin folder required administrator access, so the new bundle uses
the user's Documents folder. Ableton's system VST3 scanning is now On, and its
custom VST3 folder is `C:\Users\Danze\Documents\VST\VST3`. Previously, the
custom folder pointed to the standard system VST3 directory with system scanning
Off. Existing system plugins remain available.

## Native panel and telemetry, version 0.2.0

Version 0.2.0 adds a native editor and read-only VST3 telemetry. It has passed the
plugin validator, native/UI suites, and 27 agent adapter/CLI tests. The installed
native editor and changing meters were then verified in Ableton. Agent access to
those meters depends on host API exposure and remains unavailable in this Live
instance. The Auto algorithm and its failed LAPEL evaluation are unchanged.

The panel presents Auto state, Trim, Target RMS, Max Boost, Max Cut, and Activity
Threshold. Type a number and use **Apply** or **Enter**; **Esc** restores the
current host value without sending an edit. Trim's −/+ buttons step by 1 dB and
preserve the Auto state. Auto Off leaves manual Trim active.

Clean fields follow host and agent changes. If another change arrives while a
person has unsubmitted text, the editor preserves that text, flags the conflict,
and requires Esc to reload before applying. It does not silently overwrite the
newer host value. Host failures report uncertainty without automatic retry or
rollback. Standard Windows fields and buttons support keyboard focus and
accessibility names; controls also scale with the host's display scale.

Input/output RMS and sample peaks, applied gain, automatic correction,
peak-protection reduction, and activity status appear beside the controls.
Output measurements use actual processed samples, with a rolling 100 ms window;
they are not calculated from input RMS plus the last gain value. The ten meter
parameters are read-only and excluded from saved settings and writable ownership.

The processor attempts host-meter publication at most 20 times per second. A
sequence advances only after the host accepts every payload point. The editor
requires all fields and a changed nonzero sequence received within 750 ms. It
shows **NO LIVE DATA** and dashes when delivery is unavailable or stale. This is
the age of host delivery, not a guaranteed audio-capture age. Meter fields are
not an atomic frame and are not sample-aligned with the Analysis Nodes.

Initial out-of-domain values mean waiting, not measured levels: −161 dBFS for
levels, −25 dB for automatic gain, −193 dB for applied gain, −1 dB for protection,
Waiting for activity, and sequence 0. Consumers must check readiness. The
sequence uses 1–65535 and wraps to 1; repeated values do not refresh UI freshness.
Exact meter IDs, ranges and state mapping are in
`native/wcfi_control_node/README.md`.

Version 0.2.0 evidence is in `_work/control-node-ui-2026-10-25/` and
`native/wcfi_control_node/build-ui/Testing/Temporary/LastTest.log`.
`after-reload.json` records the six settings exposed by Live and the absence of
read-only meters in its scripting parameter list. Native UI verification confirms
meter delivery to the editor; it does not establish agent telemetry access or
qualify the Auto policy.

## Original LAPEL policy result, version 0.1.0

The original plugin validator passed 47 tests. Native tests passed, including 160
unity cases and gain, transition, state, automation, ABI and concurrency checks.
The agent adapter passed 23 fake-host/CLI tests; the offline harness passed 13.
All technical checks on the frozen recording passed, with no added full-scale
samples and all 19 original exports unchanged.

| Median absolute error from the −26 dBFS target | Fixed +18 dB Trim | Auto |
| --- | ---: | ---: |
| Development, 0–120 s | 5.08 dB | 6.53 dB |
| Unfitted validation, 120–179.904 s | 7.41 dB | 8.38 dB |

The automatic correction therefore worsened validation error by 0.98 dB instead
of improving it by the required 2 dB. These are energy-selected measurement
windows, not a listening result. The detector qualified only 7.93 seconds.
613 upward correction ticks were outweighed by 116 cuts at six times the rate;
automatic gain ended at −0.83 dB and ranged from −2.98 to +0.29 dB. Longer,
louder syllables appear to dominate the detector, leaving reductions held into
softer speech. The threshold was calibrated using 100 ms windows, while native
qualification uses consecutive 10 ms windows. That mismatch needs investigation.

The subsequent offline phrase-level candidate is documented in
`docs/phrase-gain-prototype.md`. It improved development error to 3.95 dB but
missed its frozen 2 dB improvement requirement; the old failed render is intact.
It is separate from the installed native policy. A revised policy still needs
a fresh recording for confirmation because this excerpt has been examined.
No music ducking or live Auto enable is included in either evaluation.

Evidence: `_work/lapel-automix-prototype-2026-10-25/render-01/report.json`,
`trace-diagnostics.json` in the same render folder, and
`native/wcfi_control_node/validation-manifest.json`.

Intended device chain:

```text
WCFI Analysis Node (before)
    → WCFI Control Node
    → WCFI Analysis Node (after)
```

Both analysis points are in the device chain, before the track fader. The second
node measures the Control Node's output, not necessarily the final broadcast feed.
The existing TouchMix broadcast path is separate.

## What this prototype does

| Control | Range | Fresh instance |
| --- | --- | --- |
| Auto Enable | Off / On | Off |
| Trim dB | −24 to +24 dB | 0 dB |
| Target RMS | −36 to −12 dBFS | −24 dBFS |
| Max Boost | 0 to 24 dB | 12 dB |
| Max Cut | 0 to 24 dB | 12 dB |
| Activity Threshold | −90 to −24 dBFS | −60 dBFS |

Fresh Auto Off / Trim 0 passes finite samples at exact unity. Mono and stereo,
32-bit and 64-bit host samples, and in-place processing are supported. The plugin
adds no declared latency and performs no file, network, allocation, or locking
operations in the audio loop.

Auto uses a 100 ms RMS measurement, updated every 10 ms. It requires 150 ms of
consecutive input energy above the activity threshold before adjusting. This is
an energy rule for a dedicated source, not speech recognition. Raw input energy
is measured before Trim. Automatic boost moves at up to 1 dB/s; reduction at up
to 6 dB/s. The last automatic offset holds during inactivity. There is no mute or
noise gate. Changes in commanded gain are smoothed with a 20 ms time constant.

Trim plus the automatic offset is bounded to ±24 dB. While Auto is on, linked
stereo sample-peak protection immediately reduces gain as needed to keep output
samples at or below −1 dBFS. Protective attenuation releases at 6 dB/s, including
during pauses; it is separate from the held automatic offset. Abnormally hot
floating-point input can require protection below the −24 dB command limit.
This is not a true-peak limiter or an intelligibility guarantee. Auto Off removes
this protection while smoothly returning to manual Trim. Nonfinite input is
replaced with zero and counted.

## Frozen LAPEL experiment

The test uses the 179.904-second post-fader Ableton export made on 2026-10-25.
It is not a recording of the TouchMix broadcast. Parameters were chosen using
seconds 0–120; seconds 120–179.904 were held back from tuning, although coarse
30-second statistics for the whole recording were already available.

The candidate is Trim +18 dB, Target RMS −26 dBFS, Max Boost / Cut 6 dB, and
Activity Threshold −55 dBFS. Its output is compared with the original LAPEL and
with the same native DSP using fixed +18 dB Trim and Auto Off. These values are
specific to this export; they are not universal live settings.

The Python renderer calls a DLL built from the same DSP source as the VST3.
It retains original effect returns and substitutes the direct LAPEL contribution
only after verifying additive reconstruction from the exported groups and master.
Such a preview does not simulate changes to post-device effect sends.

The prospective usefulness criterion is at least a 2 dB improvement in median
absolute active-window error from the target compared with fixed Trim. Technical
checks cover bounds, finite samples, frame counts, silence behavior, peaks, and
unchanged source hashes. Energy-selected windows are not labeled speech. A
passing result still requires a listening comparison and a real host check.

Music is unchanged in this milestone. LAPEL-to-INSTRUMENTS overlap is measured
to show whether another small step is needed to keep continuing music softly
underneath speech. Full-band balance and EQ decisions are outside this prototype.

## Agent configuration

`wcfi audio control-node inspect LAPEL --output binding.json` reads a node and
writes a new ownership binding. It requires immediate Analysis Node neighbors.
The caller must separately verify their labels and measurement stages.

When Live exposes the read-only meter parameters, the result also includes
telemetry marked `unverified_single_snapshot`. Add `--verify-meters` to compare
two inspections for sequence advancement. That confirms delivery between
inspections, not sample alignment or overall mixing readiness. Volatile meter
changes do not invalidate ownership of the six writable controls.

In the verified Live 11.3.35 instance, `device.parameters` contains Device On and
the six editable settings only. The bridge passes that complete list through;
it does not filter out meter parameters. Therefore agent inspection reports
telemetry unavailable even after the 0.2.0 reload, and `--verify-meters` cannot
confirm freshness here. Native meters remain visible in the plugin editor.
Their read-only, nonautomatable flags are retained; exposing them as writable
automation would conflict with the SDK's read-only semantics. No additional
telemetry transport is included in this step.

`wcfi audio control-node set binding.json --setting trim-db --value 18` performs
a dry-run. An actual write requires `--apply --receipt new-receipt.jsonl`.
The receipt is created without overwriting another file, flushed before the
request, and contains the confirmed next binding on success. Subsequent calls
must use that returned binding; the original becomes stale after a change.

The adapter compares session, device order, parameter identities, ranges, and all
six owned values. It rejects disabled or automated parameters. The actual write
uses the originally owned value as the host's compare-and-set condition. Manual
changes stop further commands. An uncertain reply causes no retry or rollback.
Only the target parameter is checked atomically by the host; another parameter
or topology change during the request can only be detected on readback.

The current host state should remain Auto Off with Trim 0 dB. The recorded LAPEL usefulness failure means
this Auto policy is not approved for a live automatic trial. A later policy
revision needs separate evaluation before any Auto enable. Save the result of
every confirmed configuration change. Turning Auto Off preserves manual Trim;
it is not equivalent to removing all gain. No agent should continuously reapply
a preset over manual edits.

## Build and review artifacts

Source and build script: `native/wcfi_control_node/`.
The script uses the existing pinned Steinberg SDK, builds one job at idle priority,
and runs the SDK validator and native tests. It does not download, install,
scan Ableton plugins, or load a device. Python tests use a fake host.
The 0.2.0 build defaults to `native/wcfi_control_node/build-ui/`, preserving the
older build and its evidence. Its editor preview uses synthetic in-memory audio
and a fake host, with no playback device or Ableton control.

Frozen settings, split, render runner, gain trace, comparisons and results:
`_work/lapel-automix-prototype-2026-10-25/`.
The exported source recordings and previous experiments are retained separately.

The initial implementation milestone stayed offline. The later installation is
recorded above; Auto remains Off. No Play command, fader, routing or mute changes
were made, and the TouchMix broadcast path was not altered.
