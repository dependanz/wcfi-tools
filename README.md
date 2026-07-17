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

For **speaker attribution**, install the (heavy, torch-based) extra and get a free HuggingFace
token, then accept the terms for [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1):

```bash
pip install "wcfi-tools[speaker]"
```

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
--identify-speakers             Attribute speakers by name (prompts if unset)
--diarizer {pyannote|window}    Diarization backend (window = dev stub, no token)
```

Outputs (written into the meeting folder):

- `minutes.md` — finalized minutes (headings, tables, bullets)
- `paste-block.txt` — plain, section-by-section text for pasting into a styled template
- `_work/` — cached transcripts and intermediate facts (resumable; re-runs don't re-bill)

### Speaker attribution (prototype)

`summarize` can put **names** on the people in the meeting. If you opt in (it prompts, or pass
`--identify-speakers`), it:

1. **diarizes** the audio into distinct voices (via `pyannote.audio`),
2. cuts a few clips per voice and opens a **local web page** in your browser,
3. lets you play each clip and say **who is speaking** (or mark **Unsure**),
4. feeds the resulting roster into the minutes — so **Attendance** and motion movers/seconders
   are accurate instead of guessed.

Needs the speaker extra and a (free) HuggingFace token — see Install and `wcfi setup`. For a quick
UI test without either, use the dev stub: `--identify-speakers --diarizer window`.

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

- **Speaker identification** — ✅ prototype landed (diarize → local web annotator → named minutes).
  Next: robustness on real multi-speaker room audio, and word-level "who said what".
- **`wcfi meeting enroll`** — capture/refresh per-person voiceprints from the labeled snippets so
  future meetings auto-match known voices and only ask about new/unsure ones.
- Longer term, the same UI-agnostic core (incl. the annotator) can back a hosted web/desktop app.

## License

MIT — see [LICENSE](LICENSE).
