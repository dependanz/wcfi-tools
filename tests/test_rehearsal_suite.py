"""Cross-session source policy, verified auditions and immutable operator judgments."""
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.rehearsal_suite import PASSAGES, SCOPES, correction_plan, suite_audio_keys
from wcfi_tools.audio.review_service import RehearsalReview
from wcfi_tools.audio.role_analysis import analyze_pair
from wcfi_tools.audio.service import EmulatorServer


def report_for(role, amplitude):
    t = np.arange(24000)/48000
    audio = np.repeat((amplitude*np.sin(2*np.pi*300*t))[:, None], 2, axis=1).astype(np.float32)
    on = {"name": "Device On", "value": 1}
    nodes = [{"name": "WCFI Analysis Node", "parameters": [on]} for _ in range(2)]
    gain = {"id": "gain", "name": "Output Gain", "value": -2, "min": -36, "max": 36,
            "is_quantized": False, "is_enabled": True, "automation_state": 0}
    compressor = {"name": "Compressor", "class_name": "Compressor2", "chains": [],
        "parameters": [on, {"name": "Makeup", "value": 0}, {"name": "Dry/Wet", "value": 1}, gain]}
    state = {"session_id": "test", "track": {"id": role, "name": role.upper(),
        "volume": {"value": .85, "display": "0.0 dB", "automation_state": 0},
        "devices": [nodes[0], compressor, nodes[1]]}}
    report = analyze_pair(audio, audio, 48000, role, aligned=True, chain_state=state)
    report["provenance"] = {"post_fx_kind": "current_node_capture"}
    return report


def test_policy_keeps_healthy_and_quiet_sources_and_never_boosts():
    for role in ("red", "kit"):
        for amplitude in (0, .1, .505):
            report = report_for(role, amplitude)
            plan = correction_plan(report)
            assert plan["kind"] == "keep" and plan["gain_db"] == 0
    # Meter observations still retain the strict configured ceiling; the edit
    # policy has a separate dead band to avoid chasing a tenth of a decibel.
    assert report_for("kit", .505)["proposals"]


def test_red_fader_is_explicitly_downstream_and_kit_uses_a_verified_gain_parameter():
    red = report_for("red", .9)
    plan = correction_plan(red)
    assert plan["kind"] == "fader" and plan["target_stage"] == "after_mixer"
    assert plan["gain_db"] == pytest.approx(-6-red["after"]["metrics"]["sample_peak_dbfs"])
    assert "after-effects node level is unchanged" in plan["reason"]
    kit = report_for("kit", .9)
    plan = correction_plan(kit)
    assert plan["kind"] == "parameter" and plan["parameter_id"] == "gain"
    assert plan["to_value"] == pytest.approx(-2+plan["gain_db"])
    kit["chain_state"]["track"]["devices"][1]["parameters"][2]["value"] = .5
    assert correction_plan(kit)["kind"] == "keep"
    red["chain_state"]["track"]["volume"]["automation_state"] = 1
    assert correction_plan(red)["kind"] == "keep"


@pytest.fixture
def staged_suite(tmp_path):
    review = RehearsalReview(tmp_path/"state", tmp_path/"sources")
    identifier = "a"*32
    folder = review.root/"suite-staging"/identifier
    folder.mkdir(parents=True)
    audio = np.full((24000, 2), .01, np.float32)
    template = folder/"template.wav"
    sf.write(template, audio, 48000, subtype="FLOAT")
    payload = template.read_bytes()
    template.unlink()
    files, audio_map = {}, {}
    for key in suite_audio_keys():
        name = f"MIX-{key}.wav"
        (folder/name).write_bytes(payload)
        files[name] = hashlib.sha256(payload).hexdigest()
        audio_map[key] = name
    passages = []
    for identifier_p in PASSAGES:
        passages.append({"id": identifier_p, "duration_seconds": .5, "operator_approved": False,
            "start_seconds": 32, "date": "2026-10-18", "take_id": identifier_p, "take_title": identifier_p,
            "tracks": {name: {"decision": {"kind": "keep", "gain_db": 0}} for name in ("RED", "KIT")},
            "validation": {"full_mix_delta_relative_error": 0, "candidate_native_capture": False,
                           "identical_control": True},
            "listening": {scope: {"target_lufs": -24, "matched": {v: {
                "integrated_lufs": -24, "true_peak_dbtp": -30} for v in variants}}
                for scope, variants in SCOPES.items()}})
    report = {"kind": "wcfi_rehearsal_suite", "restored": True, "preference_established": False,
        "title": "Three sessions", "passages": passages, "audio": audio_map}
    package = {"kind": "wcfi_rehearsal_suite_package", "schema_version": 1,
               "reports": {"MIX": report}, "files": files}
    (folder/"study.json").write_text(json.dumps(package))
    yield review, identifier, folder, package
    review.close()


def test_suite_import_and_judgments_survive_restart_without_mutating_measurements(staged_suite):
    review, identifier, _, _ = staged_suite
    initial = review.import_suite({"study_id": identifier})
    assert review.import_suite({"study_id": identifier}) == initial
    assert not review.snapshot(include_suites=False)["jobs"]
    assert review.audio(identifier, "MIX", "suite_oct04_red_raw_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "../../outside")
    immutable = copy.deepcopy(review.jobs[identifier]["reports"])
    result = review.assess_suite({"job_id": identifier, "passage_id": "oct04", "include": True,
        "verdict": "worse", "reason": "Lead harder to hear; reject the candidate."})
    assert result["jobs"][0]["reports"]["MIX"]["passages"][0]["review"]["verdict"] == "worse"
    assert review.jobs[identifier]["reports"] == immutable
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
        with pytest.raises(ValueError):
            restored.assess_suite({"job_id": identifier, "passage_id": "other", "include": True,
                "verdict": "better", "reason": "Wrong case"})
        with pytest.raises(ValueError):
            restored.apply({"job_id": identifier, "track": "MIX", "kind": "software_gain"})
    finally:
        restored.close()


@pytest.mark.parametrize("problem", ["restoration", "delta", "fake_native", "boost", "matching", "checksum", "path", "identity"])
def test_unverified_or_mislabelled_suites_cannot_publish(staged_suite, problem):
    review, identifier, folder, package = staged_suite
    report = package["reports"]["MIX"]
    passage = report["passages"][0]
    if problem == "restoration":
        report["restored"] = False
    elif problem == "delta":
        passage["validation"]["full_mix_delta_relative_error"] = .01
    elif problem == "fake_native":
        passage["validation"]["candidate_native_capture"] = True
    elif problem == "boost":
        passage["tracks"]["RED"]["decision"].update(kind="fader", gain_db=3)
    elif problem == "matching":
        passage["listening"]["mix"]["matched"]["candidate"]["integrated_lufs"] = -18
    elif problem == "checksum":
        package["files"][next(iter(package["files"]))] = "0"*64
    elif problem == "path":
        report["audio"][next(iter(report["audio"]))] = "../../outside.wav"
    else:
        passage["operator_approved"] = "yes"
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_suite({"study_id": identifier})
    assert not review.jobs


def test_suite_retention_never_evicts_the_existing_balance_reference_or_kit_studies(staged_suite):
    review, identifier, _, _ = staged_suite
    review.import_suite({"study_id": identifier})
    for index, source in enumerate(("balance",)*4+("reference",)*3+("study",)*3+("suite",)*2):
        key = f"{index:032x}"
        (review.root/key).mkdir()
        review.jobs[key] = {"id": key, "source": source, "status": "ready", "reports": {}}
        review._save(review.jobs[key])
    review._prune()
    assert identifier not in review.jobs
    assert len(review.jobs) == 12


def test_suite_http_is_authenticated_and_requires_an_explicit_client_opt_in(staged_suite):
    review, identifier, _, _ = staged_suite
    review.import_suite({"study_id": identifier})
    engine = SimpleNamespace()
    server = EmulatorServer(engine, token="t"*32)
    server._review = review
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        def get(query, authenticated=True):
            headers = {"Authorization": "Bearer "+server.token} if authenticated else {}
            return opener.open(urllib.request.Request(server.origin+query, headers=headers), timeout=5)
        with pytest.raises(urllib.error.HTTPError) as error:
            get("/api/rehearsal?role_suites=1", False)
        assert error.value.code == 403
        with get("/api/rehearsal") as response:
            assert not json.load(response)["jobs"]
        with get("/api/rehearsal?role_suites=1") as response:
            assert json.load(response)["jobs"][0]["source"] == "suite"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
