# wcfi-tools

A small toolkit for **Word Christian Fellowship International**, exposed as a CLI (`wcfi <command>`).
The engine is UI-agnostic so the same core can later back a web app or desktop app.

First tools:

- **`wcfi setup`** — one-time interactive setup for the API keys the other tools need (OpenAI for
  transcription; OpenAI or Anthropic/Claude for summarization). Keys are stored in your OS keyring.
- **`wcfi meeting summarize <folder>`** — turn a folder of meeting audio into a summarized set of
  copy-able minutes artifacts.

## Install

One command installs everything — CLI, summarization, and speaker separation:

```bash
pip install -e .          # editable; add ".[dev]" for the test/lint tools
```

It's **torch-free**: speaker separation runs on `sherpa-onnx` (ONNX Runtime), and its models
auto-download from public URLs on first use — **no account, token, or gated model**. You also need
**ffmpeg** and **ffprobe** on your `PATH` (used to chunk audio):

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
--identify / --no-identify      Identify speakers by name (prompts if unset)
```

Outputs (written into the meeting folder):

- `minutes.md` — finalized minutes (headings, tables, bullets)
- `paste-block.txt` — plain, section-by-section text for pasting into a styled template
- `_work/` — cached transcripts and intermediate facts (resumable; re-runs don't re-bill)

### Speaker identification (optional)

Put **names** on the voices. It's **enrollment-based**, so it stays accurate on long meetings. The
default engine needs no accounts, tokens, or gated models.

Register your board's voices once (opens a local web page — play a clip, type who it is, or mark
**Unsure**); the named voiceprints are saved on your machine:

```bash
wcfi meeting speakers register <meeting-folder>   # find + name the distinct voices
wcfi meeting speakers list                        # who's on file
wcfi meeting speakers remove "<name>"             # forget someone
```

Then `summarize --identify` (or answer its prompt) finds each meeting's voices, matches them to
your registered speakers, and asks you to name only the **new** ones — names flow into
**Attendance** and motion movers/seconders. Under the hood: `sherpa-onnx` offline diarization
(pyannote **segmentation** model as ONNX + speaker embeddings + clustering) → each speaker turn
re-embedded and matched to the nearest registered voiceprint (low-confidence → *Unsure*). All ONNX,
no torch and no gated models; voiceprints never leave your machine.

## Configuration & secrets

Precedence at runtime: **CLI flag → environment variable → `.env` → OS keyring → interactive prompt.**

- Non-secret config: `wcfi setup` writes a small TOML in your platform config dir.
- Secrets: stored in the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service).
  `.env` / environment variables are honored as a fallback for servers and CI.

## Architecture

```
wcfi (CLI, Typer)  ─┐
web (later)         ├─►  wcfi_tools.core  (pipeline · providers · artifacts · config)
desktop (later)     ─┘
```

The CLI is a thin shell; all logic lives in the importable core so other front-ends can reuse it.

## Roadmap

- **Speaker identification** — ✅ enrollment-based & torch-free: sherpa-onnx offline diarization
  (pyannote segmentation as ONNX + embeddings + clustering), non-gated. Next: word-level "who said
  what" in the transcript (not just the attendee list).
- **`--transcript`** — bring your own transcript (Otter / `.vtt` / `.srt`) and skip transcription.
- Longer term, the same UI-agnostic core (incl. the annotator) can back a hosted web/desktop app.

## License

MIT — see [LICENSE](LICENSE).
