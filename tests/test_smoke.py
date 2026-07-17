"""Smoke tests that need no API keys, network, or ffmpeg."""

from __future__ import annotations

import tomllib

from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.config import DEFAULT_CONFIG, _dump_toml
from wcfi_tools.meeting.artifacts import markdown_to_paste_block
from wcfi_tools.meeting.pipeline import derive_meeting_date

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "setup" in result.output
    assert "meeting" in result.output


def test_meeting_help():
    result = runner.invoke(app, ["meeting", "--help"])
    assert result.exit_code == 0
    assert "summarize" in result.output


def test_config_toml_roundtrips():
    rendered = _dump_toml(DEFAULT_CONFIG)
    parsed = tomllib.loads(rendered)
    assert parsed["providers"]["summarizer"] == "openai"
    assert parsed["models"]["transcribe"] == "gpt-4o-transcribe"


def test_derive_meeting_date():
    from pathlib import Path

    assert derive_meeting_date(Path("071226")) == "Jul 12, 2026"
    assert derive_meeting_date(Path("not-a-date")) == "Not explicitly captured"


def test_paste_block_flattens_table_and_emphasis():
    md = "# Metadata\n\n| Field | Detail |\n|---|---|\n| Meeting Date | **Jul 12, 2026** |\n"
    out = markdown_to_paste_block(md)
    assert "=== Metadata ===" in out
    assert "Field | Detail" in out
    assert "Meeting Date | Jul 12, 2026" in out
    assert "**" not in out
    assert "---" not in out
