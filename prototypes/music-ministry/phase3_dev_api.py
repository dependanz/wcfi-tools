"""Test-only, loopback-only Phase 3 HTTP boundary over the shared SQLite store.

The session authority in this module is deliberately synthetic. No HTTP endpoint
issues sessions; no browser-supplied user identity, role, or OAuth token is trusted.
This is NOT a production login provider, deployment entrypoint, or Wix adapter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from shared_store import MusicStore

from shared_store import InvalidRecord, NotAuthenticated, NotAuthorized, VersionConflict

MAX_JSON_BYTES = 512 * 1024
COOKIE_NAME = "wcfi_dev_session"
SONG_ITEM = re.compile(r"/api/v1/songs/([a-z0-9][a-z0-9-]{2,99})")
SONG_APPROVE = re.compile(r"/api/v1/songs/([a-z0-9][a-z0-9-]{2,99})/approve")
LINEUP_ITEM = re.compile(r"/api/v1/lineups/([a-z0-9][a-z0-9-]{2,99})")
LINEUP_PUBLISH = re.compile(r"/api/v1/lineups/([a-z0-9][a-z0-9-]{2,99})/publish")


@dataclass(frozen=True)
class _SyntheticSession:
    subject: str
    csrf_digest: bytes
    expires_at: float


class SyntheticSessionAuthority:
    """In-process test fixture. Never expose issue() or revoke() as HTTP routes."""

    def __init__(self):
        self._sessions: dict[bytes, _SyntheticSession] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def issue(self, fixture_subject: str, ttl_seconds: float = 900) -> tuple[str, str]:
        """Called by test harness with synthetic subjects, never from requests."""
        if not fixture_subject or not isinstance(fixture_subject, str):
            raise ValueError("A synthetic subject is required")
        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        record = _SyntheticSession(fixture_subject, self._digest(csrf), time.monotonic() + ttl_seconds)
        with self._lock:
            self._sessions[self._digest(cookie)] = record
        return cookie, csrf

    def resolve(self, cookie_header: str | None) -> tuple[str, bytes] | None:
        token = self._session_cookie(cookie_header)
        if not token:
            return None
        digest = self._digest(token)
        with self._lock:
            record = self._sessions.get(digest)
            if record is None:
                return None
            if record.expires_at <= time.monotonic():
                self._sessions.pop(digest, None)
                return None
            return record.subject, record.csrf_digest

    def revoke(self, cookie: str) -> None:
        with self._lock:
            self._sessions.pop(self._digest(cookie), None)

    @staticmethod
    def _session_cookie(cookie_header: str | None) -> str | None:
        if not cookie_header or len(cookie_header) > 4096:
            return None
        try:
            parsed = SimpleCookie()
            parsed.load(cookie_header)
            item = parsed.get(COOKIE_NAME)
            return item.value if item else None
        except (CookieError, ValueError):
            return None

    def valid_csrf(self, digest: bytes, supplied: str | None) -> bool:
        return bool(supplied and len(supplied) <= 256 and hmac.compare_digest(digest, self._digest(supplied)))


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError("Nonfinite JSON numbers are not allowed")


def make_handler(store: MusicStore, sessions: SyntheticSessionAuthority):
    """Build an HTTP handler using only server-held SQLite and session objects."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "WCFI-Phase3-Test"
        sys_version = ""
        protocol_version = "HTTP/1.0"  # Close after every response, including rejected bodies.

        def log_message(self, format: str, *args):  # noqa: A002
            # Do not accidentally log session cookies, lyric content, or query strings.
            return

        def _reply(self, status: int, result: object) -> None:
            payload = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _error(self, status: int, code: str) -> None:
            self._reply(status, {"error": code})

        def _origin_ok(self) -> bool:
            # This development server only runs on 127.0.0.1. Its Host and Origin
            # rules intentionally disallow remote access and cross-origin browsers.
            port = self.server.server_port
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                return False
            return True

        def _principal(self, mutating: bool = False) -> str | None:
            if not self._origin_ok():
                self._error(HTTPStatus.FORBIDDEN, "invalid_origin_or_host")
                return None
            identity = sessions.resolve(self.headers.get("Cookie"))
            if identity is None:
                self._error(HTTPStatus.UNAUTHORIZED, "member_sign_in_required")
                return None
            subject, csrf_digest = identity
            if mutating and not sessions.valid_csrf(csrf_digest, self.headers.get("X-WCFI-CSRF")):
                self._error(HTTPStatus.FORBIDDEN, "invalid_csrf")
                return None
            return subject

        def _json_body(self) -> dict:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked bodies are not supported")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("JSON content type required")
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise ValueError("Content-Length required") from exc
            if not 0 < size <= MAX_JSON_BYTES:
                raise ValueError("Invalid JSON body size")
            raw = self.rfile.read(size)
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite)
            if not isinstance(result, dict):
                raise ValueError("JSON object required")
            return result

        def _handle(self, mutating: bool) -> None:
            path = urlsplit(self.path)
            if path.query or path.fragment:
                self._error(HTTPStatus.BAD_REQUEST, "queries_not_supported")
                return
            if not mutating and path.path == "/healthz":
                if not self._origin_ok():
                    self._error(HTTPStatus.FORBIDDEN, "invalid_origin_or_host")
                else:
                    self._reply(HTTPStatus.OK, {"app": "wcfi-phase3-dev", "status": "ok"})
                return
            subject = self._principal(mutating=mutating)
            if subject is None:
                return
            try:
                if not mutating:
                    if path.path == "/api/v1/songs":
                        self._reply(HTTPStatus.OK, {"songs": store.list_songs(subject)})
                    elif path.path == "/api/v1/lineups":
                        # Internal lineup notes and member availability are not yet
                        # covered by a field-level policy. Never expose them here.
                        result = store.list_lineups(subject)
                        safe = [{k: row[k] for k in ("id", "title", "date", "songs", "state", "revision")}
                                for row in result]
                        self._reply(HTTPStatus.OK, {"lineups": safe})
                    else:
                        self._error(HTTPStatus.NOT_FOUND, "not_found")
                    return

                data = self._json_body()
                if self.command == "POST" and path.path == "/api/v1/songs":
                    if "id" in data:
                        raise InvalidRecord("Server assigns song IDs")
                    item = {**data, "id": f"song-{secrets.token_hex(12)}"}
                    store.create_song(subject, item)
                    self._reply(HTTPStatus.CREATED, {"id": item["id"], "revision": 1})
                elif self.command == "PATCH" and (m := SONG_ITEM.fullmatch(path.path)):
                    if set(data) != {"song", "expectedRevision"} or not isinstance(data["song"], dict):
                        raise InvalidRecord("Expected song and expectedRevision")
                    item = dict(data["song"])
                    if "id" in item and item["id"] != m.group(1):
                        raise InvalidRecord("Song ID mismatch")
                    item["id"] = m.group(1)
                    version = store.update_song_draft(subject, item, data["expectedRevision"])
                    self._reply(HTTPStatus.OK, {"id": item["id"], "revision": version})
                elif self.command == "POST" and (m := SONG_APPROVE.fullmatch(path.path)):
                    if data:
                        raise InvalidRecord("Approval body must be empty")
                    store.approve_song(subject, m.group(1))
                    self._reply(HTTPStatus.OK, {"status": "approved"})
                elif self.command == "POST" and path.path == "/api/v1/catalogue/import":
                    count = store.import_catalogue_v1(subject, data)
                    self._reply(HTTPStatus.CREATED, {"imported": count})
                elif self.command == "POST" and path.path == "/api/v1/lineups":
                    if "id" in data:
                        raise InvalidRecord("Server assigns lineup IDs")
                    item = {**data, "id": f"lineup-{secrets.token_hex(12)}"}
                    store.create_lineup(subject, item)
                    self._reply(HTTPStatus.CREATED, {"id": item["id"], "revision": 1})
                elif self.command == "PATCH" and (m := LINEUP_ITEM.fullmatch(path.path)):
                    if set(data) != {"lineup", "expectedRevision"} or not isinstance(data["lineup"], dict):
                        raise InvalidRecord("Expected lineup and expectedRevision")
                    item = dict(data["lineup"])
                    if "id" in item and item["id"] != m.group(1):
                        raise InvalidRecord("Lineup ID mismatch")
                    item["id"] = m.group(1)
                    version = store.update_lineup_draft(subject, item, data["expectedRevision"])
                    self._reply(HTTPStatus.OK, {"id": item["id"], "revision": version})
                elif self.command == "POST" and (m := LINEUP_PUBLISH.fullmatch(path.path)):
                    if data:
                        raise InvalidRecord("Publish body must be empty")
                    store.publish_lineup(subject, m.group(1))
                    self._reply(HTTPStatus.OK, {"status": "published"})
                else:
                    self._error(HTTPStatus.NOT_FOUND, "not_found")
            except NotAuthenticated:
                self._error(HTTPStatus.UNAUTHORIZED, "member_sign_in_required")
            except NotAuthorized:
                self._error(HTTPStatus.FORBIDDEN, "not_authorized")
            except VersionConflict:
                self._error(HTTPStatus.CONFLICT, "version_conflict")
            except (InvalidRecord, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_record")

        def do_GET(self) -> None:
            self._handle(False)

        def do_HEAD(self) -> None:
            self._handle(False)

        def do_POST(self) -> None:
            self._handle(True)

        def do_PATCH(self) -> None:
            self._handle(True)

        def do_OPTIONS(self) -> None:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

        def do_DELETE(self) -> None:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    return Handler


def make_loopback_server(store: MusicStore, sessions: SyntheticSessionAuthority, port: int = 0) -> ThreadingHTTPServer:
    """Local/synthetic test harness only; no public bind or production entrypoint."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, sessions))
    server.daemon_threads = True
    return server