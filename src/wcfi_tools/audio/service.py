"""Authenticated loopback API shared by the headless CLI, Tauri and web UI."""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from platformdirs import user_data_path

from .engine import EmulatorEngine
from .node_audio import read_node_audio

WEB_ROOT = Path(__file__).with_name("web")
MAX_UPLOAD = 128 * 1024 * 1024


def state_directory():
    override = os.environ.get("WCFI_EMULATOR_HOME")
    return Path(override) if override else user_data_path("wcfi", appauthor=False) / "audio-emulator"


class InstanceLock:
    def __init__(self, path):
        self.file = path.open("a+b")
        self.file.seek(0)
        self.file.write(b"\0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("The emulator service is already running") from None

    def close(self):
        self.file.close()


class EmulatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, engine, *, token=None, port=0, web_root=WEB_ROOT):
        self.engine = engine
        self.token = token or secrets.token_urlsafe(32)
        self.web_root = web_root.resolve()
        self._review = None
        self._review_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def review(self):
        with self._review_lock:
            if self._review is None:
                from .review_service import RehearsalReview

                self._review = RehearsalReview(self.engine.state_dir, self.engine.library_root)
            return self._review

    def server_close(self):
        if self._review is not None:
            self._review.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: EmulatorServer
    protocol_version = "HTTP/1.1"
    # Headers and PCM bodies must not wait on small-packet TCP coalescing.
    disable_nagle_algorithm = True

    def log_message(self, *_):
        pass  # Never log authorization headers, fragments, source names or local paths.

    def _reply(self, status, payload, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self):
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        # Exact Host rejects cross-site DNS rebinding into the loopback listener.
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            return False
        actual = self.headers.get("Authorization", "")
        return hmac.compare_digest(actual, f"Bearer {self.server.token}")

    def do_GET(self):
        path = urlsplit(self.path)
        if not path.path.startswith("/api/"):
            relative = unquote(path.path).lstrip("/") or "index.html"
            resource = (self.server.web_root / relative).resolve()
            if not resource.is_relative_to(self.server.web_root) or not resource.is_file():
                self._reply(404, {"error": "UI assets are missing; run npm run build in desktop/"})
                return
            mime = mimetypes.guess_type(str(resource))[0] or "application/octet-stream"
            if resource.suffix == ".js":
                mime = "text/javascript"
            self._reply(200, resource.read_bytes(), mime)
            return
        if not self._authorized():
            self._reply(403, {"error": "This local emulator session requires authorization"})
            return
        try:
            if path.path == "/api/state":
                result = self.server.engine.snapshot()
                result["service_pid"] = os.getpid()
                self._reply(200, result)
            elif path.path == "/api/analysis":
                ports = [int(n) for n in parse_qs(path.query).get("channels", ["1,2"])[0].split(",")]
                self._reply(200, self.server.engine.analyze(ports))
            elif path.path == "/api/takes":
                self._reply(200, self.server.engine.available_takes())
            elif path.path == "/api/rehearsal":
                # Older open frontends do not know the reference report type.
                self._reply(200, self.server.review.snapshot(
                    include_references=parse_qs(path.query).get("include_references") == ["1"],
                    filter_reviewed=parse_qs(path.query).get("reviewed_passages") == ["1"],
                    include_named_references=parse_qs(path.query).get("named_references") == ["1"],
                    include_balance=parse_qs(path.query).get("mix_studies") == ["1"],
                    balance_schema=next((v for v in (6, 5, 4, 3, 2) if parse_qs(path.query).get("balance_schema") == [str(v)]), 1),
                    include_candidates=parse_qs(path.query).get("candidate_balance") == ["1"],
                    include_suites=parse_qs(path.query).get("role_suites") == ["1"],
                    include_processing=parse_qs(path.query).get("processing_studies") == ["1"],
                    include_diagnostics=parse_qs(path.query).get("kit_diagnostics") == ["1"],
                    include_presence=parse_qs(path.query).get("presence_studies") == ["1"],
                    include_combined=parse_qs(path.query).get("combined_studies") == ["1"],
                    include_compression=parse_qs(path.query).get("compression_studies") == ["1"],
                    include_overlap=parse_qs(path.query).get("overlap_studies") == ["1"],
                    include_red_lowmid=parse_qs(path.query).get("red_lowmid_studies") == ["1"],
                    include_updated_reference=parse_qs(path.query).get("updated_reference_studies") == ["1"],
                    include_full_band=parse_qs(path.query).get("full_band_studies") == ["1"],
                    include_full_mix_benchmark=parse_qs(path.query).get("full_mix_benchmarks") == ["1"],
                    include_balance_benchmark=parse_qs(path.query).get("balance_benchmarks") == ["1"],
                    include_combined_benchmark=parse_qs(path.query).get("combined_benchmarks") == ["1"]))
            elif path.path == "/api/rehearsal/audio":
                query = parse_qs(path.query)
                self._reply(200, self.server.review.audio(query.get("job", [""])[0],
                    query.get("track", [""])[0], query.get("variant", [""])[0]), "audio/wav")
            elif path.path == "/api/nodes":
                self._reply(200, self.server.engine.nodes.snapshot())
            elif path.path == "/api/nodes/audio":
                query = parse_qs(path.query)
                identity = query.get("id", [""])[0]
                cursor = int(query["cursor"][0]) if "cursor" in query else None
                epoch = int(query["epoch"][0]) if "epoch" in query else None
                self._reply(200, read_node_audio(identity, cursor=cursor, epoch=epoch),
                            "application/octet-stream")
            else:
                self._reply(404, {"error": "Unknown API route"})
        except (ValueError, OSError, RuntimeError) as exc:
            self._reply(400, {"error": str(exc)})

    def _body(self, limit):
        length = int(self.headers.get("Content-Length", "-1"))
        if not 0 <= length <= limit:
            raise ValueError(f"Request must declare a length of at most {limit} bytes")
        self.connection.settimeout(30)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Incomplete request body")
        return data

    def do_POST(self):
        if not self._authorized():
            self.close_connection = True
            self._reply(403, {"error": "This local emulator session requires authorization"})
            return
        path = urlsplit(self.path)
        engine = self.server.engine
        try:
            if path.path == "/api/import":
                import soundfile as sf

                name = Path(parse_qs(path.query).get("name", ["audio.wav"])[0]).name
                extension = Path(name).suffix.lower()
                if extension not in {".wav", ".flac", ".aif", ".aiff", ".ogg", ".mp3"}:
                    raise ValueError("Import WAV, FLAC, AIFF, OGG or MP3 audio")
                data = self._body(MAX_UPLOAD)
                uploads = engine.state_dir / "uploads"
                uploads.mkdir(exist_ok=True)
                target = uploads / (uuid.uuid4().hex + extension)
                with target.open("xb") as file:
                    file.write(data)
                try:
                    info = sf.info(target)
                    if info.duration < 1 or not 1 <= info.channels <= 64:
                        raise ValueError("Use at least one second of audio with 1–64 channels")
                except (OSError, RuntimeError, ValueError):
                    target.unlink(missing_ok=True)
                    raise
                self._reply(200, {"path": str(target.resolve()), "name": name, "seconds": info.duration,
                                  "channels": info.channels, "sample_rate": info.samplerate})
                return
            body = self._body(65536)
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Request must be a JSON object")  # noqa: TRY004 - API validation
            if path.path == "/api/start":
                result = engine.start()
            elif path.path == "/api/stop":
                result = engine.stop()
            elif path.path == "/api/sending":
                result = engine.set_sending(data.get("enabled"))
            elif path.path == "/api/preset":
                result = engine.preset(data.get("name"))
            elif path.path == "/api/take/load":
                result = engine.load_take(data.get("take_id"))
            elif path.path == "/api/take/loop":
                result = engine.set_take_loop(data.get("start_frame"), data.get("end_frame"),
                                              data.get("fade_ms", 5))
            elif path.path == "/api/take/restart":
                result = engine.restart_take()
            elif path.path == "/api/take/unload":
                result = engine.unload_take()
            elif path.path == "/api/rehearsal/analyze":
                result = self.server.review.start(data)
            elif path.path == "/api/rehearsal/cancel":
                result = self.server.review.cancel()
            elif path.path == "/api/rehearsal/apply":
                result = self.server.review.apply(data)
            elif path.path == "/api/rehearsal/import-study":
                result = self.server.review.import_study(data)
            elif path.path == "/api/rehearsal/import-diagnostic":
                result = self.server.review.import_diagnostic(data)
            elif path.path == "/api/rehearsal/import-reference":
                result = self.server.review.import_reference(data)
            elif path.path == "/api/rehearsal/import-balance":
                result = self.server.review.import_balance(data)
            elif path.path == "/api/rehearsal/import-suite":
                result = self.server.review.import_suite(data)
            elif path.path == "/api/rehearsal/import-processing":
                result = self.server.review.import_suite(data | {"processing": True})
            elif path.path == "/api/rehearsal/assess-processing":
                result = self.server.review.assess_processing(data)
            elif path.path == "/api/rehearsal/assess-suite":
                result = self.server.review.assess_suite(data)
            elif path.path == "/api/rehearsal/assess-reference":
                result = self.server.review.assess_reference(data)
            elif path.path == "/api/rehearsal/assess-balance":
                result = self.server.review.assess_balance(data)
            elif path.path.startswith("/api/inputs/"):
                result = engine.configure(int(path.path.rsplit("/", 1)[-1]), data)
            elif path.path == "/api/shutdown":
                engine.stop()
                self._reply(200, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self._reply(404, {"error": "Unknown API route"})
                return
            self._reply(200, result)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            self._reply(400, {"error": str(exc)})

    def do_OPTIONS(self):
        self._reply(HTTPStatus.FORBIDDEN, {"error": "Cross-origin control is disabled"})


def serve(*, state_dir=None, dll=None, port=0, token=None, start_audio=True):
    folder = Path(state_dir) if state_dir else state_directory()
    folder.mkdir(parents=True, exist_ok=True)
    instance = InstanceLock(folder / "service.lock")
    engine = None
    server = None
    descriptor = folder / "service.json"
    try:
        engine = EmulatorEngine(folder, dll=dll)
        engine.launch_workers()
        if start_audio:
            try:
                engine.start()
            except (OSError, RuntimeError) as exc:
                engine.error = str(exc)
        server = EmulatorServer(engine, port=port, token=token)
        data = {"pid": os.getpid(), "port": server.server_port, "token": server.token,
                "origin": server.origin, "version": 1}
        temporary = descriptor.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(descriptor)
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if engine:
            engine.close()
        descriptor.unlink(missing_ok=True)
        instance.close()
