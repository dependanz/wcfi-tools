import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.creation import template, workflow
from wcfi_tools.creation.google import GoogleError
from wcfi_tools.meeting import summary_artifact as sa

TEXT = (Path(__file__).parent / "fixtures/meeting-summary.wcfi.md").read_text(encoding="utf-8")


def native_document(artifact):
    """Synthetic Docs response, including UTF-16 indices and native table cells."""
    content, index, table, row, cell = [], 1, None, None, None
    for token in artifact.tokens:
        if token.type == "table_open":
            table = {"startIndex": index, "table": {"tableRows": []}}
            content.append(table)
            index += 1
        elif token.type == "tr_open":
            row = {"tableCells": []}
            table["table"]["tableRows"].append(row)
            index += 1
        elif token.type in {"th_open", "td_open"}:
            cell = {"content": []}
            row["tableCells"].append(cell)
            index += 1
        elif token.type == "inline":
            text = sa.inline_text(token) + "\n"
            end = index + len(text.encode("utf-16-le")) // 2
            element = {"startIndex": index, "endIndex": end, "paragraph": {
                "paragraphStyle": {}, "elements": [{"startIndex": index, "endIndex": end,
                "textRun": {"content": text, "textStyle": {}}}]}}
            (cell["content"] if cell is not None else content).append(element)
            index = end
        elif token.type in {"th_close", "td_close"}:
            cell = None
        elif token.type == "table_close":
            table = None
    document = {"body": {"content": content}, "documentStyle": {}, "revisionId": "r1"}
    for (element, _), detail in zip(template.inspect_content(document)[0], template._details(artifact)):
        if detail["depth"] >= 0:
            element["paragraph"]["bullet"] = {"listId": str(detail["group"]), "nestingLevel": detail["depth"]}
    return document



def edit_native_text(document, start, end, text="", date_properties=None):
    """Minimal UTF-16 insertion/deletion model for the API's index-changing requests."""
    all_paragraphs = []
    def collect(nodes):
        for e in nodes:
            if "paragraph" in e:
                all_paragraphs.append(e)
            elif "table" in e:
                for row in e["table"]["tableRows"]:
                    for cell in row["tableCells"]:
                        collect(cell["content"])
    collect(template.body_content(document))
    target = next(e for e in all_paragraphs if e["startIndex"] <= start < e["endIndex"])
    count = 1 if date_properties is not None else template._length(text)
    delta = count - (end - start)
    left, right = [], []
    for part in target["paragraph"]["elements"]:
        a, b = part["startIndex"], part["endIndex"]
        if b <= start:
            left.append(copy.deepcopy(part))
        elif a < start:
            piece = copy.deepcopy(part)
            piece["endIndex"] = start
            piece["textRun"]["content"] = part["textRun"]["content"].encode("utf-16-le")[:(start-a)*2].decode("utf-16-le")
            left.append(piece)
        if a >= end:
            piece = copy.deepcopy(part)
            piece.update(startIndex=a+delta, endIndex=b+delta)
            right.append(piece)
        elif b > end:
            piece = copy.deepcopy(part)
            piece.update(startIndex=end+delta, endIndex=b+delta)
            piece["textRun"]["content"] = part["textRun"]["content"].encode("utf-16-le")[(end-a)*2:].decode("utf-16-le")
            right.append(piece)
    middle = []
    if count:
        body = {"dateElement": {"dateElementProperties": date_properties, "textStyle": {}}} if date_properties is not None else {"textRun": {"content": text, "textStyle": {}}}
        middle = [{"startIndex": start, "endIndex": start+count, **body}]
    target["paragraph"]["elements"] = left + middle + right
    target["endIndex"] += delta
    def shift(obj):
        if obj is target:
            return
        if isinstance(obj, dict):
            if obj.get("startIndex", -1) >= end:
                obj["startIndex"] += delta
                if "endIndex" in obj:
                    obj["endIndex"] += delta
            elif "endIndex" in obj and (obj["endIndex"] > end or (end > start and obj["endIndex"] == end)):
                obj["endIndex"] += delta
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    shift(value)
        elif isinstance(obj, list):
            for value in obj:
                shift(value)
    shift(document)


def apply_requests(document, requests):
    for request in requests:
        if "deleteContentRange" in request:
            u = request["deleteContentRange"]["range"]
            edit_native_text(document, u["startIndex"], u["endIndex"])
        elif "insertDate" in request:
            u = request["insertDate"]
            edit_native_text(document, u["location"]["index"], u["location"]["index"], date_properties=u["dateElementProperties"])
        elif "insertText" in request:
            u = request["insertText"]
            edit_native_text(document, u["location"]["index"], u["location"]["index"], text=u["text"])
        elif "updateDocumentStyle" in request:
            document["documentStyle"].update(request["updateDocumentStyle"]["documentStyle"])
        elif "updateTableCellStyle" in request:
            update = request["updateTableCellStyle"]
            index = update["tableRange"]["tableCellLocation"]["tableStartLocation"]["index"]
            table = next(e for e, _ in template.inspect_content(document)[1] if e["startIndex"] == index)
            selected = update["tableRange"]
            first_row = selected["tableCellLocation"].get("rowIndex", 0)
            first_col = selected["tableCellLocation"].get("columnIndex", 0)
            for row in table["table"]["tableRows"][first_row:first_row+selected["rowSpan"]]:
                for cell in row["tableCells"][first_col:first_col+selected["columnSpan"]]:
                    cell.setdefault("tableCellStyle", {}).update(update["tableCellStyle"])
        elif "updateTableColumnProperties" in request:
            u = request["updateTableColumnProperties"]
            table = next(e for e, _ in template.inspect_content(document)[1] if e["startIndex"] == u["tableStartLocation"]["index"])["table"]
            cols = table.setdefault("tableStyle", {}).setdefault("tableColumnProperties", [{} for _ in table["tableRows"][0]["tableCells"]])
            for i in u["columnIndices"]:
                cols[i].update(u["tableColumnProperties"])
        elif "pinTableHeaderRows" in request or "updateTableRowStyle" in request:
            pin = "pinTableHeaderRows" in request
            u = request["pinTableHeaderRows" if pin else "updateTableRowStyle"]
            table = next(e for e, _ in template.inspect_content(document)[1] if e["startIndex"] == u["tableStartLocation"]["index"])["table"]
            for i, row in enumerate(table["tableRows"]):
                row.setdefault("tableRowStyle", {}).update({"tableHeader": i < u["pinnedHeaderRowsCount"]} if pin else u["tableRowStyle"])
        elif "deleteParagraphBullets" in request or "createParagraphBullets" in request:
            creating = "createParagraphBullets" in request
            u = request["createParagraphBullets" if creating else "deleteParagraphBullets"]
            for e, value in reversed(template.inspect_content(document)[0]):
                if u["range"]["startIndex"] <= e["startIndex"] < u["range"]["endIndex"]:
                    if creating:
                        depth = len(value)-len(value.lstrip("\t"))
                        if depth:
                            edit_native_text(document, e["startIndex"], e["startIndex"]+depth)
                        e["paragraph"]["bullet"] = {"listId": "test", "nestingLevel": depth}
                    else:
                        e["paragraph"].pop("bullet", None)
        else:
            key = "updateTextStyle" if "updateTextStyle" in request else "updateParagraphStyle"
            update = request[key]
            start, end = update["range"]["startIndex"], update["range"]["endIndex"]
            for element in [e for e, _ in template.inspect_content(document)[0]] + [e for e in template.body_content(document) if "paragraph" in e and any("inlineObjectElement" in p for p in e["paragraph"]["elements"])]:
                if element["startIndex"] <= start < element["endIndex"]:
                    if key == "updateParagraphStyle":
                        if "namedStyleType" in update["paragraphStyle"]:
                            # Google resets run formatting when a named paragraph style is applied.
                            for part in element["paragraph"]["elements"]:
                                part.get("textRun", part.get("dateElement", part.get("inlineObjectElement", {})))["textStyle"] = {}
                        element["paragraph"]["paragraphStyle"].update(update["paragraphStyle"])
                        continue
                    parts = []
                    for part in element["paragraph"]["elements"]:
                        if "textRun" not in part:
                            if start <= part["startIndex"] and part["endIndex"] <= end:
                                part.get("dateElement", part.get("inlineObjectElement", {})).setdefault("textStyle", {}).update(update["textStyle"])
                            parts.append(part)
                            continue
                        boundaries = sorted({part["startIndex"], part["endIndex"],
                                            *[v for v in (start, end) if part["startIndex"] < v < part["endIndex"]]})
                        for left, right in zip(boundaries, boundaries[1:]):
                            piece = copy.deepcopy(part)
                            raw = part["textRun"]["content"].encode("utf-16-le")
                            piece["textRun"]["content"] = raw[(left-part["startIndex"])*2:(right-part["startIndex"])*2].decode("utf-16-le")
                            piece.update(startIndex=left, endIndex=right)
                            if start <= left and right <= end:
                                piece["textRun"]["textStyle"].update(update["textStyle"])
                            parts.append(piece)
                    element["paragraph"]["elements"] = parts
    document["revisionId"] += "x"


class Service:
    def __init__(self, artifact):
        self.doc = native_document(artifact)
        self.remote = []
        self.created = self.styled = 0
        self.payload = ""
        self.timeout = False
        self.ctx = {"account": {"emailAddress": "test@example.com", "permissionId": "user-1"},
                    "folder": {"id": "folder-1", "name": "Destination"},
                    "inherited_access": [{"type": "user", "role": "owner", "emailAddress": "test@example.com"}]}

    def context(self, folder):
        return copy.deepcopy(self.ctx)

    def find_operation(self, operation):
        return copy.deepcopy(self.remote)

    def create(self, title, folder, payload, operation):
        self.created += 1
        self.payload = payload
        self.remote = [{"id": "doc-1", "name": title, "parents": [folder],
                        "appProperties": {"wcfi_operation": operation}}]
        if self.timeout:
            raise TimeoutError("response lost")
        return self.remote[0]

    def read(self, _):
        return copy.deepcopy(self.doc)

    def style(self, _, requests, revision):
        assert revision == self.doc["revisionId"]
        self.styled += 1
        apply_requests(self.doc, requests)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "SUMMARY.wcfi.md"
    path.write_text(TEXT, encoding="utf-8")
    service = Service(sa.load(path))
    monkeypatch.setattr(workflow, "interactive", lambda: True)
    monkeypatch.setattr(workflow, "word", lambda *args: "start")
    monkeypatch.setattr(workflow, "say", lambda *args: None)
    return path, service


def run(path, service, **kwargs):
    return workflow.run(path, folder="folder-1", account="test@example.com", service_factory=lambda _: service, **kwargs)


def test_create_readback_retry_and_sidecar_exclusion(setup):
    path, service = setup
    (path.parent / "review-needed.md").write_text("PRIVATE REVIEW NOTES")
    (path.parent / "source-index.md").write_text("PRIVATE EVIDENCE")
    receipt = run(path, service)
    assert receipt["verification"]["content_and_mode"] == "passed", receipt
    assert receipt["verification"]["visual"] == "unverified"
    assert "PRIVATE" not in service.payload
    assert service.created == 1 and service.styled >= 1
    calls = service.styled
    retry = run(path, service)
    assert retry["document_id"] == "doc-1"
    assert service.created == 1 and service.styled == calls


def test_dry_run_and_cancel_have_no_side_effects(setup, monkeypatch):
    path, service = setup
    workflow.run(path, folder=None, account=None, dry_run=True,
                 service_factory=lambda _: pytest.fail("No Google in dry run"))
    monkeypatch.setattr(workflow, "word", lambda *args: "cancel")
    assert run(path, service) is None
    assert service.created == 0 and not (path.parent / ".wcfi-google").exists()


@pytest.mark.parametrize("change", ["artifact", "access", "notes"])
def test_stale_preview_refused(setup, monkeypatch, change):
    path, service = setup
    def changed(*_):
        if change == "artifact":
            path.write_text(TEXT.replace("Not captured.", "Edited."), encoding="utf-8")
        elif change == "access":
            service.ctx["inherited_access"].append({"type": "anyone", "role": "reader"})
        else:
            (path.parent / "review-needed.md").write_text("Changed review notes")
        return "start"
    monkeypatch.setattr(workflow, "word", changed)
    with pytest.raises(GoogleError, match="changed after preview"):
        run(path, service)
    assert service.created == 0


def test_uncertain_creation_recovers_read_only(setup):
    path, service = setup
    service.timeout = True
    with pytest.raises(GoogleError, match="uncertain"):
        run(path, service)
    assert service.created == 1
    result = run(path, service)
    assert result["document_id"] == "doc-1"
    assert result["verification"]["content_and_mode"] == "failed"
    assert service.created == 1 and service.styled == 0


def test_missing_remote_after_uncertainty_never_recreates(setup):
    path, service = setup
    service.timeout = True
    with pytest.raises(GoogleError):
        run(path, service)
    service.remote = []
    with pytest.raises(GoogleError, match="unresolved"):
        run(path, service)
    assert service.created == 1


def test_intent_write_failure_prevents_create(setup, monkeypatch):
    path, service = setup
    monkeypatch.setattr(workflow.mc, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(GoogleError, match="Receipt could not be saved"):
        run(path, service)
    assert service.created == 0


def test_receipt_failure_after_creation_reports_existing_link(setup, monkeypatch):
    path, service = setup
    real = workflow.mc.atomic_write
    def write(target, text):
        if json.loads(text).get("document_id"):
            raise OSError("disk full")
        real(target, text)
    monkeypatch.setattr(workflow.mc, "atomic_write", write)
    with pytest.raises(GoogleError, match="document/d/doc-1"):
        run(path, service)
    assert service.created == 1
    monkeypatch.setattr(workflow.mc, "atomic_write", real)
    run(path, service)
    assert service.created == 1


@pytest.mark.parametrize("change", ["duplicate", "renamed", "moved", "edited"])
def test_retry_does_not_overwrite_remote_changes(setup, change):
    path, service = setup
    run(path, service)
    old_styles = service.styled
    if change == "duplicate":
        service.remote.append(copy.deepcopy(service.remote[0]))
    elif change == "renamed":
        service.remote[0]["name"] = "User changed title"
    elif change == "moved":
        service.remote[0]["parents"] = ["elsewhere"]
    else:
        template.inspect_content(service.doc)[0][0][0]["paragraph"]["elements"][0]["textRun"]["content"] = "User changed text"
    if change == "edited":
        assert run(path, service)["verification"]["content_and_mode"] == "failed"
    else:
        with pytest.raises(GoogleError):
            run(path, service)
    assert service.created == 1 and service.styled == old_styles


def test_native_mode_style_and_table_checks(setup):
    path, service = setup
    artifact = sa.load(path)
    apply_requests(service.doc, template.style_requests(artifact, service.doc))
    assert template.verify(artifact, service.doc)["content_and_mode"] == "passed"
    service.doc["documentStyle"] = {}
    assert "pageless" in " ".join(template.verify(artifact, service.doc)["errors"])
    service.doc["documentStyle"] = {"documentFormat": {"documentMode": "PAGELESS"}}
    template.inspect_content(service.doc)[1][0][0]["table"]["tableRows"][0]["tableCells"][0]["tableCellStyle"] = {}
    assert "gray" in " ".join(template.verify(artifact, service.doc)["errors"])


def test_inline_styles_unicode_and_long_tables(tmp_path):
    text = TEXT.replace("Not captured.", "Emoji 😀 **bold** and *italic* with `code` and [link](https://example.com).", 1)
    rows = "".join(f"| Person {i} | Long task description with explicit detail {i} | Unknown | Proposed |\n" for i in range(80))
    text = text.replace("| Owner | Task Description | Due Date | Status |\n|---|---|---|---|", "| Owner | Task Description | Due Date | Status |\n|---|---|---|---|\n" + rows)
    artifact = sa.parse_text(text, tmp_path / "long.wcfi.md")
    document = native_document(artifact)
    requests = template.style_requests(artifact, document)
    assert len(requests) > 100
    apply_requests(document, requests)
    assert template.verify(artifact, document)["content_and_mode"] == "passed"
    html = template.render(artifact)
    assert "Person 79" in html and "text-align:center" in html
    assert "page-break" not in html and "<script" not in html


def test_cli_offline_commands(setup, monkeypatch):
    path, _ = setup
    runner = CliRunner()
    result = runner.invoke(app, ["create", "google-doc", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, ["setup", "google", "--help"]).exit_code == 0
    assert runner.invoke(app, ["setup", "--help"]).exit_code == 0


@pytest.mark.parametrize("fault", ["account", "noninteractive", "protected"])
def test_unapproved_destination_stops(setup, monkeypatch, fault):
    path, service = setup
    if fault == "account":
        service.ctx["account"]["emailAddress"] = "someone-else@example.com"
    elif fault == "noninteractive":
        monkeypatch.setattr(workflow, "interactive", lambda: False)
    else:
        (path.parent / ".git").mkdir()
    with pytest.raises((GoogleError, ValueError)):
        run(path, service)
    assert service.created == 0


def test_stored_credentials_cannot_invent_granted_scopes(monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    from wcfi_tools.creation import google
    monkeypatch.setattr(google.config, "get_secret", lambda _: json.dumps({"token_uri": google.TOKEN_URI, "scopes": []}))
    with pytest.raises(GoogleError, match="lacks required scopes"):
        google.credentials_for("test@example.com")


def test_read_failure_keeps_existing_link(setup, monkeypatch):
    path, service = setup
    monkeypatch.setattr(service, "read", lambda _: (_ for _ in ()).throw(OSError("connection")))
    with pytest.raises(GoogleError, match="document/d/doc-1"):
        run(path, service)
    assert service.created == 1


def test_v2_design_and_style_reset_regression(tmp_path):
    artifact = sa.parse_text(TEXT.replace("template_version: 1", "template_version: 2"), tmp_path / "new.wcfi.md")
    doc = native_document(artifact)
    requests = template.style_requests(artifact, doc)
    text_start = next(i for i, r in enumerate(requests) if "updateTextStyle" in r)
    assert all("updateTextStyle" in r for r in requests[text_start:])
    apply_requests(doc, requests)
    # The fake resets fonts on every named-style change, reproducing the observed live bug.
    p = template.inspect_content(doc)[0][0][0]["paragraph"]
    assert p["paragraphStyle"]["namedStyleType"] == "TITLE"
    assert p["elements"][0]["textRun"]["textStyle"]["fontSize"]["magnitude"] == 24
    assert p["elements"][0]["textRun"]["textStyle"]["weightedFontFamily"]["fontFamily"] == "Inter"
    assert [c["width"]["magnitude"] for c in template.inspect_content(doc)[1][0][0]["table"]["tableStyle"]["tableColumnProperties"]] == [250, 250]
    errors = template.verify(artifact, doc)["errors"]
    assert errors == ["Native date chips were not confirmed"]
    preview = template.render(artifact)
    assert "<img" not in preview
    assert template.verify(artifact, doc)["cover"] == "unverified-requires-editor-review"
    assert "border-bottom:0.5pt" in preview
    assert "font-size:13pt" in preview
    assert template.fingerprint(artifact) != template.fingerprint(sa.parse_text(TEXT, tmp_path / "old.wcfi.md"))


def test_v2_date_requests_and_nested_list_groups(tmp_path):
    text = TEXT.replace("template_version: 1", "template_version: 2").replace("Not captured.", "1. Present\n   1. Example person\n   2. Another person", 1)
    artifact = sa.parse_text(text, tmp_path / "nested.wcfi.md")
    doc = native_document(artifact)
    requests = template.decoration_requests(artifact, doc)
    assert sum("insertDate" in r for r in requests) == 2
    assert all(r["insertDate"]["dateElementProperties"]["timestamp"] == "2026-10-11T00:00:00Z" for r in requests if "insertDate" in r)
    assert sum(r.get("insertText", {}).get("text") == "\t" for r in requests) == 2
    assert any(r.get("createParagraphBullets", {}).get("bulletPreset") == "NUMBERED_DECIMAL_ALPHA_ROMAN" for r in requests)
    style = template.style_requests(artifact, doc)
    nested = [r["updateParagraphStyle"]["paragraphStyle"] for r in style if "updateParagraphStyle" in r and r["updateParagraphStyle"]["paragraphStyle"]["indentStart"]["magnitude"] == 72]
    assert len(nested) == 2
    assert all(x["indentFirstLine"]["magnitude"] == 54 for x in nested)


def test_connector_flattened_and_google_tab_readback_agree(tmp_path):
    artifact = sa.parse_text(TEXT, tmp_path / "tabs.wcfi.md")
    raw = native_document(artifact)
    google = {"tabs": [{"tabProperties": {"tabId": "tab-x"}, "documentTab": copy.deepcopy(raw)}]}
    connector = {"tabs": [{"tabId": "tab-x", **copy.deepcopy(raw)}]}
    assert template.inspect_content(google) == template.inspect_content(connector)
    requests = template.style_requests(artifact, google)
    assert all(r["updateTextStyle"]["range"]["tabId"] == "tab-x" for r in requests if "updateTextStyle" in r)
    with pytest.raises(ValueError, match="one document tab"):
        template.body_content({"tabs": [{}, {}]})


def test_v2_full_creation_decorations_and_verification(tmp_path, monkeypatch):
    text = TEXT.replace("template_version: 1", "template_version: 2").replace(
        "Not captured.", "1. Present\n   1. Example person\n   2. Another person", 1)
    path = tmp_path / "SUMMARY.wcfi.md"
    path.write_text(text, encoding="utf-8")
    artifact = sa.load(path)
    service = Service(artifact)
    monkeypatch.setattr(workflow, "interactive", lambda: True)
    monkeypatch.setattr(workflow, "word", lambda *args: "start")
    monkeypatch.setattr(workflow, "say", lambda *args: None)
    receipt = workflow.run(path, folder="folder-1", account="test@example.com", service_factory=lambda _: service)
    assert receipt["verification"]["content_and_mode"] == "passed", receipt["verification"]
    assert sum("dateElement" in part for e, _ in template.inspect_content(service.doc)[0] for part in e["paragraph"]["elements"]) == 2
    broken = copy.deepcopy(service.doc)
    assert receipt["verification"]["cover"] == "unverified-requires-editor-review"
    assert receipt["state"] == "needs-review"
    assert "<img" not in service.payload
    broken["inlineObjects"] = {"old-banner": {"inlineObjectProperties": {
        "embeddedObject": {"imageProperties": {"contentUri": "fixture"}}}}}
    assert "Unexpected inline image; the template requires a native cover" in template.verify(artifact, broken)["errors"]
    broken = copy.deepcopy(service.doc)
    chip = next(part["dateElement"] for e, _ in template.inspect_content(broken)[0] for part in e["paragraph"]["elements"] if "dateElement" in part)
    chip["textStyle"]["fontSize"]["magnitude"] = 9
    assert "Native text formatting differs from the template" in template.verify(artifact, broken)["errors"]
