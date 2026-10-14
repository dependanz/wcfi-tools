"""Versioned pageless meeting template. HTML is an import/preview, not a PDF layout."""
from __future__ import annotations

import copy
from datetime import date, datetime
from importlib.resources import files
import hashlib
import html
import json
import re

from ..meeting.summary_artifact import MD, Artifact, inline_text

SPEC = {"id": "meeting_minutes_pageless", "version": 1, "layout": "pageless",
        "font": "Inter", "body_pt": 11, "title_pt": 24, "heading_pt": 16,
        "line_spacing": 115, "banner": "none", "chips": "ordinary_text",
        "header_gray": 0.94, "implementation": 2}
FINGERPRINT = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()


def _render_v1(artifact: Artifact) -> str:
    tokens = copy.deepcopy(artifact.tokens)
    styles = {"h1": "font-family:Inter,Arial,sans-serif;font-size:24pt;line-height:1.2;font-weight:bold;text-align:center;margin:5pt 0 0;",
              "h2": "font-family:Inter,Arial,sans-serif;font-size:16pt;line-height:1.2;font-weight:bold;margin:24pt 0 6pt;",
              "h3": "font-family:Inter,Arial,sans-serif;font-size:13pt;line-height:1.2;font-weight:bold;margin:12pt 0 6pt;",
              "p": "font-family:Inter,Arial,sans-serif;font-size:11pt;line-height:1.15;margin:0 0 6pt;",
              "table": "border-collapse:collapse;width:100%;table-layout:fixed;margin:6pt 0 12pt;",
              "th": "background-color:#f0f0f0;font-weight:bold;border:1px solid #cccccc;padding:6pt;text-align:left;",
              "td": "border:1px solid #cccccc;padding:6pt;vertical-align:top;overflow-wrap:anywhere;"}
    before_sections = True
    table_index, column_index = -1, 0
    widths = ((32, 68), (50, 18, 16, 16), (18, 48, 18, 16))
    for i, token in enumerate(tokens):
        if token.type == "table_open":
            table_index += 1
        elif token.type == "tr_open":
            column_index = 0
        if token.type == "heading_open" and token.tag == "h2":
            before_sections = False
        if token.nesting == 1 and token.tag in styles:
            style = styles[token.tag]
            if token.tag == "h2" and inline_text(tokens[i + 1]) == "Metadata":
                style = style.replace("24pt 0 6pt", "5pt 0")
            if token.tag == "p" and before_sections:
                style += "text-align:center;"
            if token.tag in {"th", "td"}:
                style += f"width:{widths[table_index][column_index]}%;"
                column_index += 1
            token.attrSet("style", style)
    body = MD.renderer.render(tokens, MD.options, {})
    title = html.escape(artifact.metadata["title"])
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            f"<title>{title}</title></head>"
            '<body style="font-family:Inter,Arial,sans-serif;font-size:11pt;line-height:1.15;'
            'max-width:1000px;margin:32px auto;padding:0 24px;overflow-wrap:anywhere;">'
            + body + "</body></html>")


def expected_content(artifact: Artifact):
    text = [inline_text(t) for t in artifact.tokens if t.type == "inline"]
    tables, rows, cells = [], None, None
    for token in artifact.tokens:
        if token.type == "table_open":
            rows = []
        elif token.type == "tr_open":
            cells = []
        elif token.type == "inline" and rows is not None and cells is not None:
            cells.append(inline_text(token))
        elif token.type == "tr_close":
            rows.append(cells)
            cells = None
        elif token.type == "table_close":
            tables.append(rows)
            rows = None
    return text, tables


def normal(value):
    return re.sub(r"\s+", " ", value).strip()


def body_content(document):
    tabs = document.get("tabs", [])
    if tabs:
        if len(tabs) != 1 or tabs[0].get("childTabs"):
            raise ValueError("Expected one document tab")
        return tabs[0].get("documentTab", tabs[0]).get("body", {}).get("content", [])
    return document.get("body", {}).get("content", [])


def inspect_content(document):
    paragraphs, tables = [], []
    def walk(elements):
        texts = []
        for element in elements:
            if "paragraph" in element:
                paragraph = element["paragraph"]
                text = "".join(_element_text(x) for x in paragraph.get("elements", []))
                if normal(text):
                    paragraphs.append((element, text))
                    texts.append(text)
            if "table" in element:
                grid = []
                for row in element["table"]["tableRows"]:
                    grid.append([" ".join(walk(cell.get("content", []))) for cell in row["tableCells"]])
                tables.append((element, grid))
        return texts
    walk(body_content(document))
    return paragraphs, tables


def paragraph_specs(artifact):
    specs, header, before_sections = [], False, True
    for i, token in enumerate(artifact.tokens):
        if token.type == "thead_open":
            header = True
        elif token.type == "thead_close":
            header = False
        elif token.type == "heading_open" and token.tag == "h2":
            before_sections = False
        elif token.type == "inline" and normal(inline_text(token)):
            previous = artifact.tokens[i - 1]
            kind = previous.tag if previous.type == "heading_open" else "p"
            specs.append((token, kind, header, before_sections))
    return specs


def _length(text):
    return len(text.encode("utf-16-le")) // 2


def _base_style_requests(artifact, document):
    """Match reviewed paragraphs before styling returned native UTF-16 indices."""
    paragraphs, tables = inspect_content(document)
    specs = paragraph_specs(artifact)
    if len(paragraphs) != len(specs) or any(normal(text) != normal(inline_text(spec[0]))
                                        for (_, text), spec in zip(paragraphs, specs)):
        raise ValueError("Native paragraph structure differs from the artifact; draft requires review")
    requests = [{"updateDocumentStyle": {"documentStyle": {"documentFormat": {"documentMode": "PAGELESS"}},
                                         "fields": "documentFormat.documentMode"}}]
    for (element, text), (token, kind, header, before_sections) in zip(paragraphs, specs):
        start, end = element["startIndex"], element["endIndex"] - 1
        scope = {"startIndex": start, "endIndex": end}
        size = {"h1": 24, "h2": 16, "h3": 13}.get(kind, 11)
        style = {"weightedFontFamily": {"fontFamily": "Inter"}, "fontSize": {"magnitude": size, "unit": "PT"},
                 "bold": header or kind != "p", "italic": False}
        requests.append({"updateTextStyle": {"range": scope, "textStyle": style, "fields": ",".join(style)}})
        para = {"lineSpacing": 115, "alignment": "CENTER" if kind == "h1" or before_sections else "START",
                "namedStyleType": {"h1": "HEADING_1", "h2": "HEADING_2", "h3": "HEADING_3"}.get(kind, "NORMAL_TEXT")}
        if kind == "h1":
            para.update(spaceAbove={"magnitude": 5, "unit": "PT"}, spaceBelow={"magnitude": 0, "unit": "PT"})
        elif kind == "h2":
            para.update(spaceAbove={"magnitude": 5 if normal(text) == "Metadata" else 24, "unit": "PT"},
                        spaceBelow={"magnitude": 5 if normal(text) == "Metadata" else 6, "unit": "PT"})
        requests.append({"updateParagraphStyle": {"range": scope, "paragraphStyle": para, "fields": ",".join(para)}})
        if any("dateElement" in part for part in element["paragraph"].get("elements", [])):
            continue
        position, active = 0, {}
        for child in token.children or []:
            if child.type in {"strong_open", "em_open", "link_open"}:
                active[{"strong_open": "bold", "em_open": "italic", "link_open": "link"}[child.type]] = (
                    {"url": child.attrGet("href")} if child.type == "link_open" else True)
            elif child.type in {"strong_close", "em_close", "link_close"}:
                active.pop({"strong_close": "bold", "em_close": "italic", "link_close": "link"}[child.type], None)
            elif child.type in {"text", "code_inline", "softbreak", "hardbreak"}:
                value = " " if child.type.endswith("break") else child.content
                pattern = r"\s+".join(re.escape(part) for part in re.split(r"\s+", value))
                match = re.match(pattern, text[position:])
                if not match:
                    raise ValueError("Native inline text differs from the artifact; draft requires review")
                stop = position + match.end()
                inline_style = dict(active)
                if child.type == "code_inline":
                    inline_style["weightedFontFamily"] = {"fontFamily": "Courier New"}
                if inline_style and stop > position:
                    requests.append({"updateTextStyle": {"range": {"startIndex": start + _length(text[:position]),
                                                                  "endIndex": start + _length(text[:stop])},
                                                       "textStyle": inline_style, "fields": ",".join(inline_style)}})
                position = stop
    for element, grid in tables:
        requests.append({"updateTableCellStyle": {
            "tableRange": {"tableCellLocation": {"tableStartLocation": {"index": element["startIndex"]},
                                                "rowIndex": 0, "columnIndex": 0},
                           "rowSpan": 1, "columnSpan": len(grid[0])},
            "tableCellStyle": {"backgroundColor": {"color": {"rgbColor": {"red": 0.94, "green": 0.94, "blue": 0.94}}}},
            "fields": "backgroundColor"}})
    return requests


def _contains(actual, expected):
    if isinstance(expected, dict):
        if "rgbColor" in expected and isinstance(actual, dict):
            rgb, wanted = actual.get("rgbColor", {}), expected["rgbColor"]
            return all(abs(rgb.get(c, 0) - wanted.get(c, 0)) <= 1/255 for c in ("red", "green", "blue"))
        return isinstance(actual, dict) and all(_contains(actual.get(key, 0 if value == 0 else None), value)
                                               for key, value in expected.items())
    return actual == expected


def _base_verify(artifact: Artifact, document: dict):
    expected, grids = expected_content(artifact)
    paragraphs, tables = inspect_content(document)
    errors = []
    if normal(" ".join(expected)) != normal(" ".join(text for _, text in paragraphs)):
        errors.append("Native document text differs from the reviewed artifact")
    actual_grids = [[[normal(cell) for cell in row] for row in grid] for _, grid in tables]
    if actual_grids != [[[normal(cell) for cell in row] for row in grid] for grid in grids]:
        errors.append("Native tables differ from the reviewed table content")
    if _document_tab(document).get("documentStyle", document.get("documentStyle", {})).get("documentFormat", {}).get("documentMode") != "PAGELESS":
        errors.append("Native pageless mode was not confirmed")
    try:
        requests = style_requests(artifact, document)
        # Compare each character run against the final (base plus inline) explicit style.
        for element, _ in paragraphs:
            for part in element["paragraph"].get("elements", []):
                run = part.get("textRun", part.get("dateElement", {}))
                if "dateElement" not in part and not normal(run.get("content", "")):
                    continue
                start, end = part["startIndex"], part["endIndex"]
                boundaries = {start, end}
                for request in requests:
                    update = request.get("updateTextStyle")
                    if update:
                        boundaries.update(v for k, v in update["range"].items() if k != "tabId" and start < v < end)
                for left, right in zip(sorted(boundaries), sorted(boundaries)[1:]):
                    expected_style = {}
                    for request in requests:
                        update = request.get("updateTextStyle")
                        if update and update["range"]["startIndex"] <= left and update["range"]["endIndex"] >= right:
                            expected_style.update(update["textStyle"])
                    actual = _effective_text_style(document, element, run.get("textStyle", {}))
                    if any(not _contains(actual.get(key, False if key in {"bold", "italic", "underline", "strikethrough"} else None), value) for key, value in expected_style.items()):
                        errors.append("Native text formatting differs from the template")
        for request in requests:
            update = request.get("updateParagraphStyle")
            if update:
                element = next(e for e in [p for p, _ in paragraphs] + body_content(document)
                               if "paragraph" in e and e["startIndex"] == update["range"]["startIndex"])
                actual = element["paragraph"].get("paragraphStyle", {})
                if any(not _contains(actual.get(key, False if value is False else None), value) for key, value in update["paragraphStyle"].items()):
                    errors.append("Native heading/date/paragraph formatting differs from the template")
        for element, _ in tables:
            for cell in element["table"]["tableRows"][0]["tableCells"]:
                rgb = cell.get("tableCellStyle", {}).get("backgroundColor", {}).get("color", {}).get("rgbColor", {})
                if any(abs(rgb.get(channel, 0) - spec_for(artifact)["header_gray"]) > 0.005 for channel in ("red", "green", "blue")):
                    errors.append("Native gray table headers were not confirmed")
    except ValueError as exc:
        errors.append(str(exc))
    return {"content_and_mode": "passed" if not errors else "failed",
            "visual": "unverified", "status": "needs-review", "errors": list(dict.fromkeys(errors))}


# Version 1 remains explicitly selectable. Version 2 reproduces the July reference.
SPECS = {
    1: SPEC,
    2: {**SPEC, "version": 2, "body_pt": 13, "banner": "none", "cover": "native", "cover_asset": "cloud-banner.png",
        "chips": "date", "header_gray": 0.95, "implementation": 2,
        "table_widths_pt": ((250, 250), (175, 175, 175, 175),
                            (119.25, 174.75, 174.75, 174.75)),
        "table_rule_gray": 0.5, "table_rule_pt": 0.5},
}


def spec_for(artifact):
    return SPECS[artifact.metadata["presentation"]["template_version"]]


def banner_bytes():
    return files(__package__).joinpath("assets/cloud-banner.png").read_bytes()


def fingerprint(artifact):
    spec = dict(spec_for(artifact))
    if spec["version"] == 2:
        spec["cover_asset_sha256"] = hashlib.sha256(banner_bytes()).hexdigest()
    # Ordering/readback changes also invalidate an earlier renderer's operation.
    return hashlib.sha256(json.dumps({"spec": spec, "renderer": 4}, sort_keys=True).encode()).hexdigest()


def _document_tab(document):
    tabs = document.get("tabs", [])
    if not tabs:
        return document
    if len(tabs) != 1 or tabs[0].get("childTabs"):
        raise ValueError("Expected one document tab")
    return tabs[0].get("documentTab", tabs[0])


def _tab_id(document):
    tabs = document.get("tabs", [])
    if not tabs:
        return None
    return tabs[0].get("tabProperties", {}).get("tabId") or tabs[0].get("tabId")


def _full_date(value):
    day = date.fromisoformat(value)
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def _element_text(element):
    if "textRun" in element:
        return element["textRun"].get("content", "")
    if "dateElement" in element:
        properties = element["dateElement"].get("dateElementProperties", {})
        stamp = properties.get("timestamp", "")
        try:
            return _full_date(datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat())
        except (ValueError, TypeError):
            return properties.get("displayText", "")
    return ""


def _effective_text_style(document, element, explicit):
    styles = {s["namedStyleType"]: s.get("textStyle", {}) for s in
              _document_tab(document).get("namedStyles", document.get("namedStyles", {})).get("styles", [])}
    kind = element["paragraph"].get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
    return {**styles.get("NORMAL_TEXT", {}), **styles.get(kind, {}), **explicit}


def _details(artifact):
    """Keep list grouping/depth and table role alongside each reviewed paragraph."""
    output, stack, group, counter, table, header, before = [], [], None, 0, -1, False, True
    in_table = False
    for i, token in enumerate(artifact.tokens):
        if token.type in {"ordered_list_open", "bullet_list_open"}:
            if not stack:
                counter += 1
                group = counter
            stack.append(token.type == "ordered_list_open")
        elif token.type in {"ordered_list_close", "bullet_list_close"}:
            stack.pop()
        elif token.type == "table_open":
            table += 1
            in_table = True
        elif token.type == "table_close":
            in_table = False
        elif token.type == "thead_open":
            header = True
        elif token.type == "thead_close":
            header = False
        elif token.type == "heading_open" and token.tag == "h2":
            before = False
        elif token.type == "inline" and normal(inline_text(token)):
            previous = artifact.tokens[i - 1]
            output.append({"token": token, "kind": previous.tag if previous.type == "heading_open" else "p",
                           "before": before, "header": header, "table": table if in_table else None,
                           "depth": len(stack) - 1, "ordered": stack[0] if stack else False,
                           "group": group if stack else None})
    return output


def render(artifact):
    if spec_for(artifact)["version"] == 1:
        return _render_v1(artifact)
    tokens = copy.deepcopy(artifact.tokens)
    widths = SPECS[2]["table_widths_pt"]
    before, table, col = True, -1, 0
    full_date = _full_date(artifact.metadata["meeting_date"]) if artifact.metadata["meeting_date"] != "unknown" else None
    for i, token in enumerate(tokens):
        if token.type == "table_open":
            table += 1
        elif token.type == "tr_open":
            col = 0
        elif token.type == "heading_open" and token.tag == "h2":
            before = False
        if token.nesting != 1:
            continue
        styles = {
            "h1": "font-size:24pt;font-weight:bold;text-align:center;margin:10pt 0 0;",
            "h2": "font-size:16pt;font-weight:bold;margin:24pt 0 6pt;",
            "h3": "font-size:13pt;font-weight:bold;margin:12pt 0 6pt;",
            "p": "font-size:13pt;line-height:1.15;margin:0 0 6pt;",
            "li": "font-size:13pt;line-height:1.15;margin:0 0 3pt;",
            "ol": "padding-left:36pt;margin:0 0 6pt;list-style-type:decimal;" if token.level < 2 else "padding-left:36pt;margin:0;list-style-type:lower-alpha;",
            "ul": "padding-left:36pt;margin:0 0 6pt;",
            "table": f"border-collapse:collapse;table-layout:fixed;width:{sum(widths[max(table, 0)])}pt;max-width:100%;margin:6pt 0 12pt;",
            "th": "background:#f2f2f2;font-size:11pt;font-weight:normal;border:0;border-bottom:0.5pt solid #808080;padding:6pt;text-align:left;",
            "td": "font-size:13pt;border:0;border-bottom:0.5pt solid #808080;padding:6pt;vertical-align:top;overflow-wrap:anywhere;",
        }
        style = styles.get(token.tag, "")
        if token.tag == "h2" and inline_text(tokens[i+1]) == "Metadata":
            style = style.replace("24pt 0 6pt", "12pt 0 6pt")
            style += "border-top:0.5pt solid #808080;padding-top:12pt;"
        if token.tag == "p" and before:
            style += "text-align:center;"
            if inline_text(tokens[i+1]) == full_date:
                style = "font-size:16pt;font-weight:bold;text-align:center;margin:5pt 0 12pt;"
            else:
                style = style.replace("font-size:13pt", "font-size:11pt")
        if token.tag in {"th", "td"}:
            style += f"width:{widths[table][col]/sum(widths[table])*100}%;"
            col += 1
        if style:
            token.attrSet("style", style)
    body = MD.renderer.render(tokens, MD.options, {})
    if full_date:
        body = body.replace(html.escape(full_date), '<span style="background:#e8eaed;border-radius:8px;padding:1px 5px;">' + html.escape(full_date) + "</span>")
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">'
            f'<title>{html.escape(artifact.metadata["title"])}</title></head>'
            '<body style="font-family:Inter,Arial,sans-serif;font-size:13pt;line-height:1.15;max-width:1000px;margin:32px auto;padding:0 24px;overflow-wrap:anywhere;">'
            + body + "</body></html>")


def decoration_requests(artifact, document):
    """Index-changing decorations precede a fresh read and final styling."""
    paragraphs, _ = inspect_content(document)
    details = _details(artifact)
    if len(paragraphs) != len(details) or any(normal(text) != normal(inline_text(d["token"]))
            for (_, text), d in zip(paragraphs, details)):
        raise ValueError("Native paragraph structure differs from the artifact; draft requires review")
    requests, groups = [], {}
    for (element, _), detail in zip(paragraphs, details):
        if detail["group"] is not None:
            groups.setdefault(detail["group"], []).append((element, detail))
    tab = _tab_id(document)
    def scope(start, end):
        return {"startIndex": start, "endIndex": end, **({"tabId": tab} if tab else {})}
    # Each group removes the tabs it inserts, so earlier document indices remain valid.
    for group in reversed(list(groups.values())):
        start, end = group[0][0]["startIndex"], group[-1][0]["endIndex"] - 1
        requests.append({"deleteParagraphBullets": {"range": scope(start, end)}})
        for element, detail in reversed(group):
            if detail["depth"]:
                requests.append({"insertText": {"location": {"index": element["startIndex"], **({"tabId": tab} if tab else {})},
                                                 "text": "\t" * detail["depth"]}})
        tabs = sum(detail["depth"] for _, detail in group)
        requests.append({"createParagraphBullets": {"range": scope(start, end + tabs),
                         "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN" if group[0][1]["ordered"] else "BULLET_DISC_CIRCLE_SQUARE"}})
    if spec_for(artifact)["version"] == 2 and artifact.metadata["meeting_date"] != "unknown":
        canonical = _full_date(artifact.metadata["meeting_date"])
        for element, text in reversed(paragraphs):
            parts = element["paragraph"].get("elements", [])
            if normal(text) == canonical and not any("dateElement" in x for x in parts):
                start, end = element["startIndex"], element["endIndex"] - 1
                requests.extend([
                    {"deleteContentRange": {"range": scope(start, end)}},
                    {"insertDate": {"location": {"index": start, **({"tabId": tab} if tab else {})},
                                    "dateElementProperties": {"timestamp": artifact.metadata["meeting_date"] + "T00:00:00Z",
                                       "locale": "en", "dateFormat": "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED",
                                       "timeFormat": "TIME_FORMAT_DISABLED"}}}])
    return requests


def style_requests(artifact, document):
    requests = _base_style_requests(artifact, document)
    spec = spec_for(artifact)
    paragraphs, tables = inspect_content(document)
    details = _details(artifact)
    by_index = {element["startIndex"]: d for (element, _), d in zip(paragraphs, details)}
    canonical = _full_date(artifact.metadata["meeting_date"]) if artifact.metadata["meeting_date"] != "unknown" else None
    for r in requests:
        if "updateTextStyle" in r:
            u = r["updateTextStyle"]
            if "fontSize" in u["textStyle"]:
                d = by_index[u["range"]["startIndex"]]
                size = {"h1": 24, "h2": 16, "h3": 13}.get(d["kind"], spec["body_pt"])
                is_date = d["before"] and inline_text(d["token"]) == canonical
                if spec["version"] == 2 and (d["header"] or (d["before"] and d["kind"] == "p")):
                    size = 16 if is_date else 11
                u["textStyle"].update(fontSize={"magnitude": size, "unit": "PT"},
                    underline=False, strikethrough=False, foregroundColor={"color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}})
                if spec["version"] == 2:
                    u["textStyle"]["bold"] = d["kind"] != "p" or is_date
                u["fields"] = ",".join(u["textStyle"])
        elif "updateParagraphStyle" in r:
            u = r["updateParagraphStyle"]
            d = by_index[u["range"]["startIndex"]]
            para = u["paragraphStyle"]
            para["namedStyleType"] = {"h1": "TITLE", "h2": "HEADING_1", "h3": "HEADING_2"}.get(d["kind"], "NORMAL_TEXT")
            para.update(indentStart={"magnitude": 0, "unit": "PT"}, indentEnd={"magnitude": 0, "unit": "PT"},
                        indentFirstLine={"magnitude": 0, "unit": "PT"},
                        keepWithNext=d["kind"] != "p", keepLinesTogether=False,
                        spaceBelow=para.get("spaceBelow", {"magnitude": 6, "unit": "PT"}))
            if d["depth"] >= 0:
                para.update(indentStart={"magnitude": 36 * (d["depth"] + 1), "unit": "PT"},
                            indentFirstLine={"magnitude": 36 * d["depth"] + 18, "unit": "PT"},
                            spaceAbove={"magnitude": 0, "unit": "PT"},
                            spaceBelow={"magnitude": 3, "unit": "PT"})
            if spec["version"] == 2 and inline_text(d["token"]) == "Metadata":
                para.update(borderTop={"color": {"color": {"rgbColor": {"red": .5, "green": .5, "blue": .5}}},
                       "width": {"magnitude": .5, "unit": "PT"}, "padding": {"magnitude": 12, "unit": "PT"}, "dashStyle": "SOLID"},
                            spaceAbove={"magnitude": 12, "unit": "PT"})
            u["fields"] = ",".join(para)
        elif "updateTableCellStyle" in r:
            rgb = {"red": spec["header_gray"], "green": spec["header_gray"], "blue": spec["header_gray"]}
            r["updateTableCellStyle"]["tableCellStyle"]["backgroundColor"] = {"color": {"rgbColor": rgb}}
    if spec["version"] == 2:
        for number, (element, grid) in enumerate(tables):
            loc = {"index": element["startIndex"]}
            for column, width in enumerate(spec["table_widths_pt"][number]):
                requests.append({"updateTableColumnProperties": {"tableStartLocation": loc, "columnIndices": [column],
                    "tableColumnProperties": {"widthType": "FIXED_WIDTH", "width": {"magnitude": width, "unit": "PT"}},
                    "fields": "widthType,width"}})
            empty_border = {"color": {"color": {"rgbColor": {}}}, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID"}
            rule = {"color": {"color": {"rgbColor": {"red": .5, "green": .5, "blue": .5}}},
                    "width": {"magnitude": .5, "unit": "PT"}, "dashStyle": "SOLID"}
            style = {"borderTop": empty_border, "borderBottom": empty_border, "borderLeft": empty_border, "borderRight": empty_border,
                     "paddingTop": {"magnitude": 6, "unit": "PT"}, "paddingBottom": {"magnitude": 6, "unit": "PT"},
                     "paddingLeft": {"magnitude": 6, "unit": "PT"}, "paddingRight": {"magnitude": 6, "unit": "PT"}}
            requests.append({"updateTableCellStyle": {
                "tableRange": {"tableCellLocation": {"tableStartLocation": loc, "rowIndex": 0, "columnIndex": 0},
                               "rowSpan": len(grid), "columnSpan": len(grid[0])},
                "tableCellStyle": style, "fields": ",".join(style)}})
            # Shared edges: clearing the next row's top can erase the previous bottom.
            # Clear all borders first, then apply horizontal rules in a separate request.
            requests.append({"updateTableCellStyle": {
                "tableRange": {"tableCellLocation": {"tableStartLocation": loc, "rowIndex": 0, "columnIndex": 0},
                               "rowSpan": len(grid), "columnSpan": len(grid[0])},
                "tableCellStyle": {"borderBottom": rule}, "fields": "borderBottom"}})
            requests.append({"pinTableHeaderRows": {"tableStartLocation": loc, "pinnedHeaderRowsCount": 1}})
            requests.append({"updateTableRowStyle": {"tableStartLocation": loc, "tableRowStyle": {"preventOverflow": True}, "fields": "preventOverflow"}})
    tab = _tab_id(document)
    if tab:
        for r in requests:
            u = next(iter(r.values()))
            if "range" in u:
                u["range"]["tabId"] = tab
            if "tableStartLocation" in u:
                u["tableStartLocation"]["tabId"] = tab
            if "tableRange" in u:
                u["tableRange"]["tableCellLocation"]["tableStartLocation"]["tabId"] = tab
            if "updateDocumentStyle" in r:
                u["tabId"] = tab
    # Named paragraph styles reset explicit fonts. Apply all text styles LAST.
    return [r for r in requests if "updateTextStyle" not in r] + [r for r in requests if "updateTextStyle" in r]


def verify(artifact, document):
    result = _base_verify(artifact, document)
    errors = result["errors"]
    paragraphs, tables = inspect_content(document)
    details = _details(artifact)
    if len(paragraphs) == len(details):
        for (element, _), detail in zip(paragraphs, details):
            bullet = element["paragraph"].get("bullet")
            if detail["depth"] >= 0 and (not bullet or bullet.get("nestingLevel", 0) != detail["depth"]):
                errors.append("Native list nesting differs from the reviewed Markdown")
    if spec_for(artifact)["version"] == 2:
        tab = _document_tab(document)
        objects = tab.get("inlineObjects") or {}
        images = [x for x in objects.values() if x.get("inlineObjectProperties", {}).get("embeddedObject", {}).get("imageProperties")]
        if images:
            errors.append("Unexpected inline image; the template requires a native cover")
        # Public document readback does not expose cover-image state. A passing
        # body check must never claim that the native cover has been verified.
        result["cover"] = "unverified-requires-editor-review"
        expected_dates = sum(normal(inline_text(d["token"])) == _full_date(artifact.metadata["meeting_date"])
                             for d in details) if artifact.metadata["meeting_date"] != "unknown" else 0
        dates = sum("dateElement" in x for p, _ in paragraphs for x in p["paragraph"]["elements"])
        if dates != expected_dates:
            errors.append("Native date chips were not confirmed")
        for (element, _), widths in zip(tables, SPECS[2]["table_widths_pt"]):
            actual = element["table"].get("tableStyle", {}).get("tableColumnProperties", [])
            if [c.get("width", {}).get("magnitude") for c in actual] != list(widths):
                errors.append("Native table widths differ from the reference template")
            if not element["table"]["tableRows"][0].get("tableRowStyle", {}).get("tableHeader"):
                errors.append("Native table header repetition was not confirmed")
            for row in element["table"]["tableRows"]:
                if row.get("tableRowStyle", {}).get("preventOverflow") is not True:
                    errors.append("Native table rows may split across PDF pages")
                for cell in row["tableCells"]:
                    style = cell.get("tableCellStyle", {})
                    if (style.get("borderBottom", {}).get("width", {}).get("magnitude") != .5
                        or any(style.get("border" + side, {}).get("width", {}).get("magnitude", 0) != 0 for side in ("Left", "Right"))):
                        errors.append("Native table rules differ from the reference template")
    result["errors"] = list(dict.fromkeys(errors))
    result["content_and_mode"] = "failed" if errors else "passed"
    return result
