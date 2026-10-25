"""App analysis jobs and deliberately narrow user-triggered DAW application."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.review_apply import apply_gain, availability
from wcfi_tools.audio.review_capture import capture_pairs
from wcfi_tools.audio.review_service import RehearsalReview
from wcfi_tools.audio.service import EmulatorServer


def parameter(name, value, lower=0, upper=1):
    return {"id": name, "name": name, "value": value, "min": lower, "max": upper,
            "is_enabled": True, "is_quantized": False, "automation_state": 0}


@pytest.fixture
def report():
    node = {"name": "WCFI Analysis Node", "parameters": [parameter("Device On", 1)], "chains": []}
    compressor = {"id": "compressor", "name": "Compressor", "class_name": "Compressor2", "chains": [],
        "parameters": [parameter("Device On", 1), parameter("Makeup", 0), parameter("Dry/Wet", 1),
                       parameter("Output Gain", 0, -36, 36)]}
    track = {"id": "kit", "name": "KIT", "kind": "track", "devices": [node, compressor, copy.deepcopy(node)]}
    return {"provenance": {"post_fx_kind": "current_node_capture", "host_pid": 123},
        "chain_state": {"session_id": "session", "track": track},
        "proposals": [{"kind": "software_gain", "gain_db": -6}], "audio": {"after": "KIT-after.wav"}}


class Bridge:
    def __init__(self, report):
        self.descriptor = {"pid": 123}
        self.snapshot = {"session_id": "session", "tracks": [copy.deepcopy(report["chain_state"]["track"])]}
        self.calls = []
        self.change_on_dry_run = False
        self.bad_scale = False
        self.fail_write = False

    def call(self, op, args=None, **kwargs):
        self.calls.append((op, args, kwargs))
        if op == "inspect_session":
            return copy.deepcopy(self.snapshot)
        assert op == "set_parameter"
        if args.get("dry_run"):
            if self.change_on_dry_run:
                self.snapshot["tracks"][0]["mute"] = True
            return {"requested_display": "Normalized 0.3" if self.bad_scale else f"{args['value']:.2f} dB"}
        if self.fail_write:
            raise RuntimeError("Lost write response")
        assert args["expected_value"] == 0 and args["value"] == -6
        return {"verified": True}


def writes(bridge):
    return [c for c in bridge.calls if c[0] == "set_parameter" and not c[1].get("dry_run")]


def test_only_supported_exact_gain_adapter_can_write(report):
    bridge = Bridge(report)
    assert availability(report)["supported"]
    result = apply_gain(bridge, report, request_id="once")
    assert result["parameter_readback_verified"] and not result["audio_verified"]
    assert len(writes(bridge)) == 1 and writes(bridge)[0][2]["request_id"] == "once"
    report["provenance"]["post_fx_kind"] = "bypass_reference_not_current_ableton_chain"
    assert not availability(report)["supported"]


@pytest.mark.parametrize("problem", ["stale", "midflight", "scale", "pid"])
def test_stale_chain_wrong_host_and_non_db_controls_are_never_written(report, problem):
    bridge = Bridge(report)
    if problem == "stale":
        bridge.snapshot["tracks"][0]["name"] = "New KIT"
    elif problem == "midflight":
        bridge.change_on_dry_run = True
    elif problem == "scale":
        bridge.bad_scale = True
    else:
        bridge.descriptor = {"pid": 456}
    with pytest.raises(ValueError):
        apply_gain(bridge, report, request_id="none")
    assert not writes(bridge)


@pytest.mark.parametrize("problem", ["wet", "makeup", "automated", "disabled", "extra_device", "boost", "hf"])
def test_unsupported_processing_cannot_be_smuggled_into_apply(report, problem):
    comp = report["chain_state"]["track"]["devices"][1]
    if problem == "wet":
        comp["parameters"][2]["value"] = .5
    elif problem == "makeup":
        comp["parameters"][1]["value"] = 1
    elif problem == "automated":
        comp["parameters"][-1]["automation_state"] = 1
    elif problem == "disabled":
        comp["parameters"][-1]["is_enabled"] = False
    elif problem == "extra_device":
        report["chain_state"]["track"]["devices"].insert(-1, {"class_name": "Saturator", "name": "Saturator"})
    elif problem == "boost":
        report["proposals"][0]["gain_db"] = 6
    else:
        report["proposals"] = [{"kind": "hf_burst_reduction"}]
    assert not availability(report)["supported"]


def seed(review, report):
    identifier = "a" * 32
    folder = review.root / identifier
    folder.mkdir()
    job = {"id": identifier, "source": "nodes", "status": "ready", "started_at": time.time(),
        "completed_at": time.time(), "reports": {"KIT": report}, "applied": {}}
    review.jobs[identifier] = job
    review._save(job)
    return {"job_id": identifier, "track": "KIT", "kind": "software_gain"}


@pytest.mark.parametrize("lose_response", [False, True])
def test_apply_cannot_repeat_after_retries_or_service_restart(tmp_path, report, lose_response):
    bridge = Bridge(report)
    bridge.fail_write = lose_response
    review = RehearsalReview(tmp_path, tmp_path, client_factory=lambda: bridge)
    data = seed(review, report)
    if lose_response:
        with pytest.raises(RuntimeError, match="Lost write"):
            review.apply(data)
    else:
        assert review.apply(data)["status"] == "applied"
    restored = RehearsalReview(tmp_path, tmp_path, client_factory=lambda: bridge)
    if lose_response:
        with pytest.raises(ValueError, match="previous apply"):
            restored.apply(data)
    else:
        assert restored.apply(data)["status"] == "applied"
    assert len(writes(bridge)) == 1
    review.close()
    restored.close()


def test_expired_and_experimental_requests_cannot_write(tmp_path, report):
    bridge = Bridge(report)
    review = RehearsalReview(tmp_path, tmp_path, client_factory=lambda: bridge)
    data = seed(review, report)
    with pytest.raises(ValueError, match="preview-only"):
        review.apply(data | {"kind": "hf_burst_reduction"})
    review.jobs[data["job_id"]]["completed_at"] -= 601
    with pytest.raises(ValueError, match="expire"):
        review.apply(data)
    assert not bridge.calls
    review.close()


def test_real_worker_prepares_stem_audio_without_daw_or_audio_device(tmp_path):
    library = tmp_path / "library"
    take = library / "test-sunday"
    take.mkdir(parents=True)
    sources = []
    t = np.arange(48000) / 48000
    for name, ports in (("RED", [1]), ("KIT", [15, 16])):
        signal = np.tile((.1*np.sin(2*np.pi*220*t))[:, None], (1, len(ports))).astype(np.float32)
        if name == "KIT":
            signal[0] = 1
        path = take / f"{name}.wav"
        sf.write(path, signal, 48000, subtype="FLOAT")
        sources.append({"track_name": name, "track_id": name, "file": path.name,
            "channels": len(ports), "emulator_inputs": ports, "frames": 48000, "sample_rate": 48000,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (take / "manifest.json").write_text(json.dumps({"kind": "wcfi_real_source_take", "schema_version": 1,
        "take_id": take.name, "frames": 48000, "sample_rate": 48000, "sources": sources}), encoding="utf-8")
    review = RehearsalReview(tmp_path / "state", library)
    try:
        started = review.start({"source": "take", "take_id": take.name, "start": 0, "seconds": 1})
        assert started["busy"]
        with pytest.raises(ValueError):
            review.start({"source": "take", "take_id": take.name, "start": 0, "seconds": 1})
        review.thread.join(timeout=30)
        result = review.snapshot()["jobs"][0]
        assert result["status"] == "ready", result
        assert set(result["reports"]) == {"RED", "KIT"}
        kit = result["reports"]["KIT"]
        assert kit["gain_preview"]["metrics"]["sample_peak_dbfs"] == pytest.approx(-6, abs=.001)
        assert not kit["apply"]["supported"] and not kit["automatic_daw_writes"]
        assert "sibilance_candidates" not in kit["after"]["analyses"]
        assert review.audio(result["id"], "KIT", "gain").startswith(b"RIFF")
        with pytest.raises(ValueError):
            review.audio(result["id"], "KIT", "../../request.json")
    finally:
        review.close()


def test_review_http_requires_auth_and_never_accepts_audio_paths(tmp_path, report):
    engine = SimpleNamespace(state_dir=tmp_path, library_root=tmp_path)
    server = EmulatorServer(engine)
    review = server.review
    data = seed(review, report)
    (review.root / data["job_id"] / "KIT-after.wav").write_bytes(b"RIFFlocal")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, authorized=True):
        headers = {"Authorization": "Bearer " + server.token} if authorized else {}
        return opener.open(urllib.request.Request(server.origin + path, headers=headers), timeout=5)

    try:
        path = f"/api/rehearsal/audio?job={data['job_id']}&track=KIT&variant=after"
        with pytest.raises(urllib.error.HTTPError) as error:
            request(path, False)
        assert error.value.code == 403
        with request(path) as response:
            assert response.read() == b"RIFFlocal"
            assert "media-src 'self' blob:" in response.headers["Content-Security-Policy"]
        with pytest.raises(urllib.error.HTTPError):
            request(path.replace("variant=after", "variant=../../request.json"))
        with request("/api/rehearsal") as response:
            assert json.load(response)["jobs"][0]["id"] == data["job_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("busy_read", [False, True])
@pytest.mark.parametrize("shared_clock", [False, True])
def test_node_capture_uses_common_clock_discards_gaps_and_closes_handles(report, busy_read, shared_clock):
    bridge = Bridge(report)
    red = copy.deepcopy(report["chain_state"]["track"])
    red.update(id="red", name="RED")
    bridge.snapshot["tracks"].append(red)

    class Nodes:
        def __init__(self):
            self.seen = set()
            self.closed = []
            self.gap_sent = False
            self.header_busy = False
            self.data_busy = False

        def discover(self):
            return ["RED0", "RED1", "KIT0", "KIT1"]

        def open(self, identity):
            return identity

        def close(self, handle):
            self.closed.append(handle)

        def clock_ns(self):
            return 1_000_000_000

        def read(self, handle, cursor):
            if busy_read and handle == "KIT0" and not self.header_busy:
                self.header_busy = True
                return None, None
            count = 4800 if handle in self.seen else 0
            if busy_read and handle == "KIT1" and count and not self.data_busy:
                self.data_busy = True
                return None, None
            self.seen.add(handle)
            first = cursor
            if count and handle == "RED0" and not self.gap_sent:
                first += 4800
                self.gap_sent = True
            channels = 1 if handle.startswith("RED") else 2
            status = SimpleNamespace(label=handle[:3].encode(), position=int(handle[-1]), pid=123,
                sample_rate=48000, channels=channels, flags=int(shared_clock), active=1, callback_ns=self.clock_ns(),
                first_frame=first, written_frame=first+count, host_sample=10000+first,
                block_size=count, epoch=1)
            return np.full((count, 2), .1, np.float32), status

    native = Nodes()
    ready = threading.Event()
    result = capture_pairs(bridge, .5, bridge_factory=lambda: native, timeout=2,
                           ready_event=ready)
    assert ready.is_set()
    assert len(native.closed) == 4
    if shared_clock:
        assert result["RED"]["provenance"]["start_host_frame"] >= 14800
        assert result["RED"]["provenance"]["start_host_frame"] == result["KIT"]["provenance"]["start_host_frame"]
    else:
        assert result["RED"]["provenance"]["start_host_frame"] is None
        assert "unverified" in result["RED"]["provenance"]["clock_mode"]
        assert result["RED"]["provenance"]["independent_counter_windows"]["pre_fx"]["end"] > 0
    assert result["RED"]["shared_time_available"] is shared_clock
    assert result["RED"]["before"].shape == (24000, 1)
    assert result["KIT"]["after"].shape == (24000, 2)
    assert result["KIT"]["provenance"]["retryable_reads"] == (2 if busy_read else 0)
    assert all(op == "inspect_session" for op, _, _ in bridge.calls)


def test_node_capture_rejects_cross_host_and_closes_handles(report):
    class WrongHost:
        closed = False

        def discover(self):
            return ["node"]

        def open(self, identity):
            return identity

        def read(self, *_):
            return None, SimpleNamespace(label=b"KIT", position=0, pid=456)

        def close(self, _):
            self.closed = True

    nodes = WrongHost()
    bridge = Bridge(report)
    red = copy.deepcopy(bridge.snapshot["tracks"][0])
    red.update(id="red", name="RED")
    bridge.snapshot["tracks"].append(red)
    ready = threading.Event()
    with pytest.raises(ValueError, match="different hosts"):
        capture_pairs(bridge, .5, bridge_factory=lambda: nodes, ready_event=ready)
    assert not ready.is_set()
    assert nodes.closed and not writes(bridge)


def test_worker_withholds_red_hf_treatment_without_shared_track_timing(tmp_path, monkeypatch):
    from wcfi_tools.audio.review_worker import run
    from wcfi_tools.audio.role_validation import _bursts

    t = np.arange(8 * 48000) / 48000
    body = (.03*np.sin(2*np.pi*220*t))[:, None].astype(np.float32)
    burst, _ = _bursts(body, 48000)
    captures = {name: {"before": audio, "after": audio, "rate": 48000,
        "shared_time_available": False, "state": None,
        "provenance": {"post_fx_kind": "current_node_capture"}}
        for name, audio in (("RED", burst), ("KIT", body))}
    monkeypatch.setattr("wcfi_tools.ableton.client.connect", lambda: None)
    monkeypatch.setattr("wcfi_tools.audio.review_capture.capture_pairs", lambda *_: captures)
    run({"source": "nodes", "seconds": 8}, tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    red = result["RED"]
    hf = red["after"]["analyses"]["sibilance_candidates"]
    assert hf["candidate_count"] >= 3 and hf["treatment_withheld_reason"]
    assert not red["paired_alignment_available"]
    assert not any(p["kind"] == "hf_burst_reduction" for p in red["proposals"])
    assert not any(p["kind"] == "hf_burst_reduction" for p in red["candidate"]["applied"])
