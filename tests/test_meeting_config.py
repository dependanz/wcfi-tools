"""Offline contract tests for explicit meeting config, paths, inventory and precedence."""

from __future__ import annotations

import copy
import math
import tomllib
from datetime import date

import pytest

from wcfi_tools.meeting import configuration as mc


def resolved(raw=None, **kwargs):
    return mc.resolve({"version": 1, **(raw or {})}, **kwargs)[0]


def recording(name="a.m4a", **kwargs):
    return {"id": "a", "path": name, "order": 1, **kwargs}


def test_partial_config_and_exact_filename(tmp_path):
    (tmp_path / "meeting.toml").write_text("not valid TOML [")
    assert mc.load(tmp_path) == {"version": 1}
    (tmp_path / mc.CONFIG_NAME).write_text("\ufeffversion = 1\n", encoding="utf-8")
    assert mc.load(tmp_path) == {"version": 1}
    data = resolved()
    assert "meeting.date" in mc.missing_fields(data)
    assert "processing.summarization.model" in mc.missing_fields(data)


@pytest.mark.parametrize("text", [
    "version = true", "version = 2", 'version = "1"', "version = 1\nversion = 1",
    "version = 1\napproved = true", "version = 1\n[output]\npage_limit = 1",
    "version = 1\n[output]\nlayout = 'pages'", "version = 1\n[meeting]\ndate = '2026-10-11'",
    "version = 1\n[meeting]\ndate = 2026-10-11T12:00:00",
    "version = 1\n[meeting]\ntime = '25:00'",
    "version = 1\n[meeting]\ntimezone = 'No/Such_Zone'",
    "version = 1\n[processing.transcription]\nchunk_minutes = nan",
    "version = 1\n[processing.transcription]\nchunk_minutes = 0",
    "version = 1\n[processing.speakers]\nidentify = 'false'",
    "version = 1\n[processing.speakers]\nidentify = false\ncount = 2",
    "version = 1\n[output]\nformats = ['plain_text']",
    "version = 1\n[output]\ninclude_review_notes = false",
    "version = 1\n[processing.summarization]\nmodel = ''",
])
def test_reject_invalid_config(tmp_path, text):
    (tmp_path / mc.CONFIG_NAME).write_text(text, encoding="utf-8")
    with pytest.raises(mc.ConfigError):
        mc.load(tmp_path)


def test_roundtrip_contract_with_all_materials():
    raw = {
        "version": 1, "meeting": {"date": date(2026, 10, 11), "title": 'Quotes " and\nnewline'},
        "recordings": [recording("a.mp4", start_seconds=1.5, end_seconds=2.5)],
        "documents": [{"id": "agenda", "path": "agenda.pdf", "role": "agenda"}],
        "images": [{"id": "image", "path": "image.png", "use": "extract_text"}],
        "videos": [{"id": "video", "path": "a.mp4", "use": "analyze_visual",
                    "frames_seconds": [0, 1.5]}],
        "excluded": [{"path": "other.bin", "reason": "Not needed"}],
    }
    data = resolved(raw)
    assert mc.validate(tomllib.loads(mc.dump(data))) == data


def test_precedence_false_and_private_defaults():
    setup = {"processing": {"speakers": {"identify": True},
                            "summarization": {"provider": "openai", "model": "setup"}}}
    last = {"processing": {"speakers": {"identify": True, "count": 7},
                           "summarization": {"provider": "openai", "model": "last"}},
            "meeting": {"date": date(2026, 1, 1)}, "recordings": [recording()],
            "output": {"directory": "private/old"}}
    raw = {"version": 1, "processing": {"summarization": {"model": "config"}}}
    overrides = {"processing": {"summarization": {"model": "named"},
                                "speakers": {"identify": False}}}
    data, origins = mc.resolve(raw, setup=setup, last=last, overrides=overrides)
    assert data["processing"]["summarization"]["model"] == "named"
    assert origins["processing.summarization.model"] == "command line"
    assert data["processing"]["speakers"] == {"identify": False}
    assert data["output"]["directory"] == "summary"
    assert "meeting" not in data and "recordings" not in data
    assert raw["processing"]["summarization"]["model"] == "config"


def test_provider_switch_does_not_inherit_wrong_model():
    data = resolved(setup={"processing": {"summarization": {"provider": "openai", "model": "gpt"}}},
                    overrides={"processing": {"summarization": {"provider": "anthropic"}}})
    assert "model" not in data["processing"]["summarization"]
    assert "processing.summarization.model" in mc.missing_fields(data)


@pytest.mark.parametrize("path", ["../a.m4a", "a/../b.m4a", "/a.m4a", "C:/a.m4a",
                                  "C:a.m4a", "*.m4a", "https://x/a.m4a", "$HOME/a.m4a",
                                  "%HOME%/a.m4a", "a//b.m4a"])
def test_input_paths_are_literal_and_contained(path):
    with pytest.raises(mc.ConfigError):
        resolved({"recordings": [recording(path)]})


def test_inventory_never_selects_unlisted_or_generated_files(tmp_path):
    for name in ("a.m4a", "notes.md", "extra.zip", "minutes.md", ".env"):
        (tmp_path / name).write_bytes(b"x")
    for name in ("summary", "_work", ".venv", ".credentials"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "private.m4a").write_bytes(b"secret")
    data = resolved({"recordings": [recording()],
                     "excluded": [{"path": "missing.txt"}, {"path": "extra.zip"}]})
    items = {x.path: x for x in mc.inventory(tmp_path, data)}
    assert items["a.m4a"].status == "selected"
    assert items["notes.md"].status == "unassigned"
    assert items["extra.zip"].status == "excluded"
    assert items["missing.txt"].status == "excluded"
    for name in ("summary", "_work", ".venv", ".credentials", ".env", "minutes.md"):
        assert items[name].status == "builtin"
    assert not any("private.m4a" in name for name in items)


def test_previous_generated_document_requires_explicit_role(tmp_path):
    (tmp_path / "minutes.md").write_text("old")
    data = resolved({"documents": [{"id": "old", "path": "minutes.md", "role": "previous_minutes"}]})
    assert mc.inventory(tmp_path, data)[0].status == "selected"


@pytest.mark.parametrize("mutate", [
    lambda d: d["recordings"].append(recording(id="b", order=2)),
    lambda d: d.update(excluded=[{"path": "a.m4a"}]),
    lambda d: d["recordings"][0].update(order=2),
    lambda d: d["recordings"][0].update(start_seconds=2, end_seconds=1),
    lambda d: d["recordings"][0].update(start_seconds=math.inf),
    lambda d: d["recordings"][0].update(path=".env"),
    lambda d: d.update(videos=[{"id": "v", "path": "v.mp4", "use": "analyze_visual",
                                "frames_seconds": [2, 1]}]),
])
def test_conflicting_selections_fail(mutate):
    data = {"version": 1, "recordings": [recording()]}
    mutate(data)
    with pytest.raises(mc.ConfigError):
        mc.validate(data)


def test_output_cannot_be_root_ancestor_input_or_git(tmp_path):
    meeting = tmp_path / "meeting"
    meeting.mkdir()
    (meeting / "a.m4a").write_bytes(b"audio")
    for path in (".", "..", "a.m4a"):
        data = resolved({"recordings": [recording()], "output": {"directory": path}})
        with pytest.raises(mc.ConfigError):
            mc.output_path(meeting, data)
    (tmp_path / ".git").mkdir()
    with pytest.raises(mc.ConfigError, match="Git"):
        mc.output_path(meeting, resolved())


def test_output_containing_selected_input_fails(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.m4a").write_bytes(b"audio")
    data = resolved({"recordings": [recording("nested/a.m4a")],
                     "output": {"directory": "nested"}})
    with pytest.raises(mc.ConfigError):
        mc.inventory(tmp_path, data)


def test_escaping_link_not_traversed(tmp_path):
    root = tmp_path / "meeting"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.m4a").write_bytes(b"secret")
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this Windows installation")
    items = mc.inventory(root, resolved())
    assert [(x.path, x.status) for x in items] == [("link", "builtin")]
    with pytest.raises(mc.ConfigError, match="outside"):
        mc.inventory(root, resolved({"recordings": [recording("link/private.m4a")]}))


def test_reusable_settings_never_contain_speaker_count_or_paths():
    data = resolved({"meeting": {"date": date(2026, 1, 1), "location": "private"},
                     "recordings": [recording()],
                     "processing": {"speakers": {"identify": True, "count": 4}}})
    reuse = mc.reusable(data)
    assert set(reuse) == {"processing", "output"}
    assert "count" not in reuse["processing"]["speakers"]
    assert "directory" not in reuse["output"]
    assert mc.validate({"version": 1, **copy.deepcopy(reuse)})
