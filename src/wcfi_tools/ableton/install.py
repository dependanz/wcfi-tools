"""Install only our standalone Python package; never edit Live preferences."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .client import state_directory

PACKAGE_NAME = "WCFI_Bridge"
BOOTSTRAP_FILES = ("__init__.py", "surface.py", "runtime.py")
RUNTIME_FILES = ("adapter.py", "browser.py", "insertion_context.py", "server.py")
FILES = BOOTSTRAP_FILES + RUNTIME_FILES
REMOTE_DIRECTORY = Path(__file__).with_name("remote")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def find_user_library():
    if sys.platform == "win32":
        preferences = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Ableton"
    elif sys.platform == "darwin":
        preferences = Path.home() / "Library/Preferences/Ableton"
    else:
        raise RuntimeError("Provide --user-library explicitly on this platform")
    configs = sorted(preferences.glob("Live */Preferences/Library.cfg"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for path in configs:
        root = ET.fromstring(path.read_bytes())
        project = root.find("./ContentLibrary/UserLibrary/LibraryProject")
        if project is not None:
            parent = project.find("ProjectPath")
            name = project.find("ProjectName")
            if parent is not None and name is not None:
                candidate = Path(parent.get("Value", "")) / name.get("Value", "")
                if candidate.is_dir():
                    return candidate.absolute()
    fallback = Path.home() / "Documents/Ableton/User Library"
    if fallback.is_dir():
        return fallback.absolute()
    raise RuntimeError("Cannot locate Live's User Library. Supply --user-library with its exact folder.")


def _write(path, data):
    _plain_path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    created = False
    try:
        with temporary.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_path(path)
        temporary.replace(path)
        created = False
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _plain_path(path):
    """Reject symlinks and Windows junctions before following any component."""
    absolute = Path(path).absolute()
    for part in reversed((absolute, *absolute.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Refusing to install through a linked path: " + str(part))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    return json.dumps(data, sort_keys=True, indent=2).encode("utf-8")


def _bundle_id(hashes):
    return _digest(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _verify_bundle(directory, hashes):
    _plain_path(directory)
    expected_names = set(RUNTIME_FILES) | {"manifest.json"}
    if not directory.is_dir() or {entry.name for entry in directory.iterdir()} != expected_names:
        raise RuntimeError("Runtime bundle has missing or unexpected files: " + str(directory))
    for name in expected_names:
        _plain_path(directory / name)
        if not (directory / name).is_file():
            raise RuntimeError("Runtime bundle contains a non-file: " + name)
    expected_manifest = {"schema": 1, "bundle_id": _bundle_id(hashes), "files": hashes}
    try:
        actual_manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Runtime bundle manifest is invalid") from exc
    if actual_manifest != expected_manifest:
        raise RuntimeError("Runtime bundle manifest does not match its contents")
    for name, expected in hashes.items():
        if _digest((directory / name).read_bytes()) != expected:
            raise RuntimeError("Runtime bundle file was modified: " + name)


def _stage_bundle(destination, contents):
    hashes = {name: _digest(contents[name]) for name in RUNTIME_FILES}
    bundle_id = _bundle_id(hashes)
    parent = destination / "runtimes"
    _plain_path(parent)
    parent.mkdir(exist_ok=True)
    target = parent / bundle_id
    if target.exists():
        _verify_bundle(target, hashes)
        return bundle_id
    staging = parent / (".staging-" + uuid.uuid4().hex)
    staging.mkdir()
    known_names = (*RUNTIME_FILES, "manifest.json")
    try:
        for name in RUNTIME_FILES:
            _write(staging / name, contents[name])
        _write(staging / "manifest.json", _json({"schema": 1, "bundle_id": bundle_id, "files": hashes}))
        _verify_bundle(staging, hashes)
        # Directory rename exposes the complete, validated immutable bundle at once.
        _plain_path(target)
        staging.rename(target)
        return bundle_id
    finally:
        if staging.exists():
            _plain_path(staging)
            if staging.parent.resolve() != parent.resolve() or not staging.name.startswith(".staging-"):
                raise RuntimeError("Unexpected runtime staging path; refusing cleanup")
            # Never recursively delete or move a computed directory.
            for name in known_names:
                path = staging / name
                _plain_path(path)
                path.unlink(missing_ok=True)
            staging.rmdir()


def _managed_files(destination):
    manifest = destination / "wcfi-managed.json"
    _plain_path(manifest)
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Existing WCFI_Bridge directory is not managed by wcfi-tools") from exc
    if not isinstance(metadata, dict) or metadata.get("owner") != "wcfi-tools":
        raise RuntimeError("Existing WCFI_Bridge directory is not managed by wcfi-tools")
    hashes = metadata.get("files")
    allowed = set(FILES) | {"bridge-home.json", "runtime-current.json"}
    if not isinstance(hashes, dict) or any(
            name not in allowed or not isinstance(value, str) or not _HASH.fullmatch(value)
            for name, value in hashes.items()):
        raise RuntimeError("Existing bridge ownership manifest is invalid")
    for name, expected in hashes.items():
        path = destination / name
        _plain_path(path)
        if not path.is_file() or _digest(path.read_bytes()) != expected:
            raise RuntimeError("Managed bridge file was modified or is missing: " + name)
    if "runtime-current.json" in hashes:
        _verify_current_bundle(destination, hashes)
    return hashes


def _verify_current_bundle(destination, managed):
    """Check the previous activation too, even when installing a different bundle."""
    try:
        pointer = json.loads((destination / "runtime-current.json").read_text(encoding="utf-8"))
        bundle_id = pointer["bundle_id"]
        bootstrap = pointer["bootstrap_hashes"]
        if pointer["schema"] != 1 or not isinstance(bundle_id, str) or not _HASH.fullmatch(bundle_id):
            raise ValueError("invalid bundle identity")
        if bootstrap != {name: managed.get(name) for name in BOOTSTRAP_FILES}:
            raise ValueError("bootstrap hashes do not match managed files")
        directory = destination / "runtimes" / bundle_id
        _plain_path(directory / "manifest.json")
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        hashes = metadata["files"]
        if not isinstance(hashes, dict) or set(hashes) != set(RUNTIME_FILES) or any(
                not isinstance(value, str) or not _HASH.fullmatch(value) for value in hashes.values()):
            raise ValueError("invalid runtime hashes")
        if _bundle_id(hashes) != bundle_id:
            raise ValueError("runtime hashes do not match bundle identity")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Existing runtime activation is invalid") from exc
    _verify_bundle(directory, hashes)


def install(*, user_library=None, state_dir=None):
    library = Path(user_library).absolute() if user_library else find_user_library()
    _plain_path(library)
    library = library.resolve()
    if not library.is_dir():
        raise RuntimeError("User Library must be an existing directory")
    destination = library / "Remote Scripts" / PACKAGE_NAME
    _plain_path(destination)
    managed = _managed_files(destination) if destination.exists() else {}
    contents = {}
    for name in FILES:
        _plain_path(REMOTE_DIRECTORY / name)
        contents[name] = (REMOTE_DIRECTORY / name).read_bytes()
        ast.parse(contents[name], filename=name, feature_version=(3, 7))
    state_home = str(Path(state_dir or state_directory()).resolve())
    root_contents = {name: contents[name] for name in BOOTSTRAP_FILES}
    root_contents["bridge-home.json"] = _json({"state_dir": state_home})
    root_names = (*root_contents, "runtime-current.json", "wcfi-managed.json")
    previous = {}
    for name in root_names:
        path = destination / name
        _plain_path(path)
        if path.exists() and name != "wcfi-managed.json" and name not in managed:
            raise RuntimeError("Refusing to overwrite an untracked bridge file: " + name)
        previous[name] = path.read_bytes() if path.exists() else None
        if name in managed and (previous[name] is None or _digest(previous[name]) != managed[name]):
            raise RuntimeError("Managed bridge file changed during installation: " + name)
    restart_required = any(previous[name] != root_contents[name] for name in root_contents)
    destination.mkdir(parents=True, exist_ok=True)
    bundle_id = _stage_bundle(destination, contents)
    pointer = {"schema": 1, "bundle_id": bundle_id,
               "bootstrap_hashes": {name: _digest(contents[name]) for name in BOOTSTRAP_FILES}}
    root_contents["runtime-current.json"] = _json(pointer)
    metadata = {"owner": "wcfi-tools", "protocol": 1,
                "files": {name: _digest(data) for name, data in root_contents.items()}}
    # Pointer is the last executable configuration published; metadata follows it.
    root_contents["wcfi-managed.json"] = _json(metadata)
    written = []
    try:
        for name, data in root_contents.items():
            path = destination / name
            _plain_path(path)
            current = path.read_bytes() if path.exists() else None
            if current != previous[name]:
                raise RuntimeError("Bridge file changed during installation: " + name)
            if current != data:
                _write(path, data)
                written.append(name)
    except Exception as exc:
        failures = []
        for name in reversed(written):
            path = destination / name
            try:
                _plain_path(path)
                if path.read_bytes() != root_contents[name]:
                    raise RuntimeError("file changed concurrently")
                if previous[name] is None:
                    path.unlink()
                else:
                    _write(path, previous[name])
            except (OSError, RuntimeError) as restore_error:
                failures.append(name + ": " + str(restore_error))
        if failures:
            raise RuntimeError("Installation failed; rollback needs inspection: " + "; ".join(failures)) from exc
        raise
    activation = (
        "Bootstrap or configuration changed: save the set and restart Live once. Select WCFI_Bridge "
        "as a Control Surface with Input and Output set to None if necessary. Subsequent runtime-only "
        "updates can use 'wcfi audio ableton reload'. Do not press Play."
        if restart_required else
        "Runtime bundle installed. With Play and Record stopped, run 'wcfi audio ableton reload' "
        "to activate it without restarting Live."
    )
    return {"installed": str(destination), "state_dir": state_home, "bundle_id": bundle_id,
            "restart_required": restart_required, "activation": activation}
