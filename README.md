# wcfi-tools

A small toolkit for **Word Christian Fellowship International**, exposed as a CLI (`wcfi <command>`).
The engine is UI-agnostic so the same core can later back a web app or desktop app.

First tools:

- **`wcfi setup`** — one-time interactive setup for the API keys the other tools need (OpenAI for
  transcription; OpenAI or Anthropic/Claude for summarization). Keys are stored in your OS keyring.
- **`wcfi meeting summarize <folder>`** — turn a folder of meeting audio into a summarized set of
  copy-able minutes artifacts.
- **`wcfi soundcheck`** — analyze a full-band multitrack rehearsal and create a calibrated
  Ableton Live 11 set copy with fixed input gain and restrained EQ. Runs locally without API keys.
- **`wcfi audio ableton`** — inspect the open Live Set, control faders and exposed plug-in
  parameters, and move existing devices through a background Remote Script. See the
  [Ableton bridge guide](docs/ableton-bridge.md) for installation and activation.
- **`wcfi monitor-control launch`** — view and operate another connected display from
  one window on your laptop, or control an application even while it sits behind the viewer.

## Monitor control (Windows prototype)

```powershell
pip install -e ".[monitor-control]"    # install Windows capture support once
wcfi monitor-control launch            # choose a display or application window
wcfi monitor-control list              # show connected display numbers and sizes
wcfi monitor-control launch --monitor 2
wcfi monitor-control launch --practice # practice even when multiple displays are attached
```

Choose **Displays** and a display, then move into its preview to click, drag, and scroll.
On one screen, use **Applications** to search for a window and interact with its preview. Lists
update automatically; each view remembers its search and selection. The app can remain behind
the viewer. Its owned dialogs, context menus, and submenus appear automatically in the same
preview without releasing control. **Arrange side by side** is optional.
Moving or resizing the app updates the preview and input coordinates without automatically
ending control; input waits for matching pixels. Focus loss and stale previews still release it.
In **Applications**, windows can cross display boundaries and owned dialogs on other
displays join the same preview. Gaps between displays and offscreen areas cannot receive input.
If focus or preview gets stuck, release control and choose **Recover selected**, then
resume control once the preview returns. It restores minimized apps without rearranging
them. Stalled capture also retries automatically; recovery never resumes input by itself.
Move outside the preview to use the viewer normally; **Ctrl+Alt+Q** is an emergency stop.
There are no Control/Release buttons. Python with Tcl/Tk support is required; no API keys,
`wcfi setup`, network connection, or driver installation is needed. See the
[monitor control guide](docs/monitor-control.md) for controls, practice mode, and limitations.

## Morning soundcheck

Install the audio extra: `pip install -e ".[soundcheck]"`.
See [the soundcheck guide](docs/soundcheck.md) for recording instructions and limitations.

```powershell
# One-time: read the saved Live 11 set and create a channel profile.
wcfi soundcheck init "C:\path\Service.als" --profile soundcheck.json

# Review guessed source roles/enabled tracks in soundcheck.json.
# Record 60–90 seconds of full-band rehearsal to individual RAW input tracks.
# Stop recording and save the set before running calibration.
wcfi soundcheck calibrate soundcheck.json --recordings "C:\path\Samples\Recorded" --output soundcheck-run

# Read soundcheck-run/report.html, then write a separate set beside its source.
wcfi soundcheck apply soundcheck-run/plan.json --output "C:\path\Service-calibrated.als"
```

To create the copy in the calibration command, add `--write-set "C:\path\Service-calibrated.als"`.
Recording discovery defaults to files modified **today** and rejects ambiguous takes; `--latest`
explicitly picks the latest matching take for each channel, and `--recorded-on YYYY-MM-DD`
selects a different date. Exact `file` paths in the profile take precedence over discovery.
Each run needs a new results directory. No command changes the open Ableton session.

## Install

```bash
pipx install .           # from a clone, or:
pip install -e ".[dev]"  # editable, for development
```

On Windows, if this checkout is already installed in `.venv` and PowerShell says
`wcfi` is not recognized, install its per-user launcher:

```powershell
& 'C:\dev\wcfi-tools\scripts\install-cli.ps1'
```

This adds `%USERPROFILE%\.local\bin\wcfi.cmd` to your user PATH and uses the
checkout's existing environment. Restart an already-open terminal afterward.
For immediate use from any folder, the full executable path also works:

```powershell
& 'C:\dev\wcfi-tools\.venv\Scripts\wcfi.exe' audio emulator launch
```

You also need **ffmpeg** and **ffprobe** on your `PATH` (used to chunk audio):

- Windows: `choco install ffmpeg` or `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Debian/Ubuntu: `sudo apt install ffmpeg`

## Setup

**Required first** — `wcfi meeting …` won't run until setup has completed once.

```bash
wcfi setup            # interactive: pick providers, paste keys (masked + validated)
wcfi setup --check    # verify configuration without changing anything (doctor mode)
```

## Summarize a meeting

Point it at a folder containing the meeting audio (`.m4a`, `.mp3`, `.wav`, …):

```bash
wcfi meeting summarize <meeting-folder>
```

Options:

```
--provider {openai|anthropic}   Summarization backend (default from `wcfi setup`)
--summary-model TEXT            Override the summarization model
--transcribe-model TEXT         Override the transcription model (OpenAI)
--chunk-minutes FLOAT           Audio chunk length (default 5)
--emit {md,paste,all}           Which artifacts to write (default all)
--force                         Rebuild cached chunks/transcripts/summaries
--dry-run                       Discover inputs & estimate chunks; no API calls
```

Outputs (written into the meeting folder):

- `minutes.md` — finalized minutes (headings, tables, bullets)
- `paste-block.txt` — plain, section-by-section text for pasting into a styled template
- `_work/` — cached transcripts and intermediate facts (resumable; re-runs don't re-bill)

## Listen to a rehearsal excerpt

With the `soundcheck` extra and FFmpeg installed, prepare a short mix or individual-track
excerpt and measure it locally:

```bash
wcfi soundcheck listen rehearsal-master.wav --start 60 --seconds 30 --offline --output listening-preview
```

Omit `--offline` and choose a new output directory to send the excerpt to OpenAI's audio model
for listening observations. This requires an OpenAI API key and incurs API usage. The report
separates fallible model observations from measured loudness/peaks; it never changes Ableton.
See [soundcheck documentation](docs/soundcheck.md#machine-listening-to-an-excerpt) for credentials,
artifacts, limits, and the listening-validation procedure.

## At-home audio interface emulator

The Windows x64 ASIO prototype exposes 32 inputs/32 outputs at 48 kHz without
JACK or a mixer. The Tauri desktop app controls each raw input and measures
returned DAW outputs. Real files, procedural sources and repeatable faults share
one local service; the same interface also runs in a browser.

```powershell
wcfi audio emulator launch       # start the emulator and open its desktop window
wcfi audio emulator start        # start headlessly; inputs initially paused
wcfi audio emulator launch --web # use the shared browser interface
wcfi audio emulator pause        # pause simulated inputs without stopping the device
wcfi audio emulator stop         # take the device offline; keep the UI available
```

The native driver and Tauri window require one-time builds; register the driver
once. Use **Listen** on an Analysis Node to hear its signal through the system
default output, with a separate listening volume. Automixing is not yet implemented.
Use **Load recorded take** in the app for synchronized real stems: the manifest
maps inputs automatically, and one shared loop range controls the entire take.
Loading and changing the range leave inputs paused.
See [setup, controls, headless operation and limitations](docs/audio-emulator.md).

Role-aware rehearsal analysis is available with `wcfi audio analyze profiles`,
`take`, `file`, `validate-take`, and passive `watch`. RED and KIT use different
analyses; local reference previews test proposed attenuation and experimental
HF-burst treatment without playing audio or changing Ableton. These are
experimental observations/proposals, not a validated automatic mixer.
See [role profiles, replay, continuous observations and controlled tests](docs/role-analysis.md).
The app’s **Rehearsal lab** shows RED/KIT findings and before/after auditions.
Raw-file studies are separate from current Ableton node captures. Fresh captures
can authorize an explicit gain reduction through a supported Compressor Output
Gain control; experimental HF treatment remains preview-only. No transport or
desktop control is used.

`wcfi audio master-benchmark prepare`, `run`, and `report` build and test a
Master evaluator using saved mixes, identical-audio controls and controlled
degradations. Audiobox inference is CPU-only, offline and limited to one worker;
results do not certify professional mix quality. See the
[Master evaluation benchmark](docs/master-evaluation.md).

`wcfi audio full-mix-benchmark prepare`, `run`, and `report` render full-band
candidates with room/hall ambience and test a blinded deterministic selector
against both saved references and deliberate faults across three sessions.
The first run failed its selection gate; the app withholds recommendations.
See the [full-mix benchmark and results](docs/full-mix-benchmark.md).

The follow-up `prepare-balance` suite adds source-balance vetoes that reference
similarity cannot override. Fresh controls passed 55/55 checks across the three
dates, detecting 33/33 deliberate balance faults without false alarms on 15
benign/null controls. Professional-quality judgment remains unvalidated; see
the [source-balance evaluator and paired results](docs/source-balance-evaluator.md).

`prepare-combined` reruns the complete controls with balance vetoes and a
source-aware hum check. The combined gate passed 112/112 checks and selected
experimental candidates on all three excerpts, with reference agreement and
separate six-second consistency checks. See the
[combined benchmark and selection results](docs/full-mix-combined.md).

`wcfi audio mix-preference prepare` and `train` fit a small MED preference ranker
with nested whole-song held-out testing and a separate ODAQ artifact ranker.
Training reads saved audio on one CPU at idle priority; it requests no new
listening judgments and does not control Ableton. Final weights remain
experimental. See [targeted mix-preference evaluation](docs/mix-preference.md).
The follow-up `prepare-v2` / `train-v2` study adds candidate-selection tuning,
reference-free artifact features, conservative source-grouped ODAQ tests and
three separately blinded final MED songs. See
[selection-aware evaluation and artifact checks](docs/mix-preference-v2.md).

`wcfi audio mix-preference prepare-source-aware`, `test-source-aware` and
`inspect-sources` add explicit source roles, measured contribution ratios and
song-held-out balance experiments. Human-rated annotation estimates and actual
stem controls are reported separately; neither is enabled for automatic edits.
See [source-aware evaluation](docs/source-aware-evaluator.md).

Joint source-balance calibration separates tonal measurements from balance
alarms, reports unsupported arrangements explicitly, and compares revised versus
legacy detectors on reserved recordings. See
[false-alarm diagnosis and calibration](docs/source-balance-calibration.md).

The versioned [shared-activity evaluator](docs/source-balance-activity.md) measures
all source roles over the same raw-supported frames and reports insufficient
overlap explicitly. It retains the previous models for comparison; automatic
mix edits remain disabled.

## Configuration & secrets

Precedence at runtime: **CLI flag → environment variable → `.env` → OS keyring → interactive prompt.**

- Non-secret config: `wcfi setup` writes a small TOML in your platform config dir.
- Secrets: stored in the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service).
  `.env` / environment variables are honored as a fallback for servers and CI.

## Architecture

```
wcfi (CLI, Typer)   ─►  wcfi_tools.core  (pipeline · providers · artifacts · config)
                   └─► wcfi_tools.audio service ─► native ASIO bridge
Tauri / browser UI ──► authenticated loopback API ─┘
```

The CLI is a thin shell; all logic lives in the importable core so other front-ends can reuse it.

## Roadmap

- **Speaker identification** — `wcfi meeting summarize` will ask whether you want speakers
  named. If yes, it plays short snippets and you annotate who is speaking; the transcript is
  then attributed by name (making attendance, movers, and seconders far more reliable).
- **`wcfi meeting enroll`** — capture/refresh per-person voiceprints from labeled samples so
  identification can carry across meetings.
- Extend the shared local audio service and desktop/browser interface with
  rehearsal scenarios and measured automix evaluation.

## License

MIT — see [LICENSE](LICENSE).
