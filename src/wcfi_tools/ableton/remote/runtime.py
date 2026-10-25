"""Stable loader: replace a complete verified runtime on Live's timer thread.

No transport, device or parameter setters belong here. This module itself is
not hot-reloaded: changes to the bootstrap require one Live restart.
"""
import hashlib
import json
import re
import stat
import sys
import types
import uuid
from pathlib import Path

BOOTSTRAP_VERSION = 1
BOOTSTRAP_FILES = ("__init__.py", "surface.py", "runtime.py")
RUNTIME_FILES = ("adapter.py", "browser.py", "insertion_context.py", "server.py")
HASH = re.compile(r"[0-9a-f]{64}\Z")
RELOAD_ID = re.compile(r"[0-9a-f]{32}\Z")


def plain_path(path):
    absolute = Path(path).absolute()
    for part in reversed((absolute,) + tuple(absolute.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked bridge paths are not supported")


def read_bytes(path, limit=1024 * 1024):
    plain_path(path)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Bridge file exceeds size limit")
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate bridge configuration key")
            result[key] = value
        return result
    return json.loads(read_bytes(path, 16384).decode("utf-8"), object_pairs_hook=unique)


def bootstrap_hashes(root):
    return {name: digest(read_bytes(root / name)) for name in BOOTSTRAP_FILES}


# Capture what was loaded, not the files present when a new surface is created.
# Live may retain this module after selecting None and then WCFI_Bridge again.
LOADED_BOOTSTRAP_HASHES = bootstrap_hashes(Path(__file__).parent)


def session_fingerprint(snapshot):
    """Compare audio state while excluding freshly issued opaque object IDs."""
    tracks = snapshot["tracks"]
    groups = {track["id"]: [track["kind"], track["index"]] for track in tracks}

    def clean(value):
        if isinstance(value, dict):
            return {key: groups.get(item, item) if key == "group_id" else clean(item)
                    for key, item in value.items() if key != "id"}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return json.dumps(clean({key: snapshot[key] for key in
                             ("name", "file_path", "transport", "tracks")}),
                      sort_keys=True, allow_nan=False)


class RuntimeController:
    def __init__(self, song, application, package_root, state_dir, loaded_bootstrap_hashes=None):
        self.song = song
        self.application = application
        self.root = Path(package_root)
        self.state_dir = Path(state_dir)
        self.loaded_hashes = dict(LOADED_BOOTSTRAP_HASHES if loaded_bootstrap_hashes is None
                                  else loaded_bootstrap_hashes)
        self.current = None
        self.pending = None
        self.last_reload = None
        self.closed = False
        bundle = self._read_bundle()
        candidate = self._load(bundle)
        try:
            session_fingerprint(candidate["adapter"].inspect_session())
            candidate["server"].publish()
            self.current = candidate
        except Exception:
            self._dispose(candidate)
            raise

    def _pointer(self):
        pointer = read_json(self.root / "runtime-current.json")
        if (not isinstance(pointer, dict) or
                set(pointer) != {"schema", "bundle_id", "bootstrap_hashes"} or
                type(pointer["schema"]) is not int or pointer["schema"] != 1 or
                not isinstance(pointer["bundle_id"], str) or not HASH.fullmatch(pointer["bundle_id"])):
            raise ValueError("Invalid runtime activation pointer")
        hashes = pointer["bootstrap_hashes"]
        if hashes != self.loaded_hashes or hashes != bootstrap_hashes(self.root):
            raise ValueError("bootstrap_update_required: save the set and restart Live once to load the changed bootstrap")
        config = read_json(self.root / "bridge-home.json")
        if Path(config["state_dir"]).resolve() != self.state_dir.resolve():
            raise ValueError("bootstrap_update_required: discovery configuration changed; restart Live once")
        return pointer

    def _read_bundle(self, expected=None):
        pointer = self._pointer()
        bundle_id = pointer["bundle_id"]
        if expected is not None and bundle_id != expected:
            raise ValueError("Installed runtime changed; inspect before reloading")
        directory = self.root / "runtimes" / bundle_id
        plain_path(directory)
        if {entry.name for entry in directory.iterdir()} != set(RUNTIME_FILES) | {"manifest.json"}:
            raise ValueError("Unexpected runtime bundle contents")
        manifest = read_json(directory / "manifest.json")
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "bundle_id", "files"} or
                type(manifest["schema"]) is not int or manifest["schema"] != 1 or
                manifest["bundle_id"] != bundle_id or not isinstance(manifest["files"], dict) or
                set(manifest["files"]) != set(RUNTIME_FILES)):
            raise ValueError("Invalid runtime bundle manifest")
        hashes = manifest["files"]
        if any(not isinstance(value, str) or not HASH.fullmatch(value) for value in hashes.values()):
            raise ValueError("Invalid runtime file hash")
        if digest(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()) != bundle_id:
            raise ValueError("Invalid runtime bundle identity")
        sources = {name: read_bytes(directory / name) for name in RUNTIME_FILES}
        if any(digest(data) != hashes[name] for name, data in sources.items()):
            raise ValueError("Runtime bundle failed integrity check")
        # Compile before admission is frozen, without executing candidate code.
        codes = {name: compile(data, str(directory / name), "exec") for name, data in sources.items()}
        return {"bundle_id": bundle_id, "directory": directory, "codes": codes}

    def _load(self, bundle):
        namespace = __package__ + "._wcfi_runtime_" + uuid.uuid4().hex
        package = types.ModuleType(namespace)
        package.__package__ = namespace
        # All imports resolve only to the verified modules below, never files on disk.
        package.__path__ = []
        sys.modules[namespace] = package
        runtime = {"bundle_id": bundle["bundle_id"], "package_name": namespace, "server": None}
        try:
            for name in ("adapter.py", "insertion_context.py", "browser.py", "server.py"):
                module_name = namespace + "." + name[:-3]
                module = types.ModuleType(module_name)
                module.__package__ = namespace
                module.__file__ = str(bundle["directory"] / name)
                sys.modules[module_name] = module
                setattr(package, name[:-3], module)
                exec(bundle["codes"][name], module.__dict__)  # noqa: S102 -- verified, fixed local module allowlist
            runtime["adapter"] = package.adapter.LiveAdapter(self.song, self.application)
            runtime["error"] = package.adapter.BridgeError
            runtime["server"] = package.server.BridgeServer(
                runtime["adapter"], self.state_dir,
                execute=lambda operation, arguments: self._execute(runtime, operation, arguments), publish=False)
            return runtime
        except Exception:
            self._dispose(runtime)
            raise

    @staticmethod
    def _dispose(runtime):
        failure = None
        try:
            if runtime.get("server") is not None:
                runtime["server"].close()
        except Exception as exc:  # noqa: BLE001 -- cleanup must not strand the active runtime
            failure = str(exc)[:500]
        finally:
            prefix = runtime["package_name"]
            for name in list(sys.modules):
                if name == prefix or name.startswith(prefix + "."):
                    del sys.modules[name]
        return failure

    def _reload_status(self):
        result = {"supported": True, "bootstrap_version": BOOTSTRAP_VERSION,
                  "active_bundle_id": self.current["bundle_id"], "installed_bundle_id": None,
                  "bootstrap_current": False, "last_reload": self.last_reload}
        try:
            pointer = self._pointer()
            result.update(installed_bundle_id=pointer["bundle_id"], bootstrap_current=True)
        except Exception as exc:  # noqa: BLE001 -- status must remain available for recovery
            result["error"] = str(exc)[:500]
        return result

    def _execute(self, runtime, operation, arguments):
        if self.closed or runtime is not self.current:
            raise runtime["error"]("disconnected", "This bridge runtime has been replaced")
        if operation == "reload_bridge":
            return self._request_reload(runtime, arguments)
        result = runtime["adapter"].execute(operation, arguments)
        if operation in ("status", "inspect_session"):
            result["reload"] = self._reload_status()
            result["operations"] = sorted(set(result.get("operations", [])) | {"reload_bridge"})
        return result

    def _guard(self, runtime, discard_receipts):
        error = runtime["error"]
        if getattr(self.song, "is_playing", None) is not False or getattr(self.song, "record_mode", None) is not False:
            raise error("transport_running", "Reload requires Play and Record stopped; no transport was changed")
        browser = runtime["adapter"].device_browser
        if browser.uncertain:
            raise error("insertion_unresolved", "Resolve the uncertain device insertion before reloading")
        count = sum(not receipt.get("removed", False) for receipt in browser.receipts.values())
        if count and not discard_receipts:
            raise error("pending_insertions", "Reload would discard insertion rollback receipts; remove the test devices first or explicitly discard receipts",
                        {"receipt_count": count})
        return count

    def _request_reload(self, runtime, arguments):
        error = runtime["error"]
        required = {"session_id", "expected_bundle_id", "reload_id"}
        if (not isinstance(arguments, dict) or not required <= set(arguments) or
                set(arguments) - required - {"dry_run", "discard_receipts"}):
            raise error("invalid_argument", "Expected session_id, expected_bundle_id, reload_id and optional reload flags")
        if arguments["session_id"] != runtime["adapter"].session_id:
            raise error("stale_session", "Inspect the current bridge session before reloading")
        for key, pattern in (("expected_bundle_id", HASH), ("reload_id", RELOAD_ID)):
            if not isinstance(arguments[key], str) or not pattern.fullmatch(arguments[key]):
                raise error("invalid_argument", "Invalid " + key)
        for name in ("dry_run", "discard_receipts"):
            if type(arguments.get(name, False)) is not bool:
                raise error("invalid_argument", name + " must be a boolean")
        if self.pending is not None:
            raise error("reloading", "A reload is already pending")
        count = self._guard(runtime, arguments.get("discard_receipts", False))
        try:
            self._read_bundle(arguments["expected_bundle_id"])
        except Exception as exc:  # noqa: BLE001 -- classify all candidate validation failures
            code = "bootstrap_update_required" if "bootstrap_update_required" in str(exc) else "invalid_bundle"
            raise error(code, str(exc)[:500])
        plan = {"reload_id": arguments["reload_id"], "old_session_id": runtime["adapter"].session_id,
                "bundle_id": arguments["expected_bundle_id"], "discarded_receipts": count,
                "dry_run": arguments.get("dry_run", False), "changed": False}
        if plan["dry_run"]:
            return dict(plan, state="validated", verified=True)
        runtime["server"].dispatcher.quiesce()
        self.pending = dict(arguments)
        return dict(plan, state="accepted")

    def _perform_reload(self):
        request = self.pending
        self.pending = None
        old = self.current
        candidate = None
        before = None
        result = {"reload_id": request["reload_id"], "old_session_id": old["adapter"].session_id,
                  "bundle_id": request["expected_bundle_id"]}
        try:
            self._guard(old, request.get("discard_receipts", False))
            bundle = self._read_bundle(request["expected_bundle_id"])
            before = session_fingerprint(old["adapter"].inspect_session())
            candidate = self._load(bundle)
            if session_fingerprint(candidate["adapter"].inspect_session()) != before:
                raise ValueError("Session state changed while constructing the replacement runtime")
            self._guard(old, request.get("discard_receipts", False))
            if self._pointer()["bundle_id"] != bundle["bundle_id"]:
                raise ValueError("Runtime activation changed while loading")
            candidate["server"].publish()
        except Exception as exc:  # noqa: BLE001 -- retain the working runtime on candidate failure
            cleanup_error = None
            if candidate is not None:
                cleanup_error = self._dispose(candidate)
            if before is not None:
                try:
                    unchanged = session_fingerprint(old["adapter"].inspect_session()) == before
                except Exception:  # noqa: BLE001 -- unreadable host state must fail closed
                    unchanged = False
                if not unchanged:
                    old["adapter"].device_browser.uncertain = True
            self.last_reload = dict(result, state="failed", error=str(exc)[:500])
            if cleanup_error:
                self.last_reload["cleanup_error"] = cleanup_error
            old["server"].dispatcher.resume()
            return
        self.current = candidate
        self.last_reload = dict(result, state="complete")
        cleanup_error = self._dispose(old)
        if cleanup_error:
            self.last_reload["cleanup_error"] = cleanup_error

    def tick(self):
        if self.closed:
            return
        if self.pending is not None:
            self._perform_reload()
        self.current["server"].dispatcher.drain()

    def close(self):
        self.closed = True
        self.pending = None
        if self.current is not None:
            failure = self._dispose(self.current)
            if failure:
                raise RuntimeError("Bridge cleanup failed: " + failure)
