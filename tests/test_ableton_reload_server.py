"""Reload queue isolation and descriptor lifecycle without any Live host."""
import concurrent.futures
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from wcfi_tools.ableton.remote import server as module
from wcfi_tools.ableton.remote.adapter import BridgeError
from wcfi_tools.ableton.remote.server import BridgeServer, Dispatcher


def test_reload_finishes_current_request_but_cancels_all_later_work():
    calls = []

    def execute(operation, arguments):
        calls.append(operation)
        if operation == "reload":
            dispatcher.quiesce()
            assert current.state == "running"
            assert dispatcher.submit("reload-id", "reload", {}) is current
        return arguments

    dispatcher = Dispatcher(execute)
    current = dispatcher.submit("reload-id", "reload", {})
    following = [dispatcher.submit(str(i), "set_parameter", {"value": i}) for i in range(10)]
    dispatcher.drain()
    assert calls == ["reload"]
    assert dispatcher.wait(current) == {"ok": True, "result": {}}
    for job in following:
        response = dispatcher.wait(job)
        assert response["error"]["code"] == "reloading"
        assert "no change" in response["error"]["message"]
        assert job.state == "cancelled"
    assert dispatcher.submit("reload-id", "reload", {}) is current
    assert dispatcher.submit("0", "set_parameter", {"value": 0}) is following[0]
    dispatcher.drain()
    assert calls == ["reload"]
    with pytest.raises(BridgeError) as exc:
        dispatcher.submit("new", "inspect_session", {})
    assert exc.value.code == "reloading"
    with pytest.raises(BridgeError) as exc:
        dispatcher.submit("reload-id", "set_parameter", {})
    assert exc.value.code == "request_conflict"


def test_resume_admits_new_jobs_but_never_revives_cancelled_mutations():
    calls = []
    dispatcher = Dispatcher(lambda operation, arguments: calls.append(operation))
    cancelled = [dispatcher.submit(str(i), "old", {}) for i in range(32)]
    dispatcher.quiesce()
    dispatcher.quiesce()
    dispatcher.resume()
    fresh = dispatcher.submit("fresh", "new", {})
    dispatcher.drain()
    assert calls == ["new"]
    assert dispatcher.wait(fresh)["ok"]
    assert all(job.state == "cancelled" for job in cancelled)


def test_quiesce_refuses_concurrent_running_operations_without_cancelling_queue():
    dispatcher = Dispatcher(lambda *_args: None)
    first = dispatcher.submit("first", "work", {})
    second = dispatcher.submit("second", "work", {})
    pending = dispatcher.submit("third", "work", {})
    first.state = second.state = "running"
    with pytest.raises(BridgeError) as exc:
        dispatcher.quiesce()
    assert exc.value.code == "busy"
    assert not dispatcher.quiesced
    assert pending.state == "pending"


def test_close_is_final_and_does_not_cancel_a_running_reply():
    dispatcher = Dispatcher(lambda *_args: None)
    running = dispatcher.submit("running", "reload", {})
    running.state = "running"
    pending = dispatcher.submit("pending", "work", {})
    dispatcher.close()
    dispatcher.close()
    assert running.state == "running"
    assert dispatcher.wait(pending)["error"]["code"] == "disconnected"
    for call in (dispatcher.resume, dispatcher.quiesce,
                 lambda: dispatcher.submit("new", "work", {})):
        with pytest.raises(BridgeError) as exc:
            call()
        assert exc.value.code == "disconnected"


class FakeServer:
    instances: ClassVar[list] = []

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.server_port = 12345
        self.token = "fake-local-token"
        self.closed = False
        self.stopped = threading.Event()
        self.instances.append(self)

    def serve_forever(self, poll_interval):
        self.stopped.wait(5)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True


@pytest.fixture
def fake_server(monkeypatch):
    FakeServer.instances = []
    monkeypatch.setattr(module, "Server", FakeServer)
    return FakeServer


def adapter(session="fake-session"):
    return SimpleNamespace(session_id=session, execute=lambda *_args: "adapter")


def close_and_wait(server):
    server.close()
    if server.stop_thread is not None:
        server.stop_thread.join(2)
        assert not server.stop_thread.is_alive()
    if server.thread is not None:
        server.thread.join(2)
        assert not server.thread.is_alive()


def test_staged_server_is_not_discoverable_until_atomic_publication(tmp_path, fake_server):
    server = BridgeServer(adapter(), tmp_path, execute=lambda *_args: "custom", publish=False)
    try:
        assert server.session_id == "fake-session"
        assert list(tmp_path.iterdir()) == []
        job = server.dispatcher.submit("request", "inspect", {})
        server.dispatcher.drain()
        assert server.dispatcher.wait(job)["result"] == "custom"
        descriptor = server.publish()
        assert json.loads(server.path.read_text()) == descriptor
        assert descriptor["instance_id"] == "fake-session"
        assert descriptor["token"] == "fake-local-token"
        assert server.publish() == descriptor
        assert list(tmp_path.iterdir()) == [server.path]
    finally:
        close_and_wait(server)
    assert fake_server.instances[0].closed
    assert not server.path.exists()
    assert server.dispatcher.closed
    close_and_wait(server)
    with pytest.raises(BridgeError) as exc:
        server.publish()
    assert exc.value.code == "disconnected"


@pytest.mark.parametrize("failure", ["dump", "replace", "fsync"])
def test_constructor_publication_failure_cleans_socket_thread_and_temporary(
        tmp_path, fake_server, monkeypatch, failure):
    def fail(*_args, **_kwargs):
        raise OSError("publication test failure")

    if failure == "dump":
        monkeypatch.setattr(module.json, "dump", fail)
    elif failure == "replace":
        monkeypatch.setattr(Path, "replace", fail)
    else:
        monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="publication test failure"):
        BridgeServer(adapter(), tmp_path)
    assert len(fake_server.instances) == 1
    assert fake_server.instances[0].closed
    assert fake_server.instances[0].stopped.is_set()
    assert fake_server.instances[0].dispatcher.closed
    assert list(tmp_path.iterdir()) == []


def test_staged_publication_failure_can_be_closed_without_deleting_another_descriptor(
        tmp_path, fake_server, monkeypatch):
    server = BridgeServer(adapter(), tmp_path, publish=False)
    existing = {"belongs_to": "another instance"}
    server.path.write_text(json.dumps(existing))

    def fail(*_args):
        raise OSError("replace denied")

    monkeypatch.setattr(Path, "replace", fail)
    try:
        with pytest.raises(OSError, match="replace denied"):
            server.publish()
    finally:
        close_and_wait(server)
    assert json.loads(server.path.read_text()) == existing
    assert list(tmp_path.iterdir()) == [server.path]


def test_close_does_not_unlink_a_replaced_descriptor(tmp_path, fake_server):
    server = BridgeServer(adapter(), tmp_path)
    replacement = {"belongs_to": "new runtime"}
    server.path.write_text(json.dumps(replacement))
    close_and_wait(server)
    assert json.loads(server.path.read_text()) == replacement


def test_publication_does_not_delete_a_temp_file_it_did_not_create(tmp_path, fake_server, monkeypatch):
    server = BridgeServer(adapter(), tmp_path, publish=False)
    monkeypatch.setattr(module.secrets, "token_hex", lambda _length: "collision")
    temporary = server.path.with_name(server.path.name + ".collision.tmp")
    temporary.write_text("preexisting")
    try:
        with pytest.raises(FileExistsError):
            server.publish()
    finally:
        close_and_wait(server)
    assert temporary.read_text() == "preexisting"
    assert not server.path.exists()


def test_directory_failure_creates_no_socket(tmp_path, fake_server):
    path = tmp_path / "not-a-directory"
    path.write_text("file")
    with pytest.raises(FileExistsError):
        BridgeServer(adapter(), path)
    assert not fake_server.instances


def test_server_thread_start_failure_closes_socket(tmp_path, fake_server, monkeypatch):
    def fail(_self):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        BridgeServer(adapter(), tmp_path)
    assert fake_server.instances[0].closed
    assert fake_server.instances[0].dispatcher.closed
    assert list(tmp_path.iterdir()) == []


def test_real_loopback_server_replies_to_current_reload_then_rejects_new_work(tmp_path):
    """An isolated socket exercises HTTP workers; no Ableton process is used."""
    server = None

    def execute(operation, _arguments):
        if operation == "reload":
            server.dispatcher.quiesce()
        return {"operation": operation}

    server = BridgeServer(adapter("isolated-http"), tmp_path, execute=execute)

    def request(request_id, operation):
        body = json.dumps({"id": request_id, "operation": operation, "arguments": {}}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server.server_port}/v1/rpc", data=body,
            headers={"Authorization": "Bearer " + server.server.token,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(request, "reload-request-0001", "reload")
            # Wait for admission, then drain exactly as Live's timer would.
            for _ in range(100):
                if not server.dispatcher.pending.empty():
                    break
                threading.Event().wait(.01)
            server.dispatcher.drain()
            result = future.result(4)
            assert result["ok"]
            assert request("reload-request-0001", "reload") == result
            with pytest.raises(urllib.error.HTTPError) as exc:
                request("new-request-000002", "set_parameter")
            assert exc.value.code == 409
            assert json.load(exc.value)["error"]["code"] == "reloading"
    finally:
        close_and_wait(server)
