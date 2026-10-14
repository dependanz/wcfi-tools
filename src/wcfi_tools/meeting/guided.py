"""Guided intake for the audio and document adapters. Every network operation follows explicit start."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import typer

from .. import config as app_config
from ..core import ffmpeg
from ..providers import build_summarizer, build_transcriber
from . import configuration as mc
from . import evidence, pipeline, schemas, summary_artifact
from .pipeline import summarize_meeting

LAST_NAME = "last-meeting.json"
CACHE_VERSION = 2
OUTPUT_FILES = ("SUMMARY.wcfi.md", "paste-block.txt", "source-index.md", "review-needed.md")


def say(text: str) -> None:
    # Plain output: filenames and model/user text are never interpreted as Rich markup.
    typer.echo(text)


def interactive() -> bool:
    return sys.stdin.isatty()


def ask(prompt: str, default: str | None = None) -> str:
    if not interactive():
        raise mc.ConfigError("Interactive terminal required; use --dry-run for offline inspection.")
    try:
        return typer.prompt(prompt, default=default, show_default=bool(default)).strip()
    except (EOFError, KeyboardInterrupt, typer.Abort):
        raise typer.Abort() from None


def word(prompt: str, choices: tuple[str, ...] = ("yes", "no")) -> str:
    while True:
        reply = ask(prompt, "").casefold()
        if reply in choices:
            return reply
        say("Type exactly " + " or ".join(json.dumps(x) for x in choices) + "; Enter accepts nothing.")


def setup_defaults(config: dict) -> dict:
    provider = config["providers"]["summarizer"]
    return {
        "processing": {
            "summarization": {"provider": provider,
                              "model": config["models"].get(f"{provider}_summary", "")},
            "transcription": {"provider": config["providers"].get("transcriber", "openai"),
                              "model": config["models"]["transcribe"]},
        },
    }


def last_path() -> Path:
    return app_config.config_dir() / LAST_NAME


def load_last() -> dict:
    path = last_path()
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        mc.validate({"version": 1, **result})
        return mc.reusable(result)
    except (ValueError, OSError, TypeError) as exc:
        raise mc.ConfigError(f"Invalid saved meeting settings at {path}: {exc}") from exc


def show_inventory(items: list[mc.InventoryItem]) -> None:
    say("\nMeeting materials:")
    for item in items:
        say(f"  [{item.status}] {item.path} ({item.kind})"
            + (f" - {item.reason}" if item.reason else ""))


def show_settings(data: dict, origins: dict) -> None:
    say("\nEffective settings (transcription = audio to text; summarization = text to minutes):")

    def walk(value: dict, prefix: str = "") -> None:
        for key, v in value.items():
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(v, dict):
                walk(v, field)
            elif key not in mc.MATERIALS and key not in {"excluded", "version"}:
                say(f"  {field}: {v} [{origins.get(field, 'reviewed answer')}]")
    walk(data)


def choose_materials(root: Path, data: dict) -> None:
    # User requested selection changes; regenerate explicit selections from the visible inventory.
    for key in (*mc.MATERIALS, "excluded"):
        data[key] = []
    items = mc.inventory(root, data)
    for item in items:
        if item.status == "builtin":
            continue
        if item.kind == "unsupported":
            if word(f'Exclude unsupported file "{item.path}"? Type "yes" or "no"') != "yes":
                raise mc.ConfigError("Unsupported file left unassigned; no processing started.")
            data["excluded"].append({"path": item.path, "reason": "Unsupported file excluded in review"})
            continue
        if word(f'Include "{item.path}" ({item.kind})? Type "yes" or "no"') == "no":
            data["excluded"].append({"path": item.path, "reason": "Excluded during material review"})
            continue
        kind = item.kind
        entry: dict[str, Any] = {"id": f"{kind}_{len(data[kind]) + 1}", "path": item.path}
        if kind == "recordings":
            entry["order"] = len(data[kind]) + 1
        elif kind == "documents":
            entry["role"] = ask("Document role (" + ", ".join(mc.ROLES) + ")", "reference")
        elif kind == "images":
            entry["use"] = ask("Image use (extract_text or analyze_visual)", "extract_text")
        data[kind].append(entry)
    mc.validate(data)


def review_materials(root: Path, data: dict) -> None:
    while True:
        items = mc.inventory(root, data)
        show_inventory(items)
        for r in sorted(data.get("recordings", []), key=lambda x: x["order"]):
            say(f"  Recording order {r['order']}: {r['path']}, "
                f"{r.get('start_seconds', 0)} to {r.get('end_seconds', 'end')} seconds"
                f", overlap group: {r.get('overlap_group', 'not declared')}")
        for document in data.get("documents", []):
            say(f"  Document: {document['path']}, role: {document['role']}")
        unresolved = [x for x in items if x.status in {"unassigned", "unsupported"}]
        if unresolved:
            say("Every file must be explicitly selected or excluded.")
            choose_materials(root, data)
            continue
        if not any(data.get(k) for k in mc.MATERIALS):
            raise mc.ConfigError("No selected sources; add meeting materials and try again.")
        say("Review recording order and overlapping coverage. To trim ranges, edit meeting.wcfi.toml.")
        if word('Are these selections, order, and overlap choices correct? Type "yes" or "no"') == "yes":
            return
        choose_materials(root, data)


def field_value(data: dict, field: str) -> Any:
    cur: Any = data
    for part in field.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def set_value(data: dict, field: str, value: Any) -> None:
    parts = field.split(".")
    target = data
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def prompt_settings(data: dict, origins: dict, config: dict, *, edit: bool) -> None:
    for field in ("meeting.date", "output.directory",
                  "processing.transcription.provider", "processing.transcription.model",
                  "processing.transcription.chunk_minutes",
                  "processing.summarization.provider", "processing.summarization.model"):
        if ".transcription." in field and not data.get("recordings"):
            continue
        current = field_value(data, field)
        if origins.get(field) == "command line" or (not edit and current is not None):
            continue
        while True:
            raw = ask(field + (' (YYYY-MM-DD or "unknown")' if field == "meeting.date" else ""),
                      str(current) if current is not None else None)
            try:
                value: Any = raw
                if field == "meeting.date" and raw != "unknown":
                    value = date.fromisoformat(raw)
                if field.endswith("chunk_minutes"):
                    value = float(raw)
                candidate = copy.deepcopy(data)
                if field.endswith(".provider") and raw != current:
                    parent = field.rsplit(".", 1)[0]
                    model_field = parent + ".model"
                    if origins.get(model_field) != "command line":
                        if parent.endswith("summarization"):
                            default = config["models"].get(f"{raw}_summary")
                        else:
                            default = config["models"].get("transcribe") if raw == "openai" else None
                        candidate[parent.split(".")[0]][parent.split(".")[1]].pop("model", None)
                        if default:
                            set_value(candidate, model_field, default)
                        origins[model_field] = "reviewed answer"
                set_value(candidate, field, value)
                mc.validate(candidate)
                data.clear()
                data.update(candidate)
                origins[field] = "reviewed answer"
                break
            except (ValueError, TypeError) as exc:
                say(str(exc))


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def preflight(root: Path, data: dict) -> tuple[list[dict], str]:
    """After material review: probe selected content and bind the preview to exact bytes/settings."""
    missing = mc.missing_fields(data)
    if missing:
        raise mc.ConfigError("Required values: " + ", ".join(missing))
    for kind in ("images", "videos"):
        if data.get(kind):
            raise mc.ConfigError(f"Selected {kind} processing is not implemented yet. "
                                 "Keep the material listed; exclude it explicitly or wait for its adapter.")
    p = data["processing"]
    if data.get("recordings") and p["transcription"]["provider"] != "openai":
        raise mc.ConfigError("The installed transcription adapter supports openai only.")
    if p["summarization"]["provider"] not in {"openai", "anthropic"}:
        raise mc.ConfigError("Summarization provider must be openai or anthropic.")
    if data.get("recordings"):
        ffmpeg.require_tool("ffmpeg")
        ffmpeg.require_tool("ffprobe")
    records = []
    for item in sorted(data.get("recordings", []), key=lambda x: x["order"]):
        path = mc.resolve_input(root, item["path"])
        duration = ffmpeg.ffprobe_audio_duration(path)
        start, end = item.get("start_seconds", 0), item.get("end_seconds", duration)
        if not 0 <= start < end <= duration:
            raise mc.ConfigError(f"{item['path']}: range must satisfy 0 <= start < end <= {duration}")
        records.append({**item, "start_seconds": start, "end_seconds": end,
                        "sha256": file_hash(path)})
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault("hash:" + record["sha256"], []).append(record)
        if record.get("overlap_group"):
            groups.setdefault("group:" + record["overlap_group"], []).append(record)
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda x: x["start_seconds"])
        if any(a["end_seconds"] > b["start_seconds"] for a, b in zip(ordered, ordered[1:])):
            raise mc.ConfigError("Duplicate/overlapping recordings: choose one copy or trim "
                                 "nonoverlapping ranges in meeting.wcfi.toml.")
    document_identity = []
    if data.get("documents"):
        from importlib.metadata import version
        from featx import documents as readers
        for item in data["documents"]:
            suffix = Path(item["path"]).suffix.lower()
            backend = {".pdf": "pypdf", ".docx": "python-docx"}.get(suffix)
            document_identity.append({**item, "sha256": file_hash(mc.resolve_input(root, item["path"])),
                                      "reader": file_hash(Path(readers.__file__)),
                                      "backend": version(backend) if backend else "utf8/1"})
    stamp = json.dumps({"version": CACHE_VERSION, "config": data, "sources": records,
                        "documents": document_identity, "evidence": file_hash(Path(evidence.__file__)),
                        "artifact_validator": file_hash(Path(summary_artifact.__file__)),
                        "prompts": file_hash(Path(schemas.__file__)),
                        "pipeline": file_hash(Path(pipeline.__file__))},
                       sort_keys=True, default=str)
    return records, hashlib.sha256(stamp.encode()).hexdigest()


def write_artifact(output: Path, data: dict, result: Any) -> None:
    metadata = data["meeting"]
    title = metadata.get("title", f"Board Meeting - {metadata['date']}")
    presentation = data["output"]
    front = [
        "---", "wcfi_version: 1", "kind: meeting_summary",
        "title: " + json.dumps(title, ensure_ascii=False),
        "meeting_date: " + json.dumps(str(metadata["date"])),
        "presentation:", "  template: " + presentation["template"],
        f"  template_version: {presentation['template_version']}", "  layout: pageless",
        "evidence:", '  source_index: "source-index.md"', '  review_notes: "review-needed.md"',
        "---", "",
    ]
    artifact_text = "\n".join(front) + result.minutes_markdown.rstrip() + "\n"
    summary_artifact.parse_text(artifact_text, output / "SUMMARY.wcfi.md")
    mc.atomic_write(output / "SUMMARY.wcfi.md", artifact_text)
    rel = result.work_dir.relative_to(output).as_posix()
    mc.atomic_write(output / "source-index.md",
                    "# Source index\n\n"
                    f"- Chunk identities and original recording/time ranges: {rel}/chunks_manifest.json\n"
                    f"- Full transcript: {rel}/transcript.md\n"
                    f"- Consolidated evidence: {rel}/atomic_summary.json\n")
    sources = getattr(result, "sources", [])
    if sources:
        lines = ["# Source index", "", f"- Full extracted documents: {rel}/documents.json",
                 f"- Source units and text: {rel}/sources.json",
                 f"- Attributable facts and conflict flags: {rel}/atomic_summary.json", ""]
        lines += [f"- {x['id']}: {x['path']} ({x['role']}; {x['locator']})" for x in sources]
        mc.atomic_write(output / "source-index.md", "\n".join(lines) + "\n")
    uncertain = [i for i in result.atomic_summary.get("items", [])
                 if i.get("uncertain") or i.get("category") == "unresolved questions"
                 or i.get("scope", "current_meeting") != "current_meeting"]
    notes = ["# Review needed", "", "Review source evidence, attendance, motion attribution, "
             "and all proposed actions before using these minutes.", "",
             "Automated flags are incomplete; their absence does not establish certainty.", ""]
    notes += ["Context, historical/proposed events and disputed facts are withheld from current-meeting minutes.", ""] if sources else []
    notes += [f"- {x.get('id', '')} [{x.get('scope', 'current_meeting')}] {x.get('claim', '')} {x.get('notes', '')}".strip() for x in uncertain]
    notes += ["- " + note for note in getattr(result, "review_notes", [])]
    mc.atomic_write(output / "review-needed.md", "\n".join(notes).rstrip() + "\n")


def run(folder: Path | None, overrides: dict, *, dry_run: bool = False,
        force: bool = False) -> None:
    if folder is None:
        if dry_run:
            raise mc.ConfigError("Supply the meeting folder for --dry-run.")
        folder = Path(ask("Local meeting folder"))
    root = folder.expanduser().resolve()
    if not root.is_dir():
        raise mc.ConfigError(f"Meeting folder does not exist: {root}")
    raw = mc.load(root)
    original = (root / mc.CONFIG_NAME).read_bytes() if (root / mc.CONFIG_NAME).exists() else None
    config = app_config.load_config()
    last = load_last()
    data, origins = mc.resolve(raw, setup=setup_defaults(config), last=last, overrides=overrides)
    if dry_run:
        show_inventory(mc.inventory(root, data))
        show_settings(data, origins)
        missing = mc.missing_fields(data)
        if missing:
            say("Still required: " + ", ".join(missing))
        say("Dry run only: no prompts, content reads, config writes, or API calls.")
        return
    if not interactive():
        raise mc.ConfigError("Interactive terminal required for explicit review and start; "
                             "use --dry-run for offline inspection.")
    review_materials(root, data)
    prompt_settings(data, origins, config, edit=False)
    while True:
        show_settings(data, origins)
        prompt = ('Use the settings from last summarization (with displayed overrides)?'
                  if last else 'Use these configured defaults (with displayed overrides)?')
        if word(prompt + ' Type "yes" or "no"') == "yes":
            break
        prompt_settings(data, origins, config, edit=True)
    documents = evidence.prepare(root, data.get("documents", []))
    for item in documents:
        selection, doc = item["selection"], item["document"]
        say(f"  Read {selection['path']}: {len(doc['blocks'])} blocks, role {selection['role']}")
        for issue in doc["issues"]:
            say(f"  Review {issue['locator']}: {issue['message']}")
        if file_hash(mc.resolve_input(root, selection["path"])) != doc["sha256"]:
            raise mc.ConfigError("Document changed while reading; review again.")
    if any(item["document"]["issues"] for item in documents):
        if word('Accept these extraction limitations for this run? Type "yes" or "no"') != "yes":
            say("Cancelled; resolve the document limitations before processing.")
            return
    for item in documents:
        if file_hash(mc.resolve_input(root, item["selection"]["path"])) != item["document"]["sha256"]:
            raise mc.ConfigError("Document changed during extraction review; run again.")
    records, stamp = preflight(root, data)
    output = mc.output_path(root, data)
    say("\nExecution preview:")
    for r in records:
        say(f"  Audio: {r['path']} [{r['start_seconds']}..{r['end_seconds']} seconds]"
            f" SHA256 {r['sha256'][:12]}")
    p = data["processing"]
    if records:
        say(f"  Audio chunks -> {p['transcription']['provider']} / {p['transcription']['model']}")
    else:
        say("  No audio selected: transcription and speaker processing are unused.")
    if documents:
        say("  Extracted document text/table cells, filenames, roles and source locations -> summarizer. "
            "Original document files and images are not uploaded.")
    say(f"  Transcripts, source filenames/times, meeting metadata and evidence -> "
        f"{p['summarization']['provider']} / {p['summarization']['model']}")
    identify = bool(records) and p.get("speakers", {}).get("identify", False)
    if identify:
        say("  Speaker identification: local voice processing; missing public ONNX models may "
            "download. Reviewed speaker names are included in summarization context.")
    say("  Paid API use. Provider/account retention rules apply; verify your account's terms.")
    say(f"  Output: {output} (pageless Markdown, evidence and optional plain text)")
    say(f"  Cache: {output / '_work' / stamp}; "
        + ("rebuild requested" if force else "reuse matching cached stages; final minutes regenerated"))
    work_root = output / "_work" / stamp
    if not work_root.resolve().is_relative_to(output):
        raise mc.ConfigError("Cache path escapes the reviewed output directory.")
    for cache_dir in (output / "_work", work_root):
        if cache_dir.exists() and not cache_dir.is_dir():
            raise mc.ConfigError(f"Cache directory is occupied by a file: {cache_dir}")
    if work_root.exists():
        for cached in work_root.rglob("*"):
            if cached.is_symlink() or not cached.resolve().is_relative_to(output):
                raise mc.ConfigError("Cache contains a redirected path; choose a fresh --output.")
    existing = [x for x in OUTPUT_FILES if (output / x).exists()]
    for name in OUTPUT_FILES:
        path = output / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise mc.ConfigError(f"Output must be a regular file: {path}")
    if existing:
        say("  Existing generated files to replace: " + ", ".join(existing))
        if word('Replace these files, including any edits? Type "yes" or "no"') != "yes":
            say("Cancelled; use --output with a new directory to preserve both versions.")
            return
    current_config = (root / mc.CONFIG_NAME).read_bytes() if (root / mc.CONFIG_NAME).exists() else None
    if current_config != original:
        raise mc.ConfigError("Config changed during review; run again for a fresh preview.")
    if mc.dump(raw) != mc.dump(data):
        if word(f'Save reviewed settings and selections to {mc.CONFIG_NAME}? Type "yes" or "no"') == "yes":
            mc.atomic_write(root / mc.CONFIG_NAME, mc.dump(data))
    config_snapshot = (root / mc.CONFIG_NAME).read_bytes() if (root / mc.CONFIG_NAME).exists() else None
    inventory_snapshot = [x for x in mc.inventory(root, data) if x.status != "builtin"]
    output_snapshot = {x: file_hash(output / x) if (output / x).is_file() else None
                       for x in OUTPUT_FILES}
    if word('Type "start" to begin processing, or "cancel" to exit', ("start", "cancel")) == "cancel":
        say("Cancelled; no provider calls made.")
        return
    # Approval is tied to reviewed selections/settings/content, not merely filenames.
    if [x for x in mc.inventory(root, data) if x.status != "builtin"] != inventory_snapshot:
        raise mc.ConfigError("Inventory changed; review again.")
    after = (root / mc.CONFIG_NAME).read_bytes() if (root / mc.CONFIG_NAME).exists() else None
    if after != config_snapshot or preflight(root, data)[1] != stamp:
        raise mc.ConfigError("Inputs/config changed after preview; run again to review.")
    for x, digest in output_snapshot.items():
        path = output / x
        if (path.is_symlink() or (path.exists() and not path.is_file()) or
                (file_hash(path) if path.is_file() else None) != digest):
            raise mc.ConfigError("Output changed after preview; run again to review.")
    mc.output_path(root, data)
    roster, roster_note = {}, ""
    if identify:
        from ..commands import speakers
        speaker_work = output / "_work" / stamp / "speaker-review"
        chunks = pipeline.build_chunk_records(
            root, [mc.resolve_input(root, r["path"]) for r in records], speaker_work,
            p["transcription"]["chunk_minutes"], None,
            {mc.resolve_input(root, r["path"]): (r["start_seconds"], r["end_seconds"])
             for r in records})
        pipeline.ensure_audio_chunks(chunks, force, None)
        roster, _ = speakers.identify_present(
            [Path(chunk.chunk_path) for chunk in chunks], speaker_work,
            num_speakers=p.get("speakers", {}).get("count", 0))
        roster_note = ("Reviewed voice labels: " + ", ".join(roster) +
                       ". Voice recognition does not establish attendance or motion attribution; "
                       "only explicit evidence establishes those facts.")
        say("Speaker context to send: " + roster_note)
        if word('Type "start" to use these reviewed names, or "cancel" to exit',
                ("start", "cancel")) == "cancel":
            return
    if (preflight(root, data)[1] != stamp or mc.output_path(root, data) != output or
            [x for x in mc.inventory(root, data) if x.status != "builtin"] != inventory_snapshot):
        raise mc.ConfigError("Inputs/output changed during review; run again.")
    after = (root / mc.CONFIG_NAME).read_bytes() if (root / mc.CONFIG_NAME).exists() else None
    if after != config_snapshot:
        raise mc.ConfigError("Config changed during review; run again.")
    for name, digest in output_snapshot.items():
        path = output / name
        if (path.is_symlink() or (path.exists() and not path.is_file()) or
                (file_hash(path) if path.is_file() else None) != digest):
            raise mc.ConfigError("Output changed during review; run again.")
    summarizer = build_summarizer(config, provider=p["summarization"]["provider"],
                                 model=p["summarization"]["model"])
    transcriber = None
    if records:
        config["providers"]["transcriber"] = p["transcription"]["provider"]
        transcriber = build_transcriber(config, model=p["transcription"]["model"])
    roster_stamp = hashlib.sha256(json.dumps([roster, roster_note], sort_keys=True).encode()).hexdigest()[:12]
    result = summarize_meeting(
        root, summarizer=summarizer, transcriber=transcriber, documents=documents,
        audio_files=[mc.resolve_input(root, r["path"]) for r in records],
        recording_ranges={mc.resolve_input(root, r["path"]): (r["start_seconds"], r["end_seconds"])
                          for r in records},
        output_dir=output, work_dir=output / "_work" / stamp / roster_stamp,
        meeting_date=str(data["meeting"]["date"]),
        meeting_time=data["meeting"].get("time", "Not captured"),
        prepared_by=data["meeting"].get("prepared_by", "Not captured"),
        location=data["meeting"].get("location", "Not captured"),
        chunk_minutes=p.get("transcription", {}).get("chunk_minutes", 5),
        emit=("paste",) if "plain_text" in data["output"]["formats"] else (),
        force=force, roster=roster, roster_note=roster_note)
    write_artifact(output, data, result)
    mc.atomic_write(last_path(), json.dumps(mc.reusable(data), indent=2) + "\n")
    say(f"Done. Review {output / 'SUMMARY.wcfi.md'} and {output / 'review-needed.md'}.")
