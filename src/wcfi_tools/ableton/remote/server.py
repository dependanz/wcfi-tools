"""Authenticated loopback RPC; network workers never touch a Live object."""
import collections
import hashlib
import hmac
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .adapter import BridgeError

MAX_REQUEST = 65536
REQUEST_SECONDS = 5.0


def decode_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


class Job:
    def __init__(self, fingerprint, operation, arguments, timeout):
        self.fingerprint = fingerprint
        self.operation = operation
        self.arguments = arguments
        self.deadline = time.monotonic() + timeout
        self.event = threading.Event()
        self.state = "pending"
        self.response = None


class Dispatcher:
    """Bounded, deduplicated queue, drained only by the control surface timer."""
    def __init__(self, execute):
        self.execute = execute
        self.jobs = collections.OrderedDict()
        self.pending = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.closed = False
        self.quiesced = False

    def submit(self, request_id, operation, arguments, timeout=REQUEST_SECONDS):
        fingerprint = hashlib.sha256(json.dumps([operation, arguments], sort_keys=True,
                                                allow_nan=False).encode()).hexdigest()
        with self.lock:
            if self.closed:
                raise BridgeError("disconnected", "Bridge is disconnected")
            job = self.jobs.get(request_id)
            if job is not None:
                if job.fingerprint != fingerprint:
                    raise BridgeError("request_conflict", "Request ID was already used for a different operation")
                return job
            if self.quiesced:
                raise BridgeError("reloading", "Bridge is reloading; no change was applied")
            if self.pending.full():
                raise BridgeError("busy", "Bridge request queue is full")
            # Keep recent results for explicit retry with the SAME request ID.
            # Never evict outstanding work or retry an ambiguous mutation.
            for key in list(self.jobs):
                if len(self.jobs) < 256:
                    break
                if self.jobs[key].state in ("done", "cancelled"):
                    del self.jobs[key]
            if len(self.jobs) >= 256:
                raise BridgeError("busy", "Bridge request history is full")
            job = Job(fingerprint, operation, arguments, timeout)
            self.jobs[request_id] = job
            self.pending.put_nowait(job)
            return job

    def wait(self, job):
        job.event.wait(max(0, job.deadline - time.monotonic()))
        with self.lock:
            if job.state == "pending":
                job.state = "cancelled"
                job.response = {"ok": False, "error": {"code": "expired",
                    "message": "Live did not service the request in time; no change was applied"}}
                job.event.set()
            if job.response is not None:
                return job.response
            return {"ok": False, "error": {"code": "outcome_unknown",
                "message": "Operation started but has not finished. Inspect before making another change; do not retry with a new ID."}}

    def drain(self):
        started = time.monotonic()
        for _ in range(4):
            if time.monotonic() - started > .015:
                break
            with self.lock:
                if self.closed or self.quiesced:
                    break
            try:
                job = self.pending.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                if job.state != "pending":
                    continue
                if self.quiesced:
                    self._cancel(job, "reloading", "Bridge is reloading; no change was applied")
                    continue
                if self.closed or time.monotonic() >= job.deadline:
                    job.state = "cancelled"
                    job.response = {"ok": False, "error": {"code": "expired",
                        "message": "Request expired; no change was applied"}}
                    job.event.set()
                    continue
                job.state = "running"
            try:
                response = {"ok": True, "result": self.execute(job.operation, job.arguments)}
                # Validate while still inside the exception boundary.
                json.dumps(response, allow_nan=False)
            except BridgeError as exc:
                response = {"ok": False, "error": {"code": exc.code, "message": str(exc),
                                                     "details": exc.details}}
            except (TypeError, ValueError) as exc:
                response = {"ok": False, "error": {"code": "invalid_argument", "message": str(exc)}}
            except Exception as exc:  # noqa: BLE001 -- isolate host failures from Live's timer
                response = {"ok": False, "error": {"code": "host_error",
                    "message": "Live operation failed: " + str(exc),
                    "details": {"inspect_before_retry": True}}}
            with self.lock:
                job.response = response
                job.state = "done"
                job.event.set()

    @staticmethod
    def _cancel(job, code, message):
        job.state = "cancelled"
        job.response = {"ok": False, "error": {"code": code, "message": message}}
        job.event.set()

    def quiesce(self):
        """Freeze admission on Live's thread; let the running reload reply finish."""
        with self.lock:
            if self.closed:
                raise BridgeError("disconnected", "Bridge is disconnected")
            if sum(job.state == "running" for job in self.jobs.values()) > 1:
                raise BridgeError("busy", "Other bridge operations are still running; reload was not started")
            self.quiesced = True
            for job in self.jobs.values():
                if job.state == "pending":
                    self._cancel(job, "reloading", "Bridge is reloading; no change was applied")
            # Discard cancelled queue entries so resume does not inherit a full
            # queue. Running jobs have already been taken out of this queue.
            while True:
                try:
                    self.pending.get_nowait()
                except queue.Empty:
                    break

    def resume(self):
        """Re-enable the old runtime after a candidate failed validation."""
        with self.lock:
            if self.closed:
                raise BridgeError("disconnected", "Bridge is disconnected")
            self.quiesced = False

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                if job.state == "pending":
                    self._cancel(job, "disconnected", "Bridge disconnected; no change was applied")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.token = secrets.token_urlsafe(32)
        self.workers = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 0), Handler)

    def process_request(self, request, address):
        if not self.workers.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.workers.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *_args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_POST(self):
        try:
            if (self.headers.get("Origin") or
                    self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_port) or
                    not hmac.compare_digest(self.headers.get("Authorization", ""),
                                            "Bearer " + self.server.token)):
                self.reply(403, {"ok": False, "error": {"code": "forbidden", "message": "Unauthorized local request"}})
                return
            if self.path != "/v1/rpc":
                self.reply(404, {"ok": False, "error": {"code": "not_found", "message": "Unknown endpoint"}})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Expected one Content-Length and no transfer encoding")
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST:
                raise ValueError("Request body exceeds limit or is empty")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            request = decode_json(raw)
            if not isinstance(request, dict) or set(request) != {"id", "operation", "arguments"}:
                raise ValueError("Expected id, operation and arguments")
            request_id = request["id"]
            if not isinstance(request_id, str) or not 16 <= len(request_id) <= 128:
                raise ValueError("Invalid request ID")
            if (not isinstance(request["operation"], str) or
                    not isinstance(request["arguments"], dict)):
                raise TypeError("Invalid operation or arguments")
            job = self.server.dispatcher.submit(request_id, request["operation"], request["arguments"])
            self.reply(200, dict(self.server.dispatcher.wait(job), id=request_id))
        except BridgeError as exc:
            self.reply(409, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
        except (TypeError, ValueError, OSError, UnicodeError, RecursionError) as exc:
            self.reply(400, {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}})


class BridgeServer:
    def __init__(self, adapter, folder, execute=None, publish=True):
        self.dispatcher = Dispatcher(adapter.execute if execute is None else execute)
        self.session_id = adapter.session_id
        self.folder = Path(folder)
        self.path = self.folder / f"bridge-{os.getpid()}-{adapter.session_id}.json"
        self.server = None
        self.thread = None
        self.stop_thread = None
        self.closed = False
        self.published = False
        self._lifecycle_lock = threading.Lock()
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.server = Server(self.dispatcher)
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           kwargs={"poll_interval": .1}, daemon=True)
            self.thread.start()
            if publish:
                self.publish()
        except Exception:
            self.close()
            if self.stop_thread is not None:
                self.stop_thread.join(2)
            raise

    def _descriptor(self):
        return {"protocol": 1, "pid": os.getpid(), "instance_id": self.session_id,
                "port": self.server.server_port, "token": self.server.token}

    def publish(self):
        """Atomically advertise a fully validated runtime, never a partial file."""
        with self._lifecycle_lock:
            if self.closed:
                raise BridgeError("disconnected", "Bridge is disconnected")
            descriptor = self._descriptor()
            if self.published:
                return descriptor
            temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
            created = False
            try:
                fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created = True
                with os.fdopen(fd, "w") as output:
                    json.dump(descriptor, output)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(self.path)
                self.published = True
            finally:
                if created:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            return descriptor

    def close(self):
        with self._lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            self.dispatcher.close()
            if self.published:
                try:
                    # A stale server must not remove a replacement descriptor.
                    if json.loads(self.path.read_text()) == self._descriptor():
                        self.path.unlink()
                except (OSError, ValueError):
                    pass
                self.published = False

            if self.server is None:
                return
            if self.thread is None or not self.thread.is_alive():
                self.server.server_close()
                return

            def stop():
                try:
                    # shutdown() deadlocks when serve_forever never started.
                    if self.thread is not None and self.thread.is_alive():
                        self.server.shutdown()
                finally:
                    self.server.server_close()

            self.stop_thread = threading.Thread(target=stop, daemon=True)
            try:
                self.stop_thread.start()
            except RuntimeError:
                # Thread creation can fail during resource exhaustion. Still
                # release the socket rather than leaving a candidate behind.
                stop()
                raise
