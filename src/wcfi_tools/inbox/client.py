"""Talk to the local inbox server: make sure it's running, push items, read decisions back.

Standard library only, so any agent or script with Python can use it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .paths import (
    APP_ID,
    LOG_NAME,
    STATE_NAME,
    default_port,
    load_or_create_token,
    prepare_data_dir,
)


class InboxClientError(RuntimeError):
    """The server couldn't be reached, started, or rejected a request."""


def _detail(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    return str(data.get("detail", data)) if isinstance(data, dict) else str(data)


class InboxClient:
    def __init__(self, port: int | None = None, data_dir: Path | None = None) -> None:
        self.data_dir = prepare_data_dir(data_dir)
        self.port = port or default_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._token: str | None = None

    @property
    def url(self) -> str:
        return self.base + "/"

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = load_or_create_token(self.data_dir)
        return self._token

    # -- lifecycle ----------------------------------------------------------------------------

    def health(self, timeout: float = 0.6) -> dict | None:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def is_running(self) -> bool:
        info = self.health()
        return bool(info and info.get("app") == APP_ID)

    def ensure_running(self, wait: float = 20.0) -> bool:
        """Start the server if it isn't up. Returns True if this call started it."""
        info = self.health()
        if info is not None:
            if info.get("app") != APP_ID:
                raise InboxClientError(
                    f"Port {self.port} is used by another program. "
                    "Pick a free one with --port or WCFI_INBOX_PORT."
                )
            return False
        self.token  # noqa: B018 - create the token before the server so both agree
        command = [
            sys.executable, "-m", "wcfi_tools.inbox.server",
            "--port", str(self.port), "--data-dir", str(self.data_dir),
        ]
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        with open(self.data_dir / LOG_NAME, "ab") as log:
            subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            time.sleep(0.25)
        raise InboxClientError(
            f"The inbox server didn't start within {wait:.0f}s. Last log lines:\n{self._log_tail()}"
        )

    def shutdown(self, wait: float = 8.0) -> bool:
        """Stop the server and wait for the process to exit. Returns False if it wasn't running."""
        if not self.is_running():
            return False
        self._request("POST", "/api/shutdown", {})
        state = self.data_dir / STATE_NAME  # the server removes this as its very last step
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not self.is_running() and not state.exists():
                return True
            time.sleep(0.2)
        raise InboxClientError(
            "The server stopped answering but hasn't finished exiting. If this persists, end the "
            f"process whose pid is in {state}."
        )

    def _log_tail(self, lines: int = 15) -> str:
        try:
            text = (self.data_dir / LOG_NAME).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no log)"
        return "\n".join(text.splitlines()[-lines:]) or "(log is empty)"

    # -- API ----------------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None, timeout: float = 15.0) -> Any:
        headers = {"X-Inbox-Token": self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = _detail(exc.read().decode("utf-8", errors="replace"))
            raise InboxClientError(f"{method} {path} was refused ({exc.code}): {detail}") from None
        except (urllib.error.URLError, OSError):
            raise InboxClientError(
                f"Can't reach the inbox at {self.base}. Start it with `wcfi inbox up`."
            ) from None

    def push(self, items: list[dict]) -> dict[str, list[str]]:
        total: dict[str, list[str]] = {"added": [], "updated": [], "unchanged": []}
        for start in range(0, len(items), 200):
            result = self._request("POST", "/api/items", {"items": items[start : start + 200]})
            for key in total:
                total[key].extend(result.get(key, []))
        return total

    def items(
        self,
        *,
        status: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = {
            "status": status, "source": source, "kind": kind, "since": since, "limit": limit,
        }
        query = urlencode({k: v for k, v in params.items() if v is not None})
        return self._request("GET", "/api/items" + (f"?{query}" if query else ""))

    def decide(self, iid: str, **fields: Any) -> dict[str, Any]:
        return self._request("PATCH", f"/api/items/{quote(iid)}", fields)

    def memory_list(self, scope: str | None = None) -> list[dict[str, Any]]:
        query = f"?{urlencode({'scope': scope})}" if scope else ""
        return self._request("GET", "/api/memory" + query)["memory"]

    def memory_add(self, text: str, scope: str = "general") -> dict[str, Any]:
        return self._request("POST", "/api/memory", {"text": text, "scope": scope})

    def memory_delete(self, mid: int) -> None:
        self._request("DELETE", f"/api/memory/{int(mid)}")

    def sources(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/sources")["sources"]

    def source_set(
        self, name: str, *, cursor: str | None = None, notes: str | None = None, swept: bool = False
    ) -> dict[str, Any]:
        body = {"cursor": cursor, "notes": notes, "swept": swept}
        return self._request("PUT", f"/api/sources/{quote(name, safe='')}", body)
