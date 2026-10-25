"""Idempotent service discovery and CLI/desktop startup; never touches DAW transport."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .service import state_directory


def discovery():
    path = state_directory() / "service.json"
    if not path.exists():
        raise RuntimeError("Emulator service is not running. Use wcfi audio emulator start")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("version") != 1 or type(data.get("port")) is not int
            or not 1 <= data["port"] <= 65535 or not isinstance(data.get("token"), str)
            or not 20 <= len(data["token"]) <= 128):
        raise RuntimeError("Invalid local emulator discovery file")
    return data


def request(method, path, data=None, *, descriptor=None, timeout=10):
    info = descriptor or discovery()
    if not path.startswith("/api/") or "://" in path:
        raise ValueError("Use a local emulator API route")
    endpoint = f"http://127.0.0.1:{info['port']}{path}"
    body = json.dumps(data or {}, allow_nan=False).encode() if method == "POST" else None
    req = urllib.request.Request(endpoint, data=body, method=method, headers={
        "Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"})
    try:
        # Proxies must never receive the local session capability.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read(65536)).get("error", str(exc))
        except (ValueError, AttributeError):
            message = str(exc)
        raise RuntimeError(message) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("The local emulator service is unavailable") from exc


def ensure_service(*, dll=None):
    try:
        info = discovery()
        request("GET", "/api/state", descriptor=info, timeout=1)
        return info
    except (OSError, ValueError, RuntimeError):
        pass
    folder = state_directory()
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "wcfi_tools.cli", "audio", "emulator", "serve"]
    if dll:
        command.extend(["--dll", str(Path(dll).resolve())])
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with (folder / "service.log").open("ab") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=flags, start_new_session=os.name != "nt")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            info = discovery()
            request("GET", "/api/state", descriptor=info, timeout=1)
            return info
        except (OSError, ValueError, RuntimeError):
            if process.poll() is not None:
                break
            time.sleep(.1)
    raise RuntimeError(f"Emulator startup failed. See {folder / 'service.log'}")


def launch(*, web=False, dll=None):
    info = ensure_service(dll=dll)
    # Launch always attempts to bring the emulator online. If native setup fails,
    # keep the UI available so its error and Start control remain accessible.
    try:
        request("POST", "/api/start", descriptor=info)
    except RuntimeError:
        pass
    url = f"http://127.0.0.1:{info['port']}/#token={info['token']}"
    if web:
        webbrowser.open(url)
        return {"ui": "browser", "service_pid": info["pid"], "port": info["port"]}
    root = Path(__file__).resolve().parents[3]
    override = os.environ.get("WCFI_EMULATOR_DESKTOP")
    candidates = [Path(override)] if override else [
        root / "desktop/src-tauri/target/release/wcfi-audio.exe",
        root / "desktop/src-tauri/target/debug/wcfi-audio.exe"]
    executable = next((path for path in candidates if path.is_file()), None)
    if not executable:
        raise RuntimeError("Tauri executable is not built. See docs/audio-emulator.md, "
                           "or use wcfi audio emulator launch --web")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen([str(executable.resolve()), "--url", url], creationflags=flags,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ui": "tauri", "pid": process.pid, "service_pid": info["pid"], "port": info["port"]}
