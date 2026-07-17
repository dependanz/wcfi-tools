# wcfi-tools

A small toolkit for **Word Christian Fellowship International**, exposed as a CLI (`wcfi <command>`).
The engine is UI-agnostic so the same core can later back a web app or desktop app.

First tools:

- **`wcfi setup`** — one-time interactive setup for the API keys the other tools need (OpenAI for
  transcription; OpenAI or Anthropic/Claude for summarization). Keys are stored in your OS keyring.
- **`wcfi meeting summarize <folder>`** — turn a folder of meeting audio into a summarized set of
  copy-able minutes artifacts.

## Install

```bash
pipx install .           # from a clone, or:
pip install -e ".[dev]"  # editable, for development
```

You also need **ffmpeg** and **ffprobe** on your `PATH` (used to chunk audio):

- Windows: `choco install ffmpeg` or `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Debian/Ubuntu: `sudo apt install ffmpeg`

## Setup

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

- **Speaker identification** — `wcfi meeting summarize` will ask whether you want speakers
  named. If yes, it plays short snippets and you annotate who is speaking; the transcript is
  then attributed by name (making attendance, movers, and seconders far more reliable).
- **`wcfi meeting enroll`** — capture/refresh per-person voiceprints from labeled samples so
  identification can carry across meetings.
- Longer term, the same UI-agnostic core can back a web or desktop front-end.

## License

MIT — see [LICENSE](LICENSE).
