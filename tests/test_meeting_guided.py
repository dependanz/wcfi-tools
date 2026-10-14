"""Guided CLI boundary and fake-provider integration tests; no live requests."""

from __future__ import annotations

import copy
import json
import shutil
import wave
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from wcfi_tools import config as app_config
from wcfi_tools.cli import app
from wcfi_tools.meeting import configuration as mc
from wcfi_tools.meeting import guided, pipeline

runner = CliRunner()
BODY = (Path(__file__).parent / "fixtures/meeting-summary.wcfi.md").read_text(encoding="utf-8").split("---", 2)[2].strip()


@pytest.fixture
def env(monkeypatch, tmp_path):
    root = tmp_path / "meeting"
    root.mkdir()
    monkeypatch.setattr(app_config, "is_configured", lambda: True)
    monkeypatch.setattr(app_config, "load_config", lambda: copy.deepcopy(app_config.DEFAULT_CONFIG))
    monkeypatch.setattr(app_config, "config_dir", lambda: tmp_path / "preferences")
    monkeypatch.setattr(guided, "interactive", lambda: True)
    monkeypatch.setattr(guided.ffmpeg, "require_tool", lambda _: None)
    monkeypatch.setattr(guided.ffmpeg, "ffprobe_audio_duration", lambda _: 2.0)
    calls = []
    monkeypatch.setattr(guided, "build_summarizer", lambda *a, **k: calls.append(("summary", k)) or object())
    monkeypatch.setattr(guided, "build_transcriber", lambda *a, **k: calls.append(("transcribe", k)) or object())

    def fake_pipeline(root, **kwargs):
        calls.append(("run", kwargs))
        kwargs["work_dir"].mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(work_dir=kwargs["work_dir"], minutes_markdown=BODY,
                               atomic_summary={"items": []})
    monkeypatch.setattr(guided, "summarize_meeting", fake_pipeline)
    return root, calls


def configure(root, **extra):
    (root / "a.m4a").write_bytes(b"selected audio")
    raw = {"version": 1, "meeting": {"date": date(2026, 10, 11)},
           "recordings": [{"id": "audio", "path": "a.m4a", "order": 1}], **extra}
    data = mc.resolve(raw, setup=guided.setup_defaults(app_config.DEFAULT_CONFIG))[0]
    (root / mc.CONFIG_NAME).write_text(mc.dump(data), encoding="utf-8")
    return data


def test_prompt_only_entry_creates_explicit_config_and_artifact(env):
    root, calls = env
    (root / "a.m4a").write_bytes(b"selected")
    answer = f"{root}\nyes\nyes\n2026-10-11\nyes\nyes\nstart\n"
    result = runner.invoke(app, ["meeting", "summarize"], input=answer)
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["summary", "transcribe", "run"]
    saved = mc.load(root)
    assert saved["recordings"][0]["path"] == "a.m4a"
    assert saved["meeting"]["date"] == date(2026, 10, 11)
    artifact = (root / "summary" / "SUMMARY.wcfi.md").read_text()
    assert 'meeting_date: "2026-10-11"' in artifact and "layout: pageless" in artifact
    assert artifact.endswith(BODY + "\n")
    reuse = json.loads(guided.last_path().read_text())
    assert "meeting" not in reuse and "recordings" not in reuse
    assert "directory" not in reuse["output"]


@pytest.mark.parametrize("answer", ["", "yes\n", "yes\nyes\ncancel\n"])
def test_eof_or_cancel_never_calls_provider(env, answer):
    root, calls = env
    configure(root)
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input=answer)
    assert not calls
    assert not guided.last_path().exists()
    assert not (root / "summary").exists()
    assert result.exit_code in (0, 1)


def test_blank_and_short_responses_do_not_approve(env):
    root, calls = env
    configure(root)
    result = runner.invoke(app, ["meeting", "summarize", str(root)],
                           input="\ny\nyes\n\nyes\n\ny\ncancel\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Enter accepts nothing") >= 4
    assert not calls


def test_complete_config_still_requires_start(env):
    root, calls = env
    configure(root)
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\nstart\n")
    assert result.exit_code == 0, result.output
    assert "Audio chunks ->" in result.output
    assert "Paid API use" in result.output
    assert calls[-1][1]["prepared_by"] == "Not captured"
    assert calls[-1][1]["location"] == "Not captured"


def test_noninteractive_live_fails_and_dry_run_is_read_only(env, monkeypatch):
    root, calls = env
    monkeypatch.setattr(guided, "interactive", lambda: False)
    (root / "a.m4a").write_bytes(b"audio")
    result = runner.invoke(app, ["meeting", "summarize", str(root)])
    assert result.exit_code == 1 and "Interactive terminal" in result.output
    result = runner.invoke(app, ["meeting", "summarize", str(root), "--dry-run"])
    assert result.exit_code == 0 and "[unassigned]" in result.output
    assert not (root / mc.CONFIG_NAME).exists() and not calls


def test_named_overrides_preserve_false_and_skip_missing_date(env):
    root, calls = env
    configure(root, processing={"speakers": {"identify": True}})
    result = runner.invoke(app, ["meeting", "summarize", str(root),
                                "--meeting-date", "2026-10-12", "--no-identify",
                                "--summary-model", "chosen-model"],
                           input="yes\nyes\nno\nstart\n")
    assert result.exit_code == 0, result.output
    assert calls[0][1]["model"] == "chosen-model"
    assert calls[-1][1]["meeting_date"] == "2026-10-12"
    assert calls[-1][1]["roster"] == {}
    assert mc.load(root)["meeting"]["date"] == date(2026, 10, 11)


def test_unsupported_selected_images_fail_before_start(env):
    root, calls = env
    (root / "image.png").write_bytes(b"synthetic image")
    configure(root, images=[{"id": "image", "path": "image.png", "use": "extract_text"}],
              processing={"image_text": {"provider": "fake", "model": "fake"}})
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\n")
    assert result.exit_code == 1 and "not implemented yet" in result.output
    assert not calls


def test_overlapping_duplicate_bytes_are_blocked(env):
    root, calls = env
    data = configure(root)
    (root / "b.m4a").write_bytes((root / "a.m4a").read_bytes())
    data["recordings"].append({"id": "b", "path": "b.m4a", "order": 2})
    (root / mc.CONFIG_NAME).write_text(mc.dump(data))
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\n")
    assert result.exit_code == 1 and "overlapping" in result.output
    assert not calls


def test_ranges_and_order_are_passed_exactly(env):
    root, calls = env
    data = configure(root)
    (root / "b.m4a").write_bytes(b"second source")
    data["recordings"][0].update(order=2, start_seconds=1, end_seconds=2)
    data["recordings"].append({"id": "b", "path": "b.m4a", "order": 1, "end_seconds": 1})
    (root / mc.CONFIG_NAME).write_text(mc.dump(data))
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\nstart\n")
    assert result.exit_code == 0, result.output
    kwargs = calls[-1][1]
    assert [p.name for p in kwargs["audio_files"]] == ["b.m4a", "a.m4a"]
    assert kwargs["recording_ranges"][root / "a.m4a"] == (1, 2)


@pytest.mark.parametrize("change", ["source", "config", "new_file", "output"])
def test_changed_review_state_refuses_provider_calls(env, monkeypatch, change):
    root, calls = env
    configure(root)
    def answer(prompt, choices=("yes", "no")):
        if choices == ("start", "cancel"):
            if change == "source":
                (root / "a.m4a").write_bytes(b"changed bytes")
            elif change == "config":
                (root / mc.CONFIG_NAME).write_text("version = 1")
            elif change == "new_file":
                (root / "b.m4a").write_bytes(b"unexpected audio")
            else:
                (root / "summary").mkdir()
                (root / "summary" / "SUMMARY.wcfi.md").write_text("user edit")
            return "start"
        return "yes"
    monkeypatch.setattr(guided, "word", answer)
    result = runner.invoke(app, ["meeting", "summarize", str(root)])
    assert result.exit_code == 1 and "changed" in result.output.lower()
    assert not calls


def test_existing_edit_is_preserved_when_overwrite_declined(env):
    root, calls = env
    configure(root)
    output = root / "summary"
    output.mkdir()
    (output / "SUMMARY.wcfi.md").write_text("edited by the user")
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\nno\n")
    assert result.exit_code == 0, result.output
    assert (output / "SUMMARY.wcfi.md").read_text() == "edited by the user"
    assert not calls


def test_failure_does_not_save_last_success(env, monkeypatch):
    root, _ = env
    configure(root)
    def fail(*a, **k):
        raise RuntimeError("Synthetic provider failure")
    monkeypatch.setattr(guided, "summarize_meeting", fail)
    result = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\nstart\n")
    assert result.exit_code == 1
    assert not guided.last_path().exists()


def test_real_chunking_with_fake_providers_and_content_based_cache(env, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg not installed")
    root, _ = env
    data = configure(root)
    (root / "a.m4a").unlink()
    data["recordings"][0]["path"] = "a.wav"
    path = root / "a.wav"
    def audio(sample):
        with wave.open(str(path), "wb") as w:
            w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            w.writeframes(sample * 32000)
    audio(b"\x00\x00")
    (root / mc.CONFIG_NAME).write_text(mc.dump(data))
    counts = {"transcribe": 0, "atomic": 0, "minutes": 0}
    class Transcriber:
        def transcribe(self, path, **kwargs):
            counts["transcribe"] += 1
            return "Synthetic meeting evidence.", {}
    class Summarizer:
        def structured_json(self, **kwargs):
            if kwargs["schema_name"] == "minutes_markdown":
                counts["minutes"] += 1
                return {"markdown": BODY}
            counts["atomic"] += 1
            return {"items": []}
    monkeypatch.setattr(guided, "build_transcriber", lambda *a, **k: Transcriber())
    monkeypatch.setattr(guided, "build_summarizer", lambda *a, **k: Summarizer())
    monkeypatch.setattr(guided, "summarize_meeting", pipeline.summarize_meeting)
    first = runner.invoke(app, ["meeting", "summarize", str(root)], input="yes\nyes\nstart\n")
    assert first.exit_code == 0, first.output
    assert counts == {"transcribe": 1, "atomic": 2, "minutes": 1}
    second = runner.invoke(app, ["meeting", "summarize", str(root)],
                           input="yes\nyes\nyes\nstart\n")
    assert second.exit_code == 0, second.output
    assert counts == {"transcribe": 1, "atomic": 2, "minutes": 2}
    audio(b"\x01\x00")
    third = runner.invoke(app, ["meeting", "summarize", str(root)],
                          input="yes\nyes\nyes\nstart\n")
    assert third.exit_code == 0, third.output
    assert counts == {"transcribe": 2, "atomic": 4, "minutes": 3}
    assert (root / "summary" / "source-index.md").is_file()
    assert not (root / "_work").exists()



def test_root_without_arguments_lists_commands(env):
    _, calls = env
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "meeting" in result.output and "setup" in result.output
    assert not calls


@pytest.mark.parametrize("collision", ["SUMMARY.wcfi.md", "_work"])
def test_output_collisions_stop_before_provider_calls(env, collision):
    root, calls = env
    configure(root)
    output = root / "summary"
    output.mkdir()
    if collision == "_work":
        (output / collision).write_text("user file")
    else:
        (output / collision).mkdir()
    result = runner.invoke(app, ["meeting", "summarize", str(root)],
                           input="yes\nyes\nstart\n")
    assert result.exit_code == 1, result.output
    assert not calls


def test_model_change_invalidates_cached_evidence(env):
    root, _ = env
    data = configure(root)
    original = guided.preflight(root, data)[1]
    changed = copy.deepcopy(data)
    changed["processing"]["summarization"]["model"] = "new-summary-model"
    assert guided.preflight(root, changed)[1] != original
    changed = copy.deepcopy(data)
    changed["processing"]["transcription"]["model"] = "new-transcription-model"
    assert guided.preflight(root, changed)[1] != original


@pytest.mark.parametrize("review", ["start", "cancel", "change_config"])
def test_speaker_review_uses_selected_range_and_rechecks_before_providers(env, monkeypatch, review):
    from wcfi_tools.commands import speakers
    root, calls = env
    data = configure(root, processing={"speakers": {"identify": True}})
    data["recordings"][0].update(start_seconds=1, end_seconds=2)
    (root / mc.CONFIG_NAME).write_text(mc.dump(data))
    local = []

    def chunks(folder, files, work, minutes, progress, ranges):
        assert ranges == {root / "a.m4a": (1, 2)}
        chunk = work / "chunks" / "selected.wav"
        chunk.parent.mkdir(parents=True)
        chunk.write_bytes(b"selected range only")
        return [SimpleNamespace(chunk_path=str(chunk))]

    def identify(files, work, **kwargs):
        assert [p.name for p in files] == ["selected.wav"]
        local.append("identified")
        if review == "change_config":
            (root / mc.CONFIG_NAME).write_text("version = 1")
        return {"Synthetic Person": "voice1"}, {}

    monkeypatch.setattr(pipeline, "build_chunk_records", chunks)
    monkeypatch.setattr(pipeline, "ensure_audio_chunks", lambda *a: None)
    monkeypatch.setattr(speakers, "identify_present", identify)
    reply = "start" if review == "change_config" else review
    result = runner.invoke(app, ["meeting", "summarize", str(root)],
                           input=f"yes\nyes\nstart\n{reply}\n")
    assert local == ["identified"], result.output
    if review == "start":
        assert result.exit_code == 0, result.output
        assert calls[-1][0] == "run"
        assert "does not establish attendance" in calls[-1][1]["roster_note"]
    else:
        assert not calls
        assert not guided.last_path().exists()
