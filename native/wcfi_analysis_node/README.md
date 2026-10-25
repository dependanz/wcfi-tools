# WCFI Analysis Node (Windows x64 prototype)

Direct C++20 / Steinberg VST3 SDK. No JUCE or VSTGUI. The processor, native
Win32 editor, state format and local observation transport are ours. SDK
helpers provide the standard VST3 component/controller/factory interfaces.
The SDK is pinned to `v3.8.1_build_84` / commit
`3cdf9ca5d1f5b1b21e0a86832aa4abe55607bd96`, with its own MIT license.

## Build and install

Requires Microsoft C++ Build Tools (Desktop development with C++, Windows SDK),
CMake/Ninja from that installation, and Git.

```powershell
.\native\wcfi_analysis_node\build.ps1 -Fetch
# If Microsoft's linker encounters error 1224, use LLVM's COFF linker:
.\native\wcfi_analysis_node\build.ps1 -Lld 'C:\path\to\rust-lld.exe'
```

Build runs Steinberg's validator and native passthrough/transport tests. Output:
`build/VST3/Release/WCFI Analysis Node.vst3` (a whole directory bundle) and
`build/bin/WcfiAnalysisBridge.dll` (the app's read-only capture bridge).

For the standard system VST3 directory, run in an Administrator PowerShell:

```powershell
& 'C:\dev\wcfi-tools\native\wcfi_analysis_node\install.ps1'
```

Alternatively pass `-Destination` pointing to an existing Ableton custom VST3
folder you can write to. Do not replace a bundle while a DAW has it loaded.
There is no driver registration and no JACK dependency. The plugin works
alongside the physical TouchMix ASIO driver or the WCFI Emulator ASIO driver.

## Use

1. Rescan plugins in Ableton, then put **WCFI Analysis Node** first in RED's
   device chain and a second instance last.
2. Open each editor. Give both the channel name **RED**. Choose **Before effects**
   for the first and **After effects** for the last, then click **Apply label**.
3. Start `wcfi audio emulator launch` and open **Analysis nodes**. Capture runs
   independently of emulator sending, transport Play, and the selected interface.
4. Repeat for a group, return, or Master if desired. Save the Live set to preserve
   labels/positions. Each loaded instance has a distinct runtime transport ID,
   including instances created by duplicating a saved device.

Normal track and Master output routing stays unchanged. No extra output pairs,
send changes, observer tracks, recording or export are required. Input monitoring
and the DAW audio engine must actually process the track for live observation.
The plugin cannot capture an unprocessed/frozen/deactivated track.

The last node in a track, group, return, or Master chain is **before that track's
fader, pan and mute**. On a return, the first node observes the summed sends
entering its effect chain. Use the emulator's existing output capture on 23/24
to observe the true final Master output when that interface is selected. The
plugin does not claim to observe a track's post-mixer signal.

## Audio and timing guarantees / limits

- Mono/stereo float32 and float64, separate or in-place buffers, transparent
  sample copy (including signed zero and headroom); zero declared latency and
  tail. No gain, EQ, dynamics or automatic mix changes in this milestone.
- Bounded float32 stereo observation copies in a 32,768-frame per-instance ring
  (~683 ms at 48 kHz). Mono is duplicated only in observation data. Passthrough
  keeps the original sample format and channel layout.
- No allocations, locks, network, files, SDK messages, UI work or waits inside
  `process()`. A missing/slow/crashed app never blocks the audio writer. Old
  observation samples are overwritten; skipped frames are counted by the app.
- Offline renders pass through without publishing into live observation.
- Per-user, per-Windows-session shared memory; read-only passive readers with
  independent cursors and bounded sequence-lock retries. Audio stays local.
- Collector polls every 10 ms, cached views target 10 Hz, meters use 50 ms,
  waveforms show 500 ms. Capture/view ages and browser request/render-opportunity
  timings are separate from audio latency. At most 128 simultaneous node slots.
- Pair alignment uses matching channel name + host PID, a unique before/after
  pair, equal sample rates, and an advancing host continuous sample clock. It
  refuses silence, weak correlations and ambiguous repetitive signals. It is
  observed alignment under DAW delay compensation, not uncompensated plugin
  latency or physical input-to-speaker latency. Hosts omitting usable timestamps
  still provide meters/waveforms; alignment stays unavailable.

The test executable's `--stream` option publishes explicitly labeled native test
signals for one minute, solely into analysis shared memory, never into Ableton
or a sound device. It is for cross-process collector/UI verification.

Ableton's RED before/after pair has been checked with recorded rehearsal stems
and stopped transport. A longer rehearsal soak test is still needed before
livestream use. Session-wide insertion, automatic track names,
post-fader capture with a physical interface, and auto-mixing are subsequent work.

## Initial validation — October 20, 2026

On this Windows x64 development laptop:

- Steinberg validator: **47 passed, 0 failed**.
- Native tests: **160** mono/stereo, float32/64, in-place/separate, silent/non-silent,
  offline/realtime, and variable/zero-block cases, plus state persistence, ring
  overwrite, multiple passive readers, discovery and restart epochs.
- Python suite: **111 passed**; audio modules lint and TypeScript/Vite build passed.
- Two actual processors in an isolated native host streamed for one minute.
  The app discovered both, paired their labels, showed waveforms/meters, measured
  **0.00 ms** relative alignment, and reported **0 skipped observation frames**.
  These were test signals with no audio-device connection, not church audio.
- During 40 local API samples: p95 capture age **2.17 ms**, cached-view age
  **94.0 ms**, API round trip **60.6 ms**. The visible desktop UI reported p95
  update interval around **130 ms**. This is a short functionality measurement,
  not a sustained-load guarantee or physical audio latency measurement.
- Standalone capture-copy benchmark, 20,000 mono/512-frame calls: p95 about
  **1.3 microseconds**, worst observed **200 microseconds** in the streaming test
  invocation. This times the transport copy, not a full DAW callback.
- Exited instances disappeared from the app. Emulator inputs remained paused.

The standard-folder install subsequently succeeded after the approved Windows
administrator prompt. Ableton Live 11.3.35 discovered the plugin. One instance
was inserted before RED's existing effects in the separate test Live set; the
app received its stereo 48 kHz buffers while transport was stopped and emulator
inputs were paused. A status sample showed capture age 1.46 ms and zero skipped
observation frames. This verifies basic host loading/capture, not musical audio,
paired alignment, saved-label recall or a rehearsal soak test. The label remains
`Unassigned` during that initial test: the native-control connection could not reliably target the floating
editor's controls. The editor also displayed a minor text-encoding artifact in
its static latency caption. Normal routing and mix controls were not changed.
Evidence is in `_work/red-taps-2026-10-20/` at the repository root, including
`native-node-connection.json`, screenshots, and test/build logs. The working
artifact directory is ignored by Git.

### Actual RED before/after pair with Sunday's stems

A new test copy, `wcfi-red-pair.als`, was created in the existing preserved-session
project. It contains an Analysis Node first and last in RED's top-level device
chain, labeled `RED` with positions 0/1. The new copy was configured using our
own documented 136-byte processor-state format, then loaded in Ableton. Existing
effects, all other tracks, and routing/mixer XML were verified unchanged; the
original test set was retained. This avoids the floating editor's control issue.

Sunday's 11 sources / 14 inputs were verified against the Live set's external
input assignments and fed through one shared **60–76 second loop**, with a 5 ms
boundary fade. Ableton transport stayed stopped throughout; no Play or Record.
Master routing remains stereo **23/24**. Both actual plugin instances appeared
in the app, with distinct runtime IDs and their saved labels/positions restored.

Read-only verification over 36 seconds found:

- Two observed shared-playhead wraps, with exactly identical pre-effects samples
  one 16-second period apart (maximum sample difference **0**).
- Before-effects waveform matched the original RED WAV at correlation
  **0.999999999998**, unity gain within float precision.
- **0** observation frames skipped; **0** new driver input underruns, output
  drops or late callbacks.
- Pre-effects peaks **−15.27 dBFS**. Post-effects peaks **+0.61 / +0.95 dBFS**
  and RMS **−10.28 / −10.25 dBFS** over this captured interval. Internal floating
  point samples exceeding full scale do not establish clipping at the final
  Master or physical output. The existing processing was not adjusted.
- Capture age p95 about **10.4 ms**; local API round trip p95 **46.1 ms**.
- Ableton supplied no process-context timestamps while stopped. Pair alignment
  therefore remains explicitly unavailable; no audio latency is inferred from
  GUI timings or independently started counters.

The stem loop was left running in the emulator. Evidence includes
`pair-setup.json`, `red-pair-loop-validation.json`, `red-pair-live-in-app.jpg`,
and `stems-with-ableton-stopped.jpg` in the same working-artifact directory.
