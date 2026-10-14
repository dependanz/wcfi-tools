"""Reviewed creation with durable intent and conservative retry reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from ..meeting import configuration as mc
from ..meeting import summary_artifact as artifact_io
from ..meeting.guided import ask, interactive, say, word
from ..private_workspace import require_private_output
from . import template
from .google import GoogleError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def lock(path):
    if path.is_symlink():
        raise GoogleError("Receipt lock cannot be a link")
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise GoogleError("This creation is already running in another process") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _save(path, receipt):
    try:
        mc.atomic_write(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        link = receipt.get("url", "No document identity received; reconcile the operation before retrying")
        raise GoogleError(f"Receipt could not be saved. Existing result: {link}; operation {receipt['operation']}") from exc


def _review_evidence(artifact):
    """Read locally only. The rendered payload never receives these bytes."""
    for warning in artifact.warnings:
        say("Review: " + warning)
    path = artifact.metadata.get("evidence", {}).get("review_notes")
    if path:
        target = (artifact.path.parent / path).resolve()
        if target.is_file():
            if target.stat().st_size > 1_000_000:
                raise GoogleError(f"Review notes are large; inspect {target} separately before creation")
            say("\nLocal review notes (not uploaded):\n" + target.read_text(encoding="utf-8-sig"))


def _evidence_hashes(artifact):
    result = {}
    for key, relative in artifact.metadata.get("evidence", {}).items():
        target = (artifact.path.parent / relative).resolve()
        if target.is_file():
            with target.open("rb") as stream:
                result[key] = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            result[key] = None
    return result


def run(path: Path | None, *, folder: str | None, account: str | None,
        dry_run: bool = False, preview: Path | None = None, service_factory=None):
    if path is None:
        if dry_run:
            raise GoogleError("Supply one artifact path for --dry-run")
        path = Path(ask("Local *.wcfi.md artifact"))
    artifact = artifact_io.load(path.expanduser())
    payload = template.render(artifact)
    say(f"Validated: {artifact.path}\nTitle: {artifact.metadata['title']}\n"
        f"Template: meeting_minutes_pageless v{template.spec_for(artifact)['version']}; pageless; Inter.\n"
        + ("Includes date chips. Native cover requires an editor step; no inline substitute is inserted. Verified person/file links are not inferred." if template.spec_for(artifact)["version"] == 2 else
           "Template uses ordinary text in place of smart chips and has no banner image."))
    say("\nCurrent reviewed body:\n" + artifact.body)
    _review_evidence(artifact)
    evidence_hashes = _evidence_hashes(artifact)
    if preview:
        preview = require_private_output(preview)
        if preview.exists() or preview.is_symlink() or preview.suffix.lower() != ".html":
            raise GoogleError("Preview must be a new .html file outside Git worktrees")
        mc.atomic_write(preview, payload)
        say(f"Local HTML preview: {preview} (native Google layout is not yet verified)")
    if dry_run:
        say("Dry run: no Google connection, receipt, upload or remote creation.")
        return None
    if not interactive():
        raise GoogleError("Interactive terminal required for reviewed Google creation")
    if not account:
        from .. import config
        account = config.load_config().get("google", {}).get("account") or ask("Google account email (run wcfi setup google first)")
    folder = folder or ask("Destination Google Drive folder ID")
    if not folder or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in folder):
        raise GoogleError("Supply a Drive folder ID, not a URL")
    if service_factory is None:
        from .google import GoogleService, credentials_for
        def service_factory(value):
            return GoogleService(credentials_for(value))
    service = service_factory(account)
    context = service.context(folder)
    if context["account"]["emailAddress"].casefold() != account.casefold():
        raise GoogleError("Authenticated Google account differs from the selected account")
    binding = {"artifact": artifact.sha256, "template": template.fingerprint(artifact),
               "payload": hashlib.sha256(payload.encode()).hexdigest(), "account": context["account"],
               "folder": context["folder"]["id"], "title": artifact.metadata["title"]}
    operation = digest(binding)
    receipt_dir = require_private_output(artifact.path.parent / ".wcfi-google")
    if not receipt_dir.is_relative_to(artifact.path.parent):
        raise GoogleError("Receipt directory escapes the artifact directory")
    receipt_path = receipt_dir / (operation + ".json")
    if receipt_path.is_symlink() or (receipt_path.exists() and not receipt_path.is_file()):
        raise GoogleError("Receipt must be a regular local file")
    old_receipt = receipt_path.read_bytes() if receipt_path.exists() else None
    say("\nGoogle creation preview:\n" + json.dumps(context, indent=2, ensure_ascii=False))
    say("Transfer: current body text/tables/links, document title, template formatting and selected template assets. "
        "Local source index, review notes, recordings and transcripts are excluded. "
        "The document inherits the displayed folder access; no sharing changes are made.")
    say(f"Operation: {operation}\nReceipt: {receipt_path}\n"
        + ("Reconcile existing operation; never create another copy blindly." if old_receipt else "Create one new Google document."))
    if word('Type "start" to create/reconcile the Google Doc, or "cancel" to exit', ("start", "cancel")) != "start":
        return None
    if (artifact_io.load(artifact.path).sha256 != artifact.sha256
            or _evidence_hashes(artifact) != evidence_hashes or service.context(folder) != context):
        raise GoogleError("Artifact/account/destination/access changed after preview; review again")
    if require_private_output(artifact.path.parent / ".wcfi-google") != receipt_dir:
        raise GoogleError("Receipt directory changed after preview")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with lock(receipt_dir / (operation + ".lock")):
        if receipt_path.is_symlink():
            raise GoogleError("Receipt became a link after preview")
        current = receipt_path.read_bytes() if receipt_path.exists() else None
        if current != old_receipt:
            raise GoogleError("Receipt changed after preview; review again")
        if current:
            receipt = json.loads(current)
            if receipt.get("operation") != operation or receipt.get("binding") != binding:
                raise GoogleError("Receipt identity is invalid; manual reconciliation required")
        else:
            receipt = {"version": 1, "operation": operation, "binding": binding, "state": "intent"}
        # Lookup also protects against a missing local receipt when remote creation succeeded.
        found = service.find_operation(operation)
        if len(found) > 1:
            raise GoogleError("Multiple remote matches; manual reconciliation required")
        is_new = False
        if found:
            remote = found[0]
            if (remote.get("name") != binding["title"] or binding["folder"] not in remote.get("parents", [])
                    or remote.get("appProperties", {}).get("wcfi_operation") != operation):
                raise GoogleError("Existing remote operation changed; manual review required")
            if receipt.get("document_id") not in (None, remote["id"]):
                raise GoogleError("Remote identity differs from the saved receipt")
            receipt.update(document_id=remote["id"], url=f"https://docs.google.com/document/d/{remote['id']}/edit")
        elif current:
            raise GoogleError("Prior outcome is unresolved; no matching document is visible yet. No duplicate was created. Retry reconciliation or inspect Drive manually.")
        else:
            receipt["state"] = "creating"
            _save(receipt_path, receipt)  # Durable intent must precede the remote side effect.
            try:
                remote = service.create(binding["title"], binding["folder"], payload, operation)
            except Exception as exc:
                raise GoogleError(f"Creation outcome is uncertain. Keep {receipt_path}; rerun this exact command to reconcile, not create blindly.") from exc
            receipt.update(document_id=remote["id"], url=f"https://docs.google.com/document/d/{remote['id']}/edit", state="created")
            _save(receipt_path, receipt)
            is_new = True
        say("Existing draft: " + receipt["url"])
        try:
            document = service.read(receipt["document_id"])
        except Exception as exc:
            raise GoogleError("Readback failed; existing draft retained: " + receipt["url"]) from exc
        if is_new:
            receipt["state"] = "formatting"
            _save(receipt_path, receipt)
            decorations = template.decoration_requests(artifact, document)
            if decorations:
                revision = document.get("revisionId")
                if not revision:
                    raise GoogleError("Native revision unavailable; existing draft needs review: " + receipt["url"])
                try:
                    service.style(receipt["document_id"], decorations, revision)
                    document = service.read(receipt["document_id"])
                except Exception as exc:
                    raise GoogleError("Decoration/readback outcome uncertain; existing draft retained: " + receipt["url"]) from exc
            requests = template.style_requests(artifact, document)
            for start in range(0, len(requests), 100):
                revision = document.get("revisionId")
                if not revision:
                    raise GoogleError("Native revision unavailable; existing draft needs review: " + receipt["url"])
                try:
                    service.style(receipt["document_id"], requests[start:start + 100], revision)
                    document = service.read(receipt["document_id"])
                except Exception as exc:
                    raise GoogleError("Formatting/readback outcome uncertain; existing draft retained: " + receipt["url"]) from exc
        # Reconciled drafts are read-only here, so user edits are never overwritten on retry.
        receipt["verification"] = template.verify(artifact, document)
        receipt["state"] = "needs-review"
        _save(receipt_path, receipt)
        say("Readback: " + json.dumps(receipt["verification"]))
        if template.spec_for(artifact)["version"] == 2:
            say("Cover still requires editor review: open the draft, then Insert > Cover image > "
                "Choose from stock images, or Upload from computer for the reference cloud artwork. "
                "The CLI cannot create or verify this native cover through its current API adapter.")
        say("Draft retained for review: " + receipt["url"] + "; native visual layout remains unverified.")
        return receipt
