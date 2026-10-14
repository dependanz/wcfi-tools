"""Synthetic document-only/mixed meeting integration and evidence boundary tests."""
from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("featx.documents")
from wcfi_tools import config as app_config
from wcfi_tools.cli import app
from wcfi_tools.meeting import configuration as mc
from wcfi_tools.meeting import evidence, guided, pipeline

runner = CliRunner()
BODY = (Path(__file__).parent / "fixtures/meeting-summary.wcfi.md").read_text(encoding="utf-8").split("---", 2)[2].strip()


def fact(quote="Approved the budget.", **changes):
    return {"category": "decisions", "claim": quote, "owner": "Not captured", "due_date": "Not captured",
            "status": "approved", "confidence": 0.8, "uncertain": False, "notes": "",
            "quote": quote, "scope": "current_meeting", **changes}


class FakeSummarizer:
    model = "synthetic"
    def __init__(self, *, conflicts=False):
        self.calls = []
        self.conflicts = conflicts
    def structured_json(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["schema_name"] == "source_facts":
            unit = json.loads(kwargs["user"])["source"]
            return {"items": [fact(unit["text"])]}
        if kwargs["schema_name"] == "evidence_conflicts":
            return {"conflicts": [{"fact_ids": ["fact-1", "fact-2"], "reason": "Conflicting outcomes"}] if self.conflicts else []}
        return {"markdown": BODY}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "meeting"
    root.mkdir()
    (root / "notes.txt").write_text("Approved the budget.", encoding="utf-8")
    raw = {"version": 1, "meeting": {"date": date(2026, 10, 11)},
           "documents": [{"id": "notes", "path": "notes.txt", "role": "supporting_notes"}]}
    data = mc.resolve(raw, setup=guided.setup_defaults(app_config.DEFAULT_CONFIG))[0]
    (root / mc.CONFIG_NAME).write_text(mc.dump(data), encoding="utf-8")
    monkeypatch.setattr(app_config, "is_configured", lambda: True)
    monkeypatch.setattr(app_config, "load_config", lambda: copy.deepcopy(app_config.DEFAULT_CONFIG))
    monkeypatch.setattr(app_config, "config_dir", lambda: tmp_path / "prefs")
    monkeypatch.setattr(guided, "interactive", lambda: True)
    def no_audio(*a, **k):
        pytest.fail("Document-only run must not use audio tools/providers")
    monkeypatch.setattr(guided.ffmpeg, "require_tool", no_audio)
    monkeypatch.setattr(guided, "build_transcriber", no_audio)
    summary = FakeSummarizer()
    monkeypatch.setattr(guided, "build_summarizer", lambda *a, **k: summary)
    return root, data, summary


def run(root, answers="yes\nyes\nstart\n"):
    return runner.invoke(app, ["meeting", "summarize", str(root)], input=answers)


def test_document_only_and_content_bound_cache(configured):
    root, _, summary = configured
    first = run(root)
    assert first.exit_code == 0, first.output
    assert "No audio selected" in first.output
    assert "Original document files and images are not uploaded" in first.output
    assert [c["schema_name"] for c in summary.calls] == ["source_facts", "minutes_markdown"]
    index = (root / "summary/source-index.md").read_text()
    assert "notes.txt" in index and "lines 1-1" in index
    second = run(root, "yes\nyes\nyes\nstart\n")
    assert second.exit_code == 0, second.output
    assert [c["schema_name"] for c in summary.calls].count("source_facts") == 1
    (root / "notes.txt").write_text("Deferred the budget.")
    third = run(root, "yes\nyes\nyes\nstart\n")
    assert third.exit_code == 0, third.output
    assert [c["schema_name"] for c in summary.calls].count("source_facts") == 2


@pytest.mark.parametrize("answers", ["", "yes\nyes\ncancel\n"])
def test_cancel_before_provider_or_outputs(configured, answers):
    root, _, summary = configured
    result = run(root, answers)
    assert not summary.calls and not (root / "summary").exists()
    assert result.exit_code in (0, 1)


def test_unreadable_and_dry_run_never_call_provider(configured):
    root, _, summary = configured
    (root / "notes.txt").write_bytes(b"\x00binary")
    preview = runner.invoke(app, ["meeting", "summarize", str(root), "--dry-run"])
    assert preview.exit_code == 0 and "no prompts, content reads" in preview.output
    result = run(root)
    assert result.exit_code == 1 and "binary/control" in result.output
    assert not summary.calls and not (root / "summary").exists()


def test_document_changed_during_start_rejected(configured, monkeypatch):
    root, _, summary = configured
    word = guided.word
    def change(prompt, choices=("yes", "no")):
        reply = word(prompt, choices)
        if choices == ("start", "cancel"):
            (root / "notes.txt").write_text("Changed after preview")
        return reply
    monkeypatch.setattr(guided, "word", change)
    result = run(root)
    assert result.exit_code == 1 and "changed" in result.output
    assert not summary.calls


def test_extraction_warning_requires_explicit_yes(configured, monkeypatch):
    root, _, summary = configured
    prepare = evidence.prepare
    def warned(*args):
        values = prepare(*args)
        values[0]["document"]["issues"] = [{"code": "synthetic", "locator": "document", "message": "Visual content omitted"}]
        return values
    monkeypatch.setattr(evidence, "prepare", warned)
    result = run(root, "yes\nyes\n\nno\n")
    assert result.exit_code == 0 and "Enter accepts nothing" in result.output
    assert not summary.calls


@pytest.mark.parametrize("role,scope", [("reference", "background"), ("agenda", "proposal"), ("previous_minutes", "historical")])
def test_context_cannot_be_promoted_into_current_minutes(configured, role, scope):
    root, data, summary = configured
    data["documents"][0]["role"] = role
    (root / mc.CONFIG_NAME).write_text(mc.dump(data))
    result = run(root)
    assert result.exit_code == 0, result.output
    minutes_request = summary.calls[-1]["user"]
    assert "Approved the budget." not in minutes_request
    atomic = next((root / "summary/_work").rglob("atomic_summary.json"))
    assert json.loads(atomic.read_text())["items"][0]["scope"] == scope
    assert scope in (root / "summary/review-needed.md").read_text()


def test_mixed_audio_and_documents_conflicts_keep_both_sources(tmp_path, monkeypatch):
    root = tmp_path
    (root / "notes.txt").write_text("Deferred the budget.")
    audio = root / "audio.wav"
    audio.write_bytes(b"synthetic selected audio")
    document = evidence.prepare(root, [{"id": "notes", "path": "notes.txt", "role": "supporting_notes"}])
    chunk = pipeline.ChunkRecord("0001_audio", 1, str(audio), audio.name, str(root / "chunk.wav"), 0, 1, 1)
    monkeypatch.setattr(pipeline.ffmpeg, "require_tool", lambda _: None)
    monkeypatch.setattr(pipeline, "build_chunk_records", lambda *args: [chunk])
    monkeypatch.setattr(pipeline, "ensure_audio_chunks", lambda *args: None)
    class Transcriber:
        def transcribe(self, path, **kwargs):
            return "Approved the budget.", {}
    summary = FakeSummarizer(conflicts=True)
    result = pipeline.summarize_meeting(root, audio_files=[audio], documents=document,
                                       summarizer=summary, transcriber=Transcriber(),
                                       output_dir=root / "out", meeting_date="2026-10-11")
    assert len(result.atomic_summary["items"]) == 2
    assert all(i["uncertain"] for i in result.atomic_summary["items"])
    assert {s["path"] for s in result.sources} == {"audio.wav", "notes.txt"}
    assert all(i["sources"][0]["quote"] for i in result.atomic_summary["items"])
    assert "Approved the budget." not in summary.calls[-1]["user"]
    assert "Deferred the budget." not in summary.calls[-1]["user"]
    assert any("Conflict" in note for note in result.review_notes)

    # Continue the same mixed-material result through the editable artifact handoff.
    from wcfi_tools.creation import workflow
    from wcfi_tools.meeting import summary_artifact
    from test_google_creation import Service
    data = mc.resolve({"version": 1, "meeting": {"date": date(2026, 10, 11)}},
                      setup=guided.setup_defaults(app_config.DEFAULT_CONFIG))[0]
    guided.write_artifact(root / "out", data, result)
    artifact_path = root / "out/SUMMARY.wcfi.md"
    edited = artifact_path.read_text(encoding="utf-8").replace("Not captured.", "Reviewed wording.", 1)
    artifact_path.write_text(edited, encoding="utf-8")
    service = Service(summary_artifact.load(artifact_path))
    monkeypatch.setattr(workflow, "interactive", lambda: True)
    monkeypatch.setattr(workflow, "word", lambda *args: "start")
    monkeypatch.setattr(workflow, "say", lambda *args: None)
    calls_before = len(summary.calls)
    receipt = workflow.run(artifact_path, account="test@example.com", folder="folder-1",
                           service_factory=lambda _: service)
    assert receipt["verification"]["content_and_mode"] == "passed"
    assert "Reviewed wording." in service.payload and len(summary.calls) == calls_before
    assert "Conflicting outcomes" not in service.payload


def test_bad_quote_fails_without_cache(tmp_path):
    class Lying:
        calls = 0
        def structured_json(self, **kwargs):
            self.calls += 1
            return {"items": [fact("Made up quote")]}
    summary = Lying()
    unit = {"id": "doc:1", "role": "supporting_notes", "text": "Actual evidence"}
    with pytest.raises(ValueError, match="not present|Line evidence"):
        evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False)
    assert not list(tmp_path.rglob("*.json"))
    assert summary.calls == 3


def test_corrupt_fact_cache_is_rebuilt(tmp_path):
    unit = {"id": "doc:1", "role": "supporting_notes", "text": "Approved the budget."}
    summary = FakeSummarizer()
    evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False)
    cache = next(tmp_path.rglob("*.json"))
    cache.write_text('{"items":')
    result = evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False)
    assert len(summary.calls) == 2 and len(result["items"]) == 1


def test_unknown_conflict_reference_rejected(tmp_path):
    summary = FakeSummarizer()
    summary.structured_json = lambda **kwargs: {"conflicts": [{"fact_ids": ["fact-1", "invented"], "reason": "x"}]}
    with pytest.raises(ValueError, match="invalid fact references"):
        evidence.find_conflicts([{"id": "fact-1"}, {"id": "fact-2"}], "2026-10-11", summary, tmp_path, "low", False)


def test_invalid_minutes_preserve_user_artifact(configured, monkeypatch):
    root, _, summary = configured
    assert run(root).exit_code == 0
    path = root / "summary/SUMMARY.wcfi.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Not captured.", "User edit.", 1), encoding="utf-8")
    before = {p: p.read_bytes() for p in (path, root / "summary/review-needed.md")}
    original = summary.structured_json
    def invalid(**kwargs):
        return {"markdown": "# Incomplete model output"} if kwargs["schema_name"] == "minutes_markdown" else original(**kwargs)
    monkeypatch.setattr(summary, "structured_json", invalid)
    result = run(root, "yes\nyes\nyes\nstart\n")
    assert result.exit_code == 1
    assert all(p.read_bytes() == content for p, content in before.items())


def test_nonverbatim_quote_retries_with_feedback_and_reuses_valid_cache(tmp_path):
    unit = {"id": "doc:1", "role": "reference", "text": "Budget  approved.\nExact source."}
    class Repairing(FakeSummarizer):
        def structured_json(self, **kwargs):
            if not self.calls:
                self.calls.append(kwargs)
                return {"items": [fact("Budget approved.")]}
            assert "validation_feedback" in json.loads(kwargs["user"])
            return super().structured_json(**kwargs)
    summary = Repairing()
    first = evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False)
    assert len(summary.calls) == 2
    assert first["items"][0]["sources"][0]["quote"] == unit["text"]
    assert first["items"][0]["scope"] == "background"
    assert evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False) == first
    assert len(summary.calls) == 2


def test_large_conflict_review_preserves_all_evidence(tmp_path):
    facts = [{"id": "fact-1", "claim": "A" * 180_000}, {"id": "fact-2", "claim": "B" * 70_000}]
    summary = FakeSummarizer(conflicts=True)
    conflicts = evidence.find_conflicts(facts, "2026-10-11", summary, tmp_path, "low", False)
    assert json.loads(summary.calls[0]["user"])["facts"] == facts
    assert conflicts[0]["fact_ids"] == ["fact-1", "fact-2"]


def test_oversized_conflict_review_stops_before_provider(tmp_path):
    summary = FakeSummarizer()
    facts = [{"id": "fact-1", "claim": "A" * evidence.MAX_CONFLICT_CHARS}]
    with pytest.raises(ValueError, match="too large"):
        evidence.find_conflicts(facts, "2026-10-11", summary, tmp_path, "low", False)
    assert not summary.calls and not list(tmp_path.rglob("*.json"))


def test_line_fallback_copies_exact_table_span_and_caches(tmp_path):
    unit = {"id": "doc:1", "role": "reference", "text": "Heading\nAlice    |    Approved\n         |    budget.\n"}
    class TableSummary(FakeSummarizer):
        def structured_json(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["schema_name"] == "source_facts":
                return {"items": [fact("Alice approved budget.")]}
            assert kwargs["schema_name"] == "source_facts_lines"
            request = json.loads(kwargs["user"])
            assert request["source_lines"][1]["text"] == "Alice    |    Approved\n"
            item = fact(claim="Alice approved the budget.")
            del item["quote"]
            return {"items": [{**item, "start_line": 2, "end_line": 3}]}
    summary = TableSummary()
    result = evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False)
    assert len(summary.calls) == 3
    assert result["items"][0]["sources"][0]["quote"] == "Alice    |    Approved\n         |    budget.\n"
    assert result["items"][0]["scope"] == "background"
    assert evidence.gather([unit], "2026-10-11", summary, tmp_path, "low", False) == result
    assert len(summary.calls) == 3


@pytest.mark.parametrize("start,end", [(0, 1), (1, 3), (2, 1), (True, 1), (1, 1.0)])
def test_line_fallback_rejects_invalid_ranges(start, end):
    item = fact()
    del item["quote"]
    with pytest.raises(ValueError, match="outside the source"):
        evidence._resolve_line_facts({"items": [{**item, "start_line": start, "end_line": end}]},
                                     {"id": "doc:1", "role": "reference", "text": "First\nSecond\n"})
