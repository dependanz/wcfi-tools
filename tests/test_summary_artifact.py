from pathlib import Path

import pytest

from wcfi_tools.meeting import summary_artifact as sa

TEXT = (Path(__file__).parent / "fixtures/meeting-summary.wcfi.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("old,new,match", [
    ("wcfi_version: 1", "wcfi_version: 1\nwcfi_version: 1", "Duplicate"),
    ("wcfi_version: 1", "wcfi_version: true", "integer 1"),
    ("kind: meeting_summary", "kind: thumbnail", "kind"),
    ("wcfi_version: 1", "wcfi_version: &value 1", "anchors"),
    ("wcfi_version: 1", "wcfi_version: !!int 1", "tags"),
    ("wcfi_version: 1", "wcfi_version: *value", "aliases"),
    ("wcfi_version: 1", "wcfi_version: 1\nunknown: true", "unsupported"),
    ('meeting_date: "2026-10-11"', 'meeting_date: 2026-10-11', "quote dates"),
    ('meeting_date: "2026-10-11"', 'meeting_date: "2026-02-30"', "YYYY-MM-DD"),
    ("template_version: 1", "template_version: 99", "Supported presentation"),
    ("layout: pageless", "layout: pageless\n  page_limit: 1", "Remove page_limit"),
    ("## Attendance", "## Attendees", "Required H2"),
    ("# Board Meeting Minutes", "# Different title", "opening H1"),
    ("## Attendance", "## Attendance\n\n<script>evil()</script>", "html_block"),
    ("## Attendance", "## Attendance\n\n![photo](https://example.com/a.png)", "image"),
    ("## Attendance", "## Attendance\n\n> unsupported", "blockquote"),
    ("## Attendance", "## Attendance\n\n[x](file:///secret)", "HTTPS"),
    ("| Field | Detail |", "| Field | Detail | Extra |", "Required H2|table|columns"),
    ("| Meeting Time | Not captured |", "| Meeting Time | Not captured | EXTRA |", "Ragged"),
    ('review_notes: "review-needed.md"', 'review_notes: "../escape.md"', "escapes"),
    ('review_notes: "review-needed.md"', 'review_notes: "C:/secret.md"', "relative"),
])
def test_rejects_unsafe_or_ambiguous_artifact(tmp_path, old, new, match):
    with pytest.raises(sa.ArtifactError, match=match):
        sa.parse_text(TEXT.replace(old, new), tmp_path / "SUMMARY.wcfi.md")


def test_bom_and_missing_sidecars_are_disclosed(tmp_path):
    artifact = sa.parse_text("\ufeff" + TEXT, tmp_path / "SUMMARY.wcfi.md")
    assert len(artifact.warnings) == 2
    assert artifact.body.endswith("Not captured.\n")
    assert artifact.metadata["meeting_date"] == "2026-10-11"


def test_empty_section_rejected(tmp_path):
    with pytest.raises(sa.ArtifactError, match="Empty sections"):
        sa.parse_text(TEXT.replace("## Attendance\n\nNot captured.", "## Attendance"), tmp_path / "x.wcfi.md")


def test_body_line_number_includes_front_matter(tmp_path):
    text = TEXT.replace("Not captured.", "<script>bad</script>", 1)
    expected = text[:text.index("<script>")].count("\n") + 1
    with pytest.raises(sa.ArtifactError, match=f"Line {expected}:"):
        sa.parse_text(text, tmp_path / "x.wcfi.md")


def test_large_control_invalid_utf8_and_extension(tmp_path):
    with pytest.raises(sa.ArtifactError, match="control"):
        sa.parse_text(TEXT + "\x00", tmp_path / "x.wcfi.md")
    path = tmp_path / "x.wcfi.md"
    path.write_bytes(b"x" * (sa.MAX_BYTES + 1))
    with pytest.raises(sa.ArtifactError, match="million"):
        sa.load(path)
    path.write_bytes(b"\xff")
    with pytest.raises(sa.ArtifactError, match="UTF-8"):
        sa.load(path)
    with pytest.raises(sa.ArtifactError, match="explicit"):
        sa.load(tmp_path / "random.md")
