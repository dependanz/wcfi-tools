# WCFI Audio Emulator

A local Windows x64 audio interface for at-home Ableton tests: **32 mono inputs,
32 mono outputs, 48 kHz, float32**. It supplies real or procedural raw sources,
adds repeatable input problems, and measures what the DAW returns. JACK and a
physical mixer are not required. No command controls Ableton's Play or Record.

For observation inside a DAW's effect chains, see
[WCFI Analysis Node](../native/wcfi_analysis_node/README.md). This direct VST3
plugin copies audio into the app's **Analysis nodes** page independently of the
selected audio interface. It does not require spare ASIO outputs, and it does
not change the mix or provide automatic mixing yet.

The React interface runs in a Tauri desktop window or a browser. Both use the
same local Python service and native ASIO bridge. Analysis Nodes can be auditioned
through the computer's system default playback output. Automixing is not implemented
yet. Selecting the emulator replaces the physical ASIO interface in the test set.

## Listen to an Analysis Node

Open **Analysis nodes** and choose **Listen** on a card, or use **Choose a node to hear**.
The app plays a copy through the system default output using Web Audio in its desktop
window or browser. There is one selected node per window. Switching nodes replaces
the listening source; it does not solo/mute tracks or change Ableton routing.

**Listen volume** starts at −18 dB and affects only this copy. **Stop listening** stops
playback immediately. Listening stays active when navigating between app pages, but
closing/reloading the window stops it. It never resumes automatically. The headless
service does not open a playback device. No microphone permission or new driver is needed.

This is buffered auditioning, with a bounded playback queue and automatic resync if
the observation stream falls behind. The displayed queued time is not a measurement
of physical input-to-speaker latency. The playback engine resamples when the node and
system output rates differ. Start listening again after changing the system output
if the old device remains selected. Unavailable/stale nodes or invalid samples stop
the listening copy with an error; they cannot interrupt the DAW's signal path.

PCM is fetched from the authenticated loopback API. Requests have independent native
read handles/cursors; observers cannot backpressure VST3 callbacks. No audio is uploaded
to a cloud service. The monitor adds no EQ/compression or loudness normalization; raw
floating-point levels are preserved before the separate listening gain. Check its
full-scale warning when auditioning unusually hot signals.

## Quick start

After the one-time builds and driver registration below:

```powershell
wcfi audio emulator launch
```

Use `wcfi` from the activated Python environment. From this checkout without
activation, use `.\.venv\Scripts\wcfi.exe audio emulator launch` instead.
From another folder, use the full path, for example
`& 'C:\dev\wcfi-tools\.venv\Scripts\wcfi.exe' audio emulator launch`.
To make `wcfi` available in normal PowerShell windows, run
`& 'C:\dev\wcfi-tools\scripts\install-cli.ps1'` once, then restart existing
terminals. It creates a per-user launcher for this checkout without replacing
your default Python or requiring administrator rights.

Launch starts the background service, brings the emulator online, and opens
the Tauri window. Inputs start paused. In Ableton Preferences → Audio, choose
ASIO → **WCFI Emulator**. Enable the required channels in Input/Output Config.
Map each audio track to an external input and set Monitor to **In** to receive
it while the arrangement is stopped. Choose Master output **1/2**, or **23/24**
for the church layout, and select that pair in Output analysis.

Load **Two-tone routing test**, then click **Send inputs**. It enables input 1
at 220 Hz and input 2 at 330 Hz, both at −30 dBFS peak. Presets replace the
channel configuration and always pause inputs first. Use real stems next to
evaluate musical processing; tones assigned to vocal roles are not singers.

To open the same interface in a browser:

```powershell
wcfi audio emulator launch --web
```

The browser still needs the local service and registered ASIO DLL. A public
website cannot itself supply an ASIO device. Remote hosting is not configured.

## Load a synchronized recorded take

In **Input channels**, choose **Load recorded take**, select **Sunday - October
18, 2026**, then **Load selected take**. The take's manifest maps the 11 original
files onto 14 inputs, including stereo pairs. Original file levels are retained;
mapped channel gains start at 0 dB. Sending stays paused until explicitly enabled.

The **linked recorded take** panel controls every recorded channel together:

- Enter one loop start/end in seconds and click **Apply shared loop**. Boundaries
  are converted once to integer sample positions and used by every stem.
- **Use full take** selects the complete 6:56.576 recording. **Restart take**
  returns to the current range's start. Both actions pause sending.
- The playhead advances only while inputs are sending to a connected DAW.
  Pause/resume, muting and disabling individual inputs preserve synchronization.
- **Smooth loop edges** applies the same 5 ms fade at each boundary on every
  channel. It preserves the exact loop length without overlapping or stretching
  audio. Turn it off for unmodified samples; exclude boundaries from mix scoring.
- **Unload take** restores the input setup that preceded loading it. Take selection,
  loop boundaries and channel controls persist; a service restart restores them
  with sending paused and the playhead at the loop start.

Mapped take inputs retain name/role, gain, mute, solo and enable controls.
Their individual source, loop and fault settings are locked while linked; unload
the take to use the independent source editor. Recorded files can include unused
lapel/talkback/laptop feeds, so select the channels appropriate to the test.

The service discovers `real-sources/<take-id>/manifest.json` next to the source
checkout. `WCFI_EMULATOR_SOURCE_ROOT` can select another local library. The current
version 1 take format requires mono/stereo files aligned at frame zero, identical
frame counts, 48 kHz, unique one-based input mappings and per-file SHA-256 hashes.
The loader confines paths to the take folder, checks hashes and actual audio
metadata before replacing the current setup, and streams small blocks from disk.
It does not decode the complete recording into RAM or send files over the network.
If a saved take is unavailable, the previous independent setup is restored with
a visible warning. These changes do not affect Ableton's transport.

By default, the service queues up to 8192 input frames (about 171 ms at 48 kHz) ahead of
the ASIO consumer to absorb disk and Windows scheduling stalls. This is separate
from the ASIO buffer size. Input edits and pausing can take that much additional
time to reach the DAW; all stems still advance together. The emulator is intended
for rehearsal tests, rather than measuring a physical interface's latency.

For rehearsal runs with intermittent input underruns, an optional larger queue
can absorb longer scheduling stalls. Set the environment variable before starting
a new service process:

```powershell
$env:WCFI_EMULATOR_INPUT_LEAD_FRAMES = '16384'
wcfi audio emulator start
```

If the service is already running, shut it down when its simulated input can be
interrupted, then start it again. The setting is read once at service construction;
calling `start` on an existing service does not change it. Sending resumes only when
explicitly enabled. The default remains 8192; accepted values are integer multiples
of 512 from 4096 through 24576. This leaves space within the 32768-frame native ring.
Each producer refill remains capped at 4096 frames.

16384 frames represents about 341.3 ms of lookahead, adding up to 170.7 ms to source
control response compared with the default. This is **not an ASIO latency setting**
or a measured end-to-end delay. The status API reports `input_lookahead_frames` and
`input_lookahead_ms`; `driver.input_queued` reports the queue actually present.
Additional lookahead does not fix sustained producer overload. Check input-underrun
and output-drop counter changes during captures, and reject interrupted comparisons.
Remove the environment variable before restarting to return to the default.

## Headless operation and lifecycle

```powershell
wcfi audio emulator start
wcfi audio emulator preset two-tones
wcfi audio emulator send
wcfi audio emulator analyze --channels 1,2
wcfi audio emulator pause
wcfi audio emulator stop
wcfi audio emulator shutdown
```

| Action | Effect |
| --- | --- |
| `start` | Idempotently start the service and enable the device; a stopped device restarts with inputs paused |
| `launch` | Start/enable the device and open its control window |
| `send` / Send inputs | Send enabled sources; never start DAW transport |
| `pause` / Pause inputs | Fade simulated inputs down; leave the device online |
| `stop` / Stop emulator | Mark the device offline and clear simulated input; keep the service/UI usable |
| Close window | Leave the service running |
| `shutdown` | Stop the emulator and exit the background service |
| `status` | Read channel configuration, service state, meters and driver counters as JSON |

The UI's **Start emulator** button brings a stopped device online again.
Opening or starting an already running service does not interrupt sources that
are already sending. A fresh service never resumes sending automatically.
Channel settings and imported audio persist under the user's local application
data directory (`%LOCALAPPDATA%\wcfi\audio-emulator` on this Windows setup).
`WCFI_EMULATOR_HOME` can select a separate state directory for development.

**ASIO availability:** the DLL refuses initialization/start while the emulator
is stopped, absent, or has missed its heartbeat for two seconds. An already
connected host receives zero simulated input while offline; effect tails inside
the DAW may continue. Ableton can cache the registered driver name in its menu,
so that name may remain visible while offline. Opening it fails until the
emulator starts. We do not register/unregister the driver on every start/stop.

## Input controls

Each mono input has a name, source role, enabled/mute/solo switches and a gain
from −60 to +12 dB. Adjacent mono inputs can be paired in Ableton for stereo.
Source changes crossfade over 10 ms; gain changes are smoothed over roughly
50 ms. These controls affect raw signals before Ableton's effects and faders.

| Source | Settings |
| --- | --- |
| Local audio file | WAV, FLAC, AIFF, OGG or MP3; choose a file channel and start offset |
| Sine wave | Frequency and peak level |
| Noise | Gaussian signal at a nominal RMS level |
| Plucked notes | MIDI note numbers, tempo and level; procedural synthesis |
| Mixed drum kit | Procedural kick, snare and hat combined into one feed |
| Silence | Unused input |

The editor imports files into the local service (maximum 128 MiB per file,
up to 64 file channels). It prepares and resamples a 1–30 second excerpt before
switching sources. Use matching offsets and lengths on aligned stems to keep
their timing together. Loops can click at their boundaries; source crossfades
smooth edits, not every loop boundary. No neural model, sampler download,
cloud upload or API charge occurs.

Input conditions model an overdriven preamp, 60 Hz electrical hum and harmonics,
background hiss, or a muddy signal. Clipping happens **before** the downstream
trim, so damaged audio can arrive at a quiet meter level. The raw output is
bounded to full scale. The procedural kit is one mixed signal; this does not
recover individual drums from a combined recording.

## Output analysis

Select any output or pair to inspect the most recent full second of returned
audio: sample peak, RMS, crest factor, dominant FFT frequency, full-scale sample
count, waveform envelope, spectrum and pair correlation. Measurements update
locally without recording/exporting in Ableton. The service keeps a rolling
three-second buffer in memory, not an audio recording on disk.

RMS is not perceptual loudness. These measurements are not LUFS, true peak,
sibilance detection, machine listening or a musical quality score. A channel
can contain clipped audio below full scale; the full-scale counter alone cannot
identify all clipping. Stale/disconnected output is marked unavailable. Driver
underrun/drop counters help diagnose scheduling problems; they are not latency
guarantees.

## Build the native driver once

Install Python audio dependencies from the checkout:

```powershell
Set-Location C:\dev\wcfi-tools
.\.venv\Scripts\python.exe -m pip install -e '.[dev,soundcheck]'
```

Download/extract [Zig 0.15.2 Windows x64](https://ziglang.org/download/0.15.2/zig-x86_64-windows-0.15.2.zip).
Archive SHA-256: `3a0ed1e8799a2f8ce2a6e6290a9ff22e6906f8227865911fb7ddedc3cc14cb0c`.

```powershell
.\native\wcfi_asio\build.ps1 -Zig C:\path\to\zig.exe -Test
.\native\wcfi_asio\register.ps1
```

Registration requests Windows administrator approval and writes only this
driver's fixed 64-bit ASIO/COM keys. It rejects conflicting registrations.
Keep `native\wcfi_asio\build\WcfiAsio.dll` at its registered path. Unregister
with `register.ps1 -Uninstall`.

**Upgrading the earlier prototype:** save and close Ableton and run
`wcfi audio emulator shutdown` before replacing/rebuilding the DLL. Live can
keep it loaded even after choosing No Device. The v2 DLL is required for service
availability control. If its registered path is unchanged, re-registration is
not required. Reopen Live, start the emulator, then select the device.

Native tests use `WcfiAsioTest.dll` and a separate shared-memory namespace;
they cannot inject audio into the production Ableton session. Tests cover
offline refusal, availability, exclusive roles, 32-channel loopback, multiple
buffer sizes and repeated starts/stops. Registration tests redirect keys within
the test process; they do not install the production driver.

## Build the Tauri window once

Install Node.js LTS, stable Rust for `x86_64-pc-windows-msvc`, Microsoft C++ Build
Tools with **Desktop development with C++** and a Windows SDK, plus WebView2
Runtime if absent. See [Tauri's Windows prerequisites](https://v2.tauri.app/start/prerequisites/).
The frontend and Rust dependencies are pinned in their lock files.

```powershell
Set-Location C:\dev\wcfi-tools
.\desktop\build.ps1
wcfi audio emulator launch
```

The build script imports the installed Microsoft developer environment and uses
two compiler jobs by default. `-Debug` builds faster for development; `-Jobs N`
changes parallelism. `-UseLld` uses Rust's bundled COFF linker with the installed
Microsoft SDK libraries; use it if Microsoft's linker repeatedly reports
`LNK1105` while closing temporary `.exp` files. The CLI looks for release then debug `wcfi-audio.exe` under
`desktop\src-tauri\target`. `WCFI_EMULATOR_DESKTOP` can select another executable.
The native binaries are currently checkout builds, not included in the Python
wheel. Signing, an installer and standalone distribution remain future work.

To rebuild just the shared browser UI:

```powershell
Set-Location C:\dev\wcfi-tools\desktop
npm.cmd ci --no-audit --no-fund
npm.cmd run build
```

Assets are written into `src/wcfi_tools/audio/web` for the Python service.
The UI follows [wcfi.org](https://www.wcfi.org/)'s navy, white, gray and lavender
palette. It bundles OFL-licensed Montserrat/Manrope as alternatives to the site's
Lulo/Avenir/Helvetica typography; proprietary site fonts are not redistributed.
Font licenses ship with the web assets.

## Local boundary and development

The service binds only to `127.0.0.1` on a random port. The CLI supplies a local
session capability in the initial URL fragment; the UI retains it for API
authorization and removes it from the displayed URL. Requests require that
capability, the exact local Host, and a matching Origin when present. No CORS
access is enabled. Do not publish the discovery file or authenticated launch URL.

The native callback owns timing, ASIO buffers, sample position and shared rings.
No Python, file reads, inference or allocation runs in that callback. Python
prepares sources and reads outputs on separate service threads. A missing
producer yields silence; full output rings increment a drop counter. Windows
scheduling still affects real-time behavior. Buffer sizes are powers of two
from 128 to 2048 frames; start with 512 or 1024.

The older `wcfi audio emulate init/render/scenarios` profile tools remain useful
for offline diagnostic stems and richer ordered faults (`gain`, `clip`, `hum`,
`noise`, `mud`, `levels`, `dropout`). The new service owns both ring roles while
the DAW is connected. Do not run legacy `emulate start` or `audio capture` against
the same device at the same time; use this UI/service for interactive input and
output measurements.

Run checks from the repository:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

Desktop listening lifecycle checks: `npm.cmd run test:monitor` from `desktop/`.

**Rehearsal lab** adds RED/KIT role-aware observations, locally looped comparison
audio, gain-only and experimental HF previews, and explicit guarded gain application
through the Ableton bridge. Start with a raw-stem study; capture the current nodes
before applying a proposal to Live. See the [comparison and Apply workflow](role-analysis.md#rehearsal-lab-in-the-app)
for supported processors and limits. Analysis and local listening never use Live’s transport.
Prepared KIT studies add loudness-matched EQ/compression comparisons both in the
full mix and in isolation. Native compression captures and local EQ previews
are labelled separately; test settings are restored after capture.

This is an at-home development interface. A church broadcast that needs the
TouchMix's physical outputs must use the physical interface. For a future
differentiable DSP library, keep the portable DSP graph, inference/optimization,
DAW control and ASIO transport separate. Streaming samples through ASIO does not
make arbitrary Ableton effects differentiable.

