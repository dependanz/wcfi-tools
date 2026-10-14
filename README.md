# wcfi-tools

A small toolkit for **Word Christian Fellowship International**, exposed as a CLI (`wcfi <command>`).
The engine is UI-agnostic so the same core can later back a web app or desktop app.

First tools:

- **`wcfi setup`** — one-time interactive setup for the API keys the other tools need (OpenAI for
  transcription; OpenAI or Anthropic/Claude for summarization). Keys are stored in your OS keyring.
- **`wcfi meeting summarize [folder]`** — review a meeting folder and settings interactively,
  then explicitly start audio/document processing and produce editable pageless minutes.
- **`wcfi create google-doc [artifact]`** — review an edited summary and explicitly create a
  pageless Google document, with local receipts and conservative retry recovery.
- **`wcfi wix media-list <private-output>`** — inventory Wix Media Manager metadata through its
  read-only Files API.
- **`wcfi fbmsg scan|index <facebook-export> ...`** — locally inventory a Facebook Messenger JSON
  export and produce attachment and task-candidate indexes for human review.
- **`wcfi inbox up|push|list|memory`** — a local semantic inbox: an agent sweeps Gmail, Messenger, etc.
  into it and you triage items in the browser as it fills. See [Semantic inbox](#semantic-inbox).

## Install

Install the base CLI, summarization, and speaker separation from this checkout:

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

**Required before processing** — run setup once. `meeting summarize --dry-run` can inspect
local configuration and metadata without setup or API keys.

```bash
wcfi setup            # interactive: pick providers, paste keys (masked + validated)
wcfi setup --check    # verify configuration without changing anything (doctor mode)
```

## Summarize a meeting

Run the installed command in your Python environment:

```bash
wcfi meeting summarize
```

It asks for the folder, lists its files, requires explicit include/exclude choices,
asks for missing required settings, and previews exactly what will be processed.
The provider labels distinguish **audio transcription** from **text summarization**.

You can also supply the folder and any overrides:

```bash
wcfi meeting summarize <meeting-folder>
wcfi meeting summarize <meeting-folder> --meeting-date 2026-10-11 --no-identify
wcfi meeting summarize <meeting-folder> --dry-run
```

The command loads only **meeting.wcfi.toml** directly in that folder. It offers to
save your reviewed choices there. A generic `meeting.toml` is ignored. Explicit
options override meeting config, which overrides the last successful processing
settings, then setup defaults. False flags such as `--no-identify` are preserved.

Confirmation prompts show **"yes"/"no"** and **"start"/"cancel"**. Type the word
without quotes. Enter, `y`, `n`, EOF, and a complete config never authorize processing.
A real interactive terminal is required to start; use `--dry-run` for read-only
inspection in scripts. Dry-run inventories metadata and unresolved settings without
probing private content, saving files, or calling providers.

A small config example (replace the model ids with ones available to your account):

```toml
version = 1

[meeting]
date = 2026-10-11

[processing.transcription]
provider = "openai"
model = "REPLACE_TRANSCRIPTION_MODEL"
chunk_minutes = 5

[processing.summarization]
provider = "openai"
model = "REPLACE_SUMMARY_MODEL"

[processing.speakers]
identify = false

[output]
directory = "summary"
template = "meeting_minutes_pageless"
template_version = 2
layout = "pageless"

[[recordings]]
id = "audio_a"
path = "recording.m4a"
order = 1
```

Use multiple `[[recordings]]` entries with consecutive `order` values. Optional
`start_seconds`, `end_seconds`, and `overlap_group` declare selected ranges and
known overlapping coverage. Duplicate or declared overlapping recordings require
choosing one copy or trimming disjoint ranges. Unlisted files are never processed.
Credentials do not belong in the meeting config.

Available overrides include `--provider` / `--summary-provider`,
`--summary-model`, `--transcription-provider`, `--transcribe-model`,
`--chunk-minutes`, `--meeting-date`, `--meeting-time`, `--title`,
`--location`, `--prepared-by`, `--output`, `--identify/--no-identify`,
and `--speakers`. Use `--emit md` for Markdown only or `--emit all` for
Markdown plus plain text. `--force` rebuilds caches after the same explicit review;
it never bypasses approval or output-overwrite confirmation.

Outputs go in `summary/` by default, or the explicitly selected private directory:

- `SUMMARY.wcfi.md`: editable minutes with versioned formatting metadata selecting
  the continuous pageless template; no printed-page limit.
- `paste-block.txt`: optional plain text.
- `source-index.md` and `review-needed.md`: local evidence pointers and review notes.
- `_work/<fingerprint>/`: transcripts and intermediate evidence keyed to source bytes,
  selected ranges, config, and prompt/pipeline version. Unchanged stages can resume;
  final minutes generation still calls the summarizer on a rerun.

Existing artifact edits require explicit replacement confirmation. Changed inputs
or outputs after preview stop execution. Last-successful settings are saved only
after successful output; meeting paths, dates, people, speaker counts, and approvals
are excluded from reuse. Private outputs are refused inside Git worktrees.

**Current implementation boundary:** selected audio and PDF/DOCX/TXT/Markdown
supporting documents are implemented and tested offline, including document-only
meetings. See Supporting documents below for installation and evidence policy.
Image OCR, visual analysis and selected video frames still report an unsupported
adapter. Strict summary validation and `wcfi create google-doc` are implemented and
verified offline. Template version 2 has also been checked through live Google import, native style readback and PDF inspection. The standalone CLI OAuth route still needs account setup and an end-to-end live run.

### Speaker identification (optional)

Put **names** on the voices. It's **enrollment-based**, so it stays accurate on long meetings. The
default engine needs no accounts, tokens, or gated models.

Register your board's voices once (opens a local web page — play a clip, type who it is, or mark
**Unsure**); the named voiceprints are saved on your machine:

```bash
wcfi meeting speakers register <meeting-folder>   # find + name the distinct voices
wcfi meeting speakers register <folder> -n 6      # pin the count when you know who's present
wcfi meeting speakers list                        # who's on file
wcfi meeting speakers remove "<name>"             # forget someone
```

`--speakers/-n` (also on `summarize`) pins how many people are present — recommended when you know
the attendance, since it's more reliable than auto-detecting the count.

Then `summarize --identify` finds each meeting's voices, matches them to
your registered speakers, and asks you to name only the **new** ones — names flow into
the summarization context after another explicit review. Voice recognition alone does not
establish attendance or identify motion movers/seconders. Under the hood: `sherpa-onnx` offline diarization
(pyannote **segmentation** model as ONNX + speaker embeddings + clustering) → each speaker turn
re-embedded and matched to the nearest registered voiceprint (low-confidence → *Unsure*). All ONNX,
no torch and no gated models; voiceprints never leave your machine.

## Create a pageless Google document

Review `SUMMARY.wcfi.md`, `source-index.md` and `review-needed.md` before creating the
document. Creation reads the current artifact without calling a summarization model.

```powershell
pip install -e ".[google]"
wcfi create google-doc <path-to-SUMMARY.wcfi.md> --dry-run
wcfi create google-doc <path-to-SUMMARY.wcfi.md> --dry-run --preview <new-private-path.html>
```

Offline preview needs no credentials. Validation checks YAML fields, eight sections,
three table schemas, supported Markdown, exact template/version and contained evidence
pointers. It rejects unsupported HTML, images, unsafe links and YAML aliases, and reports
missing sidecars. New summaries select template version 2: Inter 13 pt body, 24 pt
title, 16 pt section headings, a native cover requirement, native date chips, nested lists,
and gray table headers with horizontal rules. Tables have widths matching the reference
and repeat headers in PDF exports without splitting ordinary rows. After creation, use **Insert > Cover image > Choose from stock images** (or
**Upload from computer** for the reference cloud artwork). The current API adapter
does not create or verify native covers; it inserts no inline substitute and keeps
cover verification pending for editor review. Person, place, and file chips are not inferred
from names. Version 1 remains selectable for the earlier 11 pt text-only layout.
Content has no page-count limit; the local preview may use Arial if Inter is unavailable.

Formatting belongs to the installed renderer; metadata selects its version:

```yaml
presentation:
  template: meeting_minutes_pageless
  template_version: 2
  layout: pageless
```

The body supplies hierarchy: numbered attendance groups with nested names, and bold
discussion-topic bullets with separate nested points. Creation preserves that reviewed
wording. It applies paragraph styles before text styles, then verifies native content,
lists, fonts, date chips, the absence of inline substitutes, tables and pageless mode. A changed artifact
is a new creation operation; this command does not update an existing Google Doc.

For authorization, enable Google Drive and Google Docs APIs in your Google Cloud project.
Configure OAuth consent (add your account as a test user if in Testing), create a
**Desktop app** OAuth client, and download its JSON outside Git. See
[Google's setup guide](https://developers.google.com/workspace/docs/api/quickstart/python).

```powershell
wcfi setup google --client-secrets <path-to-desktop-client.json>
wcfi setup google --check
wcfi create google-doc <path-to-SUMMARY.wcfi.md>
```

Setup displays scopes and requires `"start"` before opening the authorization browser.
Tokens stay in the OS keyring; only the account email goes in non-secret config.
Scopes are `drive.file` (files created/authorized for this app) and
`drive.metadata.readonly` (inspect an existing destination and its permissions).
The metadata scope is broader than access to the created document; see
[Google's scope definitions](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
The CLI cannot reuse another app's connected credentials.

Creation prompts for missing information; optional `--account <email> --folder <folder-ID>`
selects the destination explicitly. It shows the body, local review notes, account,
inherited access and writes, then requires `"start"` or `"cancel"`. Enter accepts nothing.
Only rendered body text, tables, HTTPS links, title and formatting are uploaded.
Source indexes, review notes and recordings stay local. Sharing is never changed.

Receipts live beside the artifact in `.wcfi-google/`, outside Git. Intent is saved
before creation. After a timeout, rerun the same command with the unchanged artifact
and destination: it searches Drive for the operation and reads back an existing result.
It never restyles, overwrites, deletes or blindly recreates a recovered document.
Absent or ambiguous results stop for manual reconciliation. Keep the receipt; deleting
it does not resolve an uncertain outcome. Concurrent runs using copied artifacts on
different machines are not covered by the local lock.

New documents get native pageless mode and explicit styles guarded by revision IDs.
Readback checks text, tables, native mode and requested styles. Drafts remain
**needs-review**, with **visual: unverified**, until inspected in Google Docs. A PDF
export alone cannot prove native pageless mode. Failure retains and reports the draft.

For scanned PDFs, prepare a separately reviewed searchable copy. Keep its original
and provenance, select only one copy, and do not treat OCR confidence as accuracy.
A general OCR adapter is not included in this version.

## Inventory Wix media

The Wix integration is deliberately read-only. It calls only the Media Manager List Files endpoint
and writes the returned descriptors to a guarded local JSONL catalog:

```powershell
$env:WIX_ACCESS_TOKEN = "<short-lived token or API key>"
$env:WIX_SITE_ID = "<site id>"  # required for an account-level API key
wcfi wix doctor
wcfi wix media-list C:\dev\wcfi\HeritageArchive\wix
wcfi wix inspect C:\dev\wcfi\HeritageArchive\wix\media-catalog.jsonl
```

The credential is read from the environment so it does not appear in command history. The Wix
identity must have **Read Media Manager** permission. The command cannot upload, edit, delete,
restore, or publish anything.

## Index a Facebook Messenger export

Request **Download Your Information** from Facebook with Messages selected and JSON format, extract
it locally, then point `fbmsg` at the extracted root:

```powershell
wcfi fbmsg scan C:\path\to\facebook-export
wcfi fbmsg index C:\path\to\facebook-export C:\dev\wcfi\HeritageArchive\fbmsg
wcfi fbmsg index C:\path\to\facebook-export C:\dev\wcfi\HeritageArchive\anniversary -t WCFI -t anniversary
```

`scan` prints counts only. `index` creates:

- `inventory.json` — aggregate counts and provenance;
- `messages.jsonl` — normalized local messages;
- `attachments.jsonl` — attachment paths and presence checks without copying files; and
- `task-candidates.jsonl` / `task-candidates.md` — rule-based requests, obligations, actions,
  and deadline signals that still require human review.

Raw message text, participant names, and local attachment paths are private. Output is refused
inside any Git worktree, marked `.wcfi-private`, and protected by a deny-all `.gitignore`. The
tool does not call a model and cannot send, delete, or react to messages.

## Semantic inbox

A local web page that fills with the things you need to know, while an agent (Claude Code) reads a
source for you. You mark each item **Relevant**, **Settled** or **Not relevant** and add notes; the
agent reads your decisions back and drafts replies. Nothing is ever sent for you.

```powershell
wcfi inbox up --open         # start the receiver if it isn't running, open the page
wcfi inbox push batch.json   # what the agent does: JSON item(s) or JSON Lines, or '-' for stdin
wcfi inbox list --status relevant --json   # read decisions + notes back
wcfi inbox memory add "Not in the anniversary band" --scope wcfi
wcfi inbox cursor set gmail 2026-09-10T12:00:00Z
wcfi inbox status | open | down
```

The workflow is: (0) make sure the server is running, (1) have your agent read an
authorized source and push distilled items in small batches, (2) triage in the browser,
(3) have the agent prepare drafts from your notes. Any local agent skill used to run
that workflow is configured separately.

- Pushing the same `source` + `external_id` again refreshes the item but never overwrites your
  decision or note, so sweeps can be re-run.
- **Memory**: durable facts and preferences the agent reads before each sweep. You can edit them
  from the page's Memory button.
- The server binds to `127.0.0.1` only. Every API call needs a per-install token (the page has it
  embedded; the CLI reads it from the data dir), and non-localhost `Host` headers are refused.
- Data lives in your per-user data dir (`WCFI_INBOX_DIR` to override) inside a guarded private
  folder. It is refused inside any Git worktree.

## Configuration & secrets

Precedence at runtime: **CLI flag → environment variable → `.env` → OS keyring → interactive prompt.**

- Non-secret config: `wcfi setup` writes a small TOML in your platform config dir.
- Secrets: stored in the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service).
  `.env` / environment variables are honored as a fallback for servers and CI.
- Wix uses `WIX_ACCESS_TOKEN` and optional `WIX_SITE_ID` environment variables; never put either
  value in source, documentation, devbrain, or shell transcripts.

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

## Supporting documents

The command stays `wcfi meeting summarize <meeting_folder>`. Install the optional
readers into the same environment. Until featx is published, use its checked-out source
(adjust the path on another device):

```powershell
python -m pip install -e "C:/dev/projects/featx[documents]"
python -m pip install -e ".[documents]"
wcfi meeting summarize "C:/path/to/meeting"
```

Select documents interactively or list them in `meeting.wcfi.toml`:

```toml
[[documents]]
id = "binder"
path = "board-binder.pdf"
role = "reference"

[[documents]]
id = "notes"
path = "meeting-notes.docx"
role = "supporting_notes"
```

`featx.documents.extract` reads locally after material review. WCFI shows extraction
limitations and requires an explicit `"yes"` to accept them. The final `"start"` approves
the displayed transfer of extracted text/table cells, filenames, roles and source
locations to the summary provider. Original document binaries are not uploaded.
`--dry-run` inspects metadata only. Document-only meetings do not use ffmpeg or a transcriber.

`agenda`, `previous_minutes` and `reference` remain proposed, historical or background
evidence. `supporting_notes` and `transcript` can support current events, but still
undergo scope/uncertainty classification. Every extracted fact must quote its source
unit exactly. Matching facts retain source references; conflicting facts retain both
versions and are flagged. Context and uncertain facts remain in evidence/review files
and are withheld from the minutes-writing model. This conservative policy can omit
useful material: review `review-needed.md` and `_work/.../atomic_summary.json` before
distributing minutes. Quote checks verify source linkage, not semantic correctness;
the LLM stages are not deterministic.

PDFs retain page text, not reconstructed table cells. DOCX retains ordered body
paragraphs/native table rows. Scanned/empty PDF pages, encrypted PDFs, unreadable files
and extraction failures stop the run; OCR is not implemented. Visual/non-body content
needs explicit review, and complex Word constructs may require flattening first.
Reader defaults cap inputs/expanded DOCX data at 64 MiB, text at two million characters,
and PDFs at 1,000 pages. WCFI caps selected document/combined evidence text at 500,000
characters, divides sources into 8,000-character units, and rejects conflict-review
payloads above 180,000 characters. Nothing is silently truncated; a later-stage limit
can stop a run after some model calls. These are operational limits, not a sandbox for
hostile files. Interpretation across source-unit boundaries needs review.

Document caches include source bytes, roles, extractor/backend identity, prompts and
provider/model settings. Invalid cached fact JSON is rebuilt; final minutes are
regenerated on reruns. The local `_work` evidence files contain private source text.
