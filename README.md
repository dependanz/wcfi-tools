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

## Speaker identification (local, optional)

Runs entirely on your machine with [pyannote.audio](https://github.com/pyannote/pyannote-audio) —
no meeting audio leaves the device. It diarizes ("who spoke when"), then matches each anonymous
voice against voiceprints you've enrolled before, so **returning speakers are named automatically
and you only label the new or uncertain ones**. Confirmed voices are remembered for next time.

Install the extra and enable it in setup:

```bash
pip install 'wcfi-tools[speakers]'
wcfi setup            # answer "yes" to speaker identification, paste a Hugging Face token
```

pyannote's models are **gated** on Hugging Face. A token alone isn't enough — a human has to accept
each model's license once (this can't be automated; it's Hugging Face's terms). `wcfi setup` prints
the exact links to click; after that the models cache locally and you're done.

Walk through a meeting's speakers:

```bash
wcfi meeting identify <meeting-folder>
```

It plays a few snippets per voice (via `ffplay` if present) and asks "who is speaking?" — accepting
any auto-matches from your voiceprint database. Results are written to `speakers.json` and
`_work/diarization/`.

Manage the voiceprint database:

```bash
wcfi speakers list                 # who's enrolled, and how many samples each
wcfi speakers rename "Old" "New"
wcfi speakers forget "Name"
wcfi speakers clear
```

> Voiceprints are biometric data. They stay on your machine, `wcfi speakers forget` removes them,
> and you should disclose their use to the people being recorded.

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

- **Speaker-attributed minutes** — feed the `wcfi meeting identify` results into
  `wcfi meeting summarize` so the transcript (and therefore attendance, movers, and seconders) is
  labeled by name automatically.
- **Browser walk-through** — a local web app front-end over the same speaker-ID core (the CLI
  walk-through in `wcfi meeting identify` is the first front-end).
- **LLM name proposals** — use conversational cues in the transcript ("Thank you, Pastor Jun") to
  pre-fill guesses for unmatched voices, on top of the acoustic voiceprint match.
- Longer term, the same UI-agnostic core can back a web or desktop front-end for the whole tool.

## License

MIT — see [LICENSE](LICENSE).
