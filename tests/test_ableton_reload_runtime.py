"""Real verified bundles and loopback RPC, with a fake read/write-audited Live host."""
import concurrent.futures
import importlib
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest
from test_ableton_bridge import Song

from wcfi_tools.ableton.client import AbletonClient, AbletonError, reload_bridge
from wcfi_tools.ableton.remote.runtime import (
    RuntimeController,
    bootstrap_hashes,
    session_fingerprint,
)

installer = importlib.import_module("wcfi_tools.ableton.install")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in installer.FILES:
        shutil.copyfile(installer.REMOTE_DIRECTORY / name, source / name)
    monkeypatch.setattr(installer, "REMOTE_DIRECTORY", source)
    library = tmp_path / "library"
    library.mkdir()
    state = tmp_path / "state"
    result = installer.install(user_library=library, state_dir=state)
    root = Path(result["installed"])
    song = Song()
    song.record_mode = False
    controller = RuntimeController(song, None, root, state, bootstrap_hashes(root))
    yield controller, song, source, library, state
    controller.close()


def args(controller, **changes):
    result = {"session_id": controller.current["adapter"].session_id,
              "expected_bundle_id": changes["expected_bundle_id"] if "expected_bundle_id" in changes
                  else controller._pointer()["bundle_id"],
              "reload_id": uuid.uuid4().hex}
    result.update(changes)
    return result


def request(controller, **changes):
    return controller._execute(controller.current, "reload_bridge", args(controller, **changes))


def pump(controller, function):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(function)
        deadline = time.monotonic() + 10
        while not future.done() and time.monotonic() < deadline:
            controller.tick()
            time.sleep(.005)
        assert future.done(), "loopback request did not complete"
        return future.result()


def connection(controller, state):
    descriptor = json.loads(controller.current["server"].path.read_text())
    return AbletonClient(descriptor, discovery_folder=state)


def update(harness, suffix):
    controller, _, source, library, state = harness
    with (source / "adapter.py").open("a", encoding="utf-8") as stream:
        stream.write("\n" + suffix + "\n")
    result = installer.install(user_library=library, state_dir=state)
    assert not result["restart_required"]
    assert result["bundle_id"] != controller.current["bundle_id"]
    return result


def test_real_client_reloads_new_code_and_preserves_audio_state(harness):
    controller, song, _, _, state = harness
    old = controller.current
    before = session_fingerprint(old["adapter"].inspect_session())
    old_id = old["adapter"].inspect_session()["tracks"][0]["id"]
    client = connection(controller, state)
    old_token = client.descriptor["token"]
    result = update(harness, "LiveAdapter.RELOAD_TEST = 42")
    outcome = pump(controller, lambda: reload_bridge(client, timeout=5))
    assert outcome["verified"] and outcome["bundle_id"] == result["bundle_id"]
    assert controller.current["adapter"].RELOAD_TEST == 42
    assert client.descriptor["token"] != old_token
    assert old["server"].closed and not old["server"].path.exists()
    assert controller.current["adapter"].session_id != old["adapter"].session_id
    assert session_fingerprint(controller.current["adapter"].inspect_session()) == before
    assert old["package_name"] not in sys.modules
    assert len(list(state.glob("bridge-*.json"))) == 1
    # New package uses one coherent exception class, so errors remain classified.
    with pytest.raises(AbletonError) as exc:
        pump(controller, lambda: client.call("set_fader", {
            "session_id": client.descriptor["instance_id"], "track_id": old_id,
            "db": -6, "expected_value": .5}))
    assert exc.value.code == "stale_target"
    assert not song.tracks[0].mixer_device.volume.writes
    assert not song.moves
    assert not list(controller.root.glob("runtimes/**/__pycache__"))


def test_dry_run_leaves_runtime_queue_and_credentials_unchanged(harness):
    controller, _, _, _, state = harness
    old = controller.current
    client = connection(controller, state)
    descriptor = dict(client.descriptor)
    result = pump(controller, lambda: reload_bridge(client, dry_run=True))
    assert result["verified"] and not result["changed"]
    assert controller.current is old and controller.pending is None
    assert not old["server"].dispatcher.quiesced
    assert client.descriptor == descriptor
    assert controller.last_reload is None


def test_queued_writes_are_cancelled_and_swap_waits_one_tick(harness):
    controller = harness[0]
    old = controller.current
    dispatcher = old["server"].dispatcher
    snapshot = old["adapter"].inspect_session()
    job = dispatcher.submit(uuid.uuid4().hex, "reload_bridge", args(controller))
    write = dispatcher.submit(uuid.uuid4().hex, "set_fader", {
        "session_id": snapshot["session_id"], "track_id": snapshot["tracks"][0]["id"],
        "db": -10, "expected_value": snapshot["tracks"][0]["volume"]["value"]})
    controller.tick()
    assert job.response["result"]["state"] == "accepted"
    assert write.state == "cancelled" and write.response["error"]["code"] == "reloading"
    assert controller.current is old
    controller.tick()
    assert controller.current is not old and controller.last_reload["state"] == "complete"
    assert not harness[1].tracks[0].mixer_device.volume.writes


@pytest.mark.parametrize("flag", ["playing", "recording", "unknown_recording"])
def test_transport_is_only_read_never_changed(harness, flag):
    controller, song, *_ = harness
    if flag == "playing":
        class PlayingSong(Song):
            @property
            def is_playing(self):
                return True
        song.__class__ = PlayingSong
    else:
        song.record_mode = True if flag == "recording" else None
    with pytest.raises(controller.current["error"]) as exc:
        request(controller)
    assert exc.value.code == "transport_running"
    assert controller.pending is None


def test_unresolved_insertions_always_block_reload(harness):
    controller = harness[0]
    controller.current["adapter"].device_browser.uncertain = True
    with pytest.raises(controller.current["error"]) as exc:
        request(controller, discard_receipts=True)
    assert exc.value.code == "insertion_unresolved"


def test_receipts_require_explicit_discard_without_deleting_any_devices(harness):
    controller, song, *_ = harness
    old = controller.current
    before = session_fingerprint(old["adapter"].inspect_session())
    old["adapter"].device_browser.receipts["test"] = {"removed": False}
    with pytest.raises(old["error"]) as exc:
        request(controller)
    assert exc.value.code == "pending_insertions"
    result = request(controller, discard_receipts=True)
    assert result["discarded_receipts"] == 1
    controller.tick()
    assert not controller.current["adapter"].device_browser.receipts
    assert session_fingerprint(controller.current["adapter"].inspect_session()) == before
    assert not song.moves


def test_removed_receipts_do_not_block(harness):
    controller = harness[0]
    controller.current["adapter"].device_browser.receipts["test"] = {"removed": True}
    request(controller)
    controller.tick()
    assert controller.last_reload["state"] == "complete"


@pytest.mark.parametrize("suffix", [
    "raise RuntimeError('candidate import failed')",
    "def broken_init(self, *args):\n    raise RuntimeError('candidate initialization failed')\nLiveAdapter.__init__ = broken_init",
])
def test_candidate_failure_retains_old_runtime_and_rpc(harness, suffix):
    controller, _, _, _, state = harness
    old = controller.current
    client = connection(controller, state)
    update(harness, suffix)
    with pytest.raises(AbletonError) as exc:
        pump(controller, lambda: reload_bridge(client, timeout=5))
    assert exc.value.code == "reload_failed"
    assert controller.current is old
    assert not old["server"].dispatcher.quiesced
    assert pump(controller, lambda: client.call("status"))["session_id"] == old["adapter"].session_id
    assert len(list(state.glob("bridge-*.json"))) == 1
    assert not old["adapter"].device_browser.uncertain


def test_candidate_live_state_change_latches_writes_off(harness):
    controller = harness[0]
    old = controller.current
    update(harness, "original_init = LiveAdapter.__init__\ndef changed_init(self, song, application):\n"
           "    original_init(self, song, application)\n    song.tracks[0].mute = True\n"
           "LiveAdapter.__init__ = changed_init")
    request(controller)
    controller.tick()
    assert controller.current is old
    assert controller.last_reload["state"] == "failed"
    assert old["adapter"].device_browser.uncertain


def test_pointer_change_after_acceptance_aborts_and_resumes(harness):
    controller = harness[0]
    old = controller.current
    request(controller)
    update(harness, "MARKER = 1")
    controller.tick()
    assert controller.current is old and controller.last_reload["state"] == "failed"
    assert not old["server"].dispatcher.quiesced


def test_candidate_publication_failure_cleans_resources_and_resumes(harness, monkeypatch):
    controller = harness[0]
    old = controller.current
    load = controller._load
    candidates = []

    def fail_publish(bundle):
        candidate = load(bundle)
        candidates.append(candidate)
        def fail():
            raise OSError("cannot publish")
        monkeypatch.setattr(candidate["server"], "publish", fail)
        return candidate

    monkeypatch.setattr(controller, "_load", fail_publish)
    request(controller)
    controller.tick()
    assert controller.current is old and controller.last_reload["state"] == "failed"
    assert candidates[0]["server"].closed
    assert candidates[0]["package_name"] not in sys.modules
    assert not old["server"].dispatcher.quiesced


@pytest.mark.parametrize("failed_candidate", [True, False])
def test_cleanup_error_never_strands_active_runtime(harness, monkeypatch, failed_candidate):
    controller = harness[0]
    old = controller.current
    load = controller._load

    def close_then_fail(server):
        close = server.close
        def fail():
            close()
            raise OSError("cleanup reporting failed")
        monkeypatch.setattr(server, "close", fail)

    if failed_candidate:
        def fail_load(bundle):
            candidate = load(bundle)
            close_then_fail(candidate["server"])
            def fail_publish():
                raise OSError("cannot publish")
            monkeypatch.setattr(candidate["server"], "publish", fail_publish)
            return candidate
        monkeypatch.setattr(controller, "_load", fail_load)
    else:
        close_then_fail(old["server"])
    request(controller)
    controller.tick()
    assert controller.last_reload["cleanup_error"] == "cleanup reporting failed"
    if failed_candidate:
        assert controller.current is old and controller.last_reload["state"] == "failed"
        assert not old["server"].dispatcher.quiesced
    else:
        assert controller.current is not old and controller.last_reload["state"] == "complete"
    assert controller._execute(controller.current, "status", {})["session_id"]


@pytest.mark.parametrize("name", ["surface.py", "runtime.py", "__init__.py", "bridge-home.json"])
def test_bootstrap_or_config_change_requires_restart(harness, name):
    controller = harness[0]
    path = controller.root / name
    if name.endswith("json"):
        path.write_text(json.dumps({"state_dir": str(controller.root)}))
    else:
        path.write_bytes(path.read_bytes() + b"\n# changed bootstrap\n")
    status = controller._execute(controller.current, "status", {})
    assert not status["reload"]["bootstrap_current"]
    with pytest.raises(controller.current["error"]) as exc:
        request(controller, expected_bundle_id=controller.current["bundle_id"])
    assert exc.value.code == "bootstrap_update_required"


@pytest.mark.parametrize("fault", ["hash", "extra", "manifest", "duplicate_pointer"])
def test_invalid_bundle_never_quiesces_runtime(harness, fault):
    controller = harness[0]
    bundle = controller.root / "runtimes" / controller.current["bundle_id"]
    if fault == "hash":
        with (bundle / "adapter.py").open("ab") as stream:
            stream.write(b"\n# invalid hash\n")
    elif fault == "extra":
        (bundle / "unknown.py").write_text("pass")
    elif fault == "manifest":
        (bundle / "manifest.json").write_text("{}")
    else:
        path = controller.root / "runtime-current.json"
        path.write_text(path.read_text().replace('"schema": 1', '"schema": 1, "schema": 1'))
    # Use explicit args: the test must not validate the pointer in its helper.
    with pytest.raises(controller.current["error"]) as exc:
        controller._execute(controller.current, "reload_bridge", {
            "session_id": controller.current["adapter"].session_id,
            "expected_bundle_id": controller.current["bundle_id"], "reload_id": uuid.uuid4().hex})
    assert exc.value.code == "invalid_bundle"
    assert not controller.current["server"].dispatcher.quiesced


@pytest.mark.parametrize("changes", [
    {"session_id": "stale"}, {"expected_bundle_id": "../arbitrary.py"},
    {"reload_id": "no"}, {"path": "arbitrary.py"}, {"dry_run": 1}, {"discard_receipts": 1},
])
def test_reload_arguments_fail_closed(harness, changes):
    controller = harness[0]
    with pytest.raises(controller.current["error"]):
        request(controller, **changes)
    assert controller.pending is None


def test_disconnect_cancels_pending_reload_and_removes_own_modules(harness):
    controller = harness[0]
    old = controller.current
    request(controller)
    controller.close()
    controller.tick()
    assert controller.pending is None and controller.current is old
    assert old["server"].closed and not old["server"].path.exists()
    assert old["package_name"] not in sys.modules


def test_repeated_reload_does_not_accumulate_namespaces(harness):
    controller = harness[0]
    for _ in range(3):
        old = controller.current
        request(controller)
        controller.tick()
        assert controller.current is not old
        assert old["package_name"] not in sys.modules
    namespaces = [name for name in sys.modules if name.startswith(
        "wcfi_tools.ableton.remote._wcfi_runtime_")]
    assert len(namespaces) == 5
