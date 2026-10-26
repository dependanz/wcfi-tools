# WCFI Control Node RED 0.3.0

The current development build is a distinct Windows x64 VST3 named **WCFI Control
Node RED**, with new processor/controller IDs. It calibrates a single mic once,
ramps to a bounded gain, and holds. It does not run the earlier continuous leveler.
The installed **WCFI Control Node 0.2.0** on LAPEL remains unchanged.

Run `build.ps1` to build workspace artifacts in `build-red-single-mic/` and run
the Steinberg validator plus native, host-integration and editor tests. Nothing
is installed or loaded into Ableton by that script. The offline renderer loads
`bin/WcfiMicBridge.dll`, which uses the same `MicEngine` as the new VST3. The old
`Engine` and `WcfiControl*` ABI are retained separately for regression coverage.

## RED controls and behavior

Fresh instances and restored presets start in **Manual**, with no learned gain.
The fresh Trim is 0 dB; restored Trim and limits are preserved within +/-6 dB.
The native button **Start calibration** enables learning; **Manual: use Trim**
takes control immediately and smoothly moves to the current Trim setting. It
does not capture the current automatic gain as a manual target.

| ID | Host parameter | Range | Fresh value |
| --- | --- | --- | --- |
| 100 | Calibrate & Hold | Off / On | Off |
| 101 | Trim dB | -6 to +6 dB | 0 dB |
| 102 | Target RMS | -36 to -12 dBFS | -26 dBFS |
| 103 | Max Boost | 0 to 6 dB | 6 dB |
| 104 | Max Cut | 0 to 6 dB | 6 dB |
| 105 | Activity Threshold | -90 to -24 dBFS | -55 dBFS |

The engine measures completed non-overlapping 100 ms raw-input RMS bins. Once
50 active bins exist within the trailing 30 seconds, their median determines a
one-time correction. A deciding bin cannot change its own output. Initial quiet
never calibrates; invalid audio clears unfinished evidence. This is energy
detection, not recognition of speech. A sustained noise source can qualify.

Calibration moves base gain at no more than 1 dB/s, then holds it. The startup
ramp can continue during pauses. During automatic operation, Trim is a manual
offset to the learned correction; in Manual it is the gain target itself.
Total target is limited by Max Cut/Boost and the base gain always stays within
the hard +/-6 dB range. Tightening a sub-limit bounds the command immediately;
previously applied gain transitions smoothly. Manual changes use 20 ms smoothing.

State labels are Manual, Waiting, Collecting, Ramping and Holding. Changing
Target or Activity Threshold after Holding does not restart learning. Select
Manual and then Start calibration for a new measurement. Presets never store
calibration evidence or automatically enable learning on reload.

A linked sample-peak guard remains active in **both** modes at -1 dBFS, recovering
at up to 6 dB/s. Its protective attenuation can exceed the downward gain bound.
It is not a true-peak limiter. Ordinary finite unity below that ceiling is exact;
over-ceiling samples are restrained even at Manual/Trim 0.

For agents, inspect explicitly with
`wcfi audio control-node inspect RED --device "WCFI Control Node RED"`.
The adapter requires the unique **Calibrate & Hold** host parameter so renaming
the legacy device cannot accidentally select RED's narrower gain scale. It owns
the complete device order and requires exactly one Analysis Node on each side,
allowing RED's existing Utility and EQ to remain inside the bracket. The intended
new insertion point is after that existing processing and before the post tap.
Agent configuration remains dry-run by default and rejects stale ownership.

The native editor and agent adapter use the same host parameters. The earlier
Ableton instance did not expose read-only meters through its scripting API, so
RED agent telemetry is not assumed available until tested in the host. A direct
Control Node RED host installation remains a separate review step.

## Legacy Control Node 0.2.0 reference

Windows x64 VST3 per-track manual trim and optional local leveler. The build script creates workspace artifacts without installing a plug-in or interacting with Ableton. It reuses the existing pinned Steinberg SDK read-only. The companion `WcfiControlBridge.dll` renders through the identical `Engine` used by the VST3.

Signal chain: **Analysis Node (pre) → WCFI Control Node → Analysis Node (post)**. The native editor and agents use the same six writable host parameters while the plug-in runs the fast audio loop locally. Host edits/automation are authoritative; there is no background service reapplying settings. There is no gate, mute, network listener, transport control, EQ, or cross-track ducking.

**Current deployment:** version 0.2.0 is installed and reloaded on LAPEL between its Analysis Nodes, with Auto Off and Trim 0 dB. The native editor opens in Ableton and shows changing input/output RMS and peaks; output matches input at unity. The six agent controls remain available. Live 11.3.35 does not expose this instance's read-only meters through the current scripting bridge, so agent meter inspection reports unavailable. The set was saved to the new copy `_work/rehearsal-automix-2026-10-18-01/preserved-session Project/wcfi-control-node-ui-2026-10-25.als`. The Auto algorithm is unchanged and remains experimental: its earlier LAPEL comparison failed the usefulness criterion. A new UI does not qualify that policy for live automatic mixing.

## Native editor

Open the plug-in editor from Ableton's plug-in edit button. The panel shows Auto state, manual Trim, Target RMS, Max Boost, Max Cut, and Activity Threshold alongside input/output levels, applied gain, automatic correction, peak-protection reduction, and the current activity state.

Type a number without units and click that row's **Apply**, or press **Enter** in the field. **Esc** restores the current host value without sending an edit. Trim's −/+ buttons move in 1 dB steps and preserve Auto's state. The Auto button is an explicit separate action; disabling Auto leaves manual Trim active. Numeric bounds are enforced and explained in the status message for invalid edits.

The editor keeps unsubmitted text when a host or agent change arrives. It flags the conflict and refuses to apply the older value until Esc reloads the current host value. Clean fields follow host changes. Each accepted user edit uses the host's begin/edit/end gesture path; incoming host updates do not echo another edit. A host failure reports an uncertain outcome without retry or rollback. These checks do not establish an atomic lease against a concurrently changing host.

Standard Windows text fields and buttons provide keyboard focus and accessibility names. Tab moves between controls. Enter and Space are handled within focused editor controls rather than being forwarded as host transport shortcuts. The native editor supports host content scaling.

## Read-only telemetry and host access

Version 0.2.0 adds ten read-only parameters. The original writable IDs 100–105 and six-value saved-state format are unchanged; meters are never stored in the preset or accepted as DSP configuration.

| ID | Host name | Reported range | Initial waiting value |
| --- | --- | --- | --- |
| 200 | Input RMS | −160 to +48 dBFS | −161 |
| 201 | Input Peak | −160 to +48 dBFS | −161 |
| 202 | Output RMS | −160 to +48 dBFS | −161 |
| 203 | Output Peak | −160 to +48 dBFS | −161 |
| 204 | Auto Gain | −24 to +24 dB | −25 |
| 205 | Applied Gain | −192 to +24 dB | −193 |
| 206 | Guard Reduction | 0 to 192 dB of attenuation | −1 |
| 207 | Activity State | Manual / Hold / Qualifying / Active | Waiting |
| 208 | Activity Gate | Inactive / Active | Waiting |
| 209 | Telemetry Sequence | 1–65535, wrapping to 1 | 0 |

The waiting values are inside the parameters' declared ranges but outside their real measurement ranges. They mean **unavailable**, not measured levels. They ensure the first real publication changes every value even at unity or digital silence. State codes used by the agent decoder are −1 waiting, 0 manual, 1 hold, 2 qualifying, and 3 active; activity codes are −1 waiting, 0 inactive, and 1 active. The host string lists include Waiting as their first item. Meter values beyond the reportable ranges saturate at the endpoints; the DSP's protective attenuation can be larger.

Input measurements come from the existing DSP. Output RMS and peak are measured from the actual processed samples with a rolling 100 ms window updated every 10 ms; output RMS is not estimated by adding the final gain to input RMS. RMS averages the channels. Peak values are sample peaks, not true peaks. Gain and activity fields describe the latest processor state.

The processor attempts publication through the host's optional `outputParameterChanges` at most 20 times per second, including failed attempts. It uses one point per meter and publishes the changed sequence only after all nine payload points were accepted. Missing or partial output queues do not affect the audio. The processor itself allocates no memory for metering; the host must supply realtime-safe queue implementations.

The editor requires every payload field to have been received and a changed, nonzero sequence received within 750 ms. A repeated sequence does not renew freshness; wrapping from 65535 to 1 does. Without current delivery it displays **NO LIVE DATA** and dashes while retaining the host's current editable settings. The 750 ms age measures **host delivery to the controller**, not the wall-clock age of captured audio. Payload fields are not committed as one atomic frame: partial or reordered host delivery may temporarily combine fields from nearby updates. These meters are not sample-aligned with the Analysis Nodes.

When the host exposes the meter parameters through its scripting API, `wcfi audio control-node inspect LAPEL` includes decoded telemetry separately from owned writable values. One inspection is labeled `unverified_single_snapshot`; `--verify-meters` compares two inspections for sequence advancement. That confirms delivery between those inspections, not sample alignment or a complete live-mixing qualification.

The verified Ableton 11.3.35 instance exposes Device On and the six writable settings through `device.parameters`, but none of the ten read-only meters. The bridge enumerates that host list without filtering, so reloading version 0.2.0 does not by itself enable agent meter access. The native editor receives the live meter updates through the VST3 controller. Keep the meter parameters read-only and nonautomatable; the Steinberg SDK explicitly defines read-only parameters as not automatable. Agent inspection currently reports telemetry unavailable, and `--verify-meters` cannot verify freshness in this instance. This is an observed host API limitation, not evidence that native metering failed.

## Writable controls and audio behavior

| Parameter ID | Host name | Range | Default |
|---|---|---|---|
| 100 | Auto Enable | Off / On | Off |
| 101 | Trim dB | −24 to +24 dB | 0 dB |
| 102 | Target RMS | −36 to −12 dBFS | −24 dBFS |
| 103 | Max Boost | 0 to 24 dB | 12 dB |
| 104 | Max Cut | 0 to 24 dB | 12 dB |
| 105 | Activity Threshold | −90 to −24 dBFS | −60 dBFS |

VST normalized values are `(plain − minimum) / (maximum − minimum)`. The Auto parameter is binary. The **sum** of trim and automatic correction is hard bounded to −24…+24 dB. Changing trim does not authorize an additional 24 dB beyond that sum.

Default Auto Off / Trim 0 passes finite mono/stereo float32/64 samples bit-exactly, including samples above 0 dBFS. Nonfinite input samples become zero and are counted. Nonzero manual trim is smoothed with a 20 ms time constant; finite numerical overflow in manual mode saturates only at the floating-point type maximum. Manual mode has no peak guard.

Auto uses 10 ms control intervals, a rolling 100 ms RMS measurement averaged across channels, and 150 ms consecutive energy qualification above the raw input activity threshold. This is **activity detection, not speech recognition**; use it on a dedicated source such as LAPEL. A steady room noise source above the threshold can qualify. The level target is sample RMS, not LUFS or a broadcast loudness specification.

Once qualified, automatic gain rises by at most 1 dB/s or falls by at most 6 dB/s, with a 0.5 dB deadband and configured boost/cut limits. The automatic offset holds during pauses and requires fresh qualification when activity returns. It does not reset toward zero in silence. The 20 ms output smoothing continues toward the held target during the boundary of a pause.

Auto mode also links the channels to a current-input **sample peak** guard at −1 dBFS. Restraint is immediate before each output sample, with protective attenuation recovering at no more than 6 dB/s elapsed time, including silence. This recovery is distinct from the held automatic level offset. It is not a true-peak or intersample limiter and can affect transients. Protective attenuation can exceed 24 dB for unusually hot float input; commanded trim plus auto remains bounded. Turning Auto off folds any protective attenuation into the current smoothed gain before returning to the manual target. Unity becomes exact after smoothing converges; switching settings does not abruptly snap to unity.

The gain engine uses no allocations, locks, files, sockets, or host messages in its processing path. The surrounding processor adds the bounded host-meter publication described above. It supports sample rates 8–384 kHz, mono or stereo, in-place buffers, realtime or offline processing, and sample-offset parameter changes. State saves only user settings; processing restart and state restore clear measurements and volatile gain. State parsing validates version, ranges, finite values, and stream completeness before publishing settings through bounded lock-free mailboxes.

## Build and verification

From the repository root, run `native/wcfi_control_node/build.ps1`. Existing Visual Studio C++ tools, the pinned SDK, and existing LLVM linker are required. It downloads nothing. The script uses idle priority, one CPU, and one build job. It builds the VST3, the offline DLL, native and UI tests, and an offline editor preview, then runs the SDK validator and CTest. Embedded debug information avoids reliance on the compiler's PDB service during configuration.

The default output is now `build-ui/`; `-BuildDirectory` selects another output folder. The VST3 bundle is under `VST3/Release/`, and the offline DLL, test executables, and `control_ui_preview.exe` are under `bin/`. The earlier `build/` and baseline evidence remain intact. The preview uses synthetic in-memory audio and a fake host; it does not send audio to a device or control Ableton.

The 0.2.0 workspace run passed 47 actual plug-in validator tests and both CTest suites. Coverage includes the prior 160 unity cases plus editor host gestures, conflicts, failed edits, output-meter accuracy, absent/partial queues, stale delivery and sequence wrapping. The agent adapter/CLI tests passed 27 cases. Evidence: `_work/control-node-ui-2026-10-25/build-ui.log`, `_work/control-node-ui-2026-10-25/python-tests.txt`, and `build-ui/Testing/Temporary/LastTest.log`. A separate Ableton check confirmed the installed editor and changing native meters. `_work/control-node-ui-2026-10-25/after-reload.json` records the six exposed settings and the absence of script-accessible meter parameters; native UI verification does not establish agent telemetry access or qualify Auto.

## Offline ABI v1

See `dsp.h` for the 48-byte configuration and 88-byte status structures. The DLL exports `WcfiControlCreate`, `WcfiControlConfigure`, `WcfiControlProcess`, `WcfiControlGetStatus`, `WcfiControlReset`, and `WcfiControlDestroy`. Processing takes interleaved float32 mono/stereo frames, permits in-place processing, and accepts at most 1,048,576 frames per call. The caller owns the handle and serializes all calls. Return codes are zero for success and −1 for invalid arguments; create returns null on invalid format/allocation failure. Invalid configuration preserves the last valid configuration.

Status contains input RMS/peak, automatic offset, applied gain, commanded target gain, separate protective attenuation, processed sample count, rejected nonfinite sample count, and the number of samples that imposed new headroom restraint. `state`: 0 manual, 1 inactive, 2 qualifying, 3 active. `activity` describes qualified input energy. Reset clears volatile state while retaining configured parameters. State is reset at the start of each independent offline render, never between ordinary blocks.

This prototype has synthetic native and SDK host validation plus a separate chronological LAPEL offline evaluation. Those checks do not establish live listening quality or full-band mix balance; no Ableton installation or live audition is implied by a successful build.
