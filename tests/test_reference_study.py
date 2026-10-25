"""Reference matching descriptors, bounded import and legacy UI compatibility."""
from __future__ import annotations

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

from wcfi_tools.audio.reference_study import (
    PASSAGES,
    REFERENCE_AUDIO,
    describe_mix,
    priorities,
    reviewed_reference,
)
from wcfi_tools.audio.review_service import RehearsalReview
from wcfi_tools.audio.service import EmulatorServer


def test_stereo_descriptors_do_not_cancel_antiphase_or_confuse_gain_with_tone():
    t = np.arange(48000)/48000
    mono = .1*np.sin(2*np.pi*220*t)
    stereo = np.column_stack((mono, mono))
    a, b = describe_mix(stereo, 48000), describe_mix(stereo*.1, 48000)
    assert a["side_energy_percent"] == pytest.approx(0)
    assert a["lr_correlation"] == pytest.approx(1)
    assert b["rms_dbfs"] == pytest.approx(a["rms_dbfs"]-20)
    assert b["band_energy_percent"] == pytest.approx(a["band_energy_percent"])
    anti = describe_mix(np.column_stack((mono, -mono)), 48000)
    assert anti["side_energy_percent"] == pytest.approx(100)
    assert anti["rms_dbfs"] == pytest.approx(a["rms_dbfs"])
    assert anti["lr_correlation"] == pytest.approx(-1)
    assert anti["mono_energy_change_db"] < -100


def test_one_different_excerpt_cannot_become_a_repeated_difference_priority():
    metric = {key: 0 for key in ("side_energy_percent", "low_mid_over_presence_db", "sub_over_presence_db",
                                "low_end_over_low_mid_db", "high_over_presence_db")}
    pairs = [{"wcfi": {"metrics": dict(metric)}, "bethel": {"metrics": dict(metric)}} for _ in range(3)]
    pairs[0]["wcfi"]["metrics"]["high_over_presence_db"] = -12
    assert not any(f["id"] == "brightness" for f in priorities(pairs, []))
    pairs[1]["wcfi"]["metrics"]["high_over_presence_db"] = -9
    results = priorities(pairs, [{"track": "KIT", "full_scale_samples": 10, "possible_flat_top_samples": 20}])
    assert results[0]["id"] == "capture"
    assert next(f for f in results if f["id"] == "brightness")["evidence_type"] == "reference_difference_hypothesis"
    assert all(not f["automatically_apply"] for f in results)


def test_excluded_jams_cannot_drive_brightness_or_repeatability_claims():
    metric = {key: 0 for key in ("side_energy_percent", "low_mid_over_presence_db", "sub_over_presence_db",
                                "low_end_over_low_mid_db", "high_over_presence_db")}
    pairs = [{"id": p, "label": p, "duration_seconds": 12,
        "wcfi": {"metrics": dict(metric), "start_seconds": n*20}, "bethel": {"metrics": dict(metric)}}
        for n, p in enumerate(PASSAGES)]
    for pair in pairs:
        pair["wcfi"]["metrics"]["side_energy_percent"] = 1
        pair["bethel"]["metrics"]["side_energy_percent"] = 24
        pair["wcfi"]["metrics"]["high_over_presence_db"] = -2 if pair["id"] == "loud" else -12
    report = {"pairs": pairs, "source_audit": [
        {"track": "EGUITAR", "passage": "quiet", "full_scale_samples": 100, "possible_flat_top_samples": 50},
        {"track": "KIT", "passage": "loud", "full_scale_samples": 10, "possible_flat_top_samples": 20}]}
    before = copy.deepcopy(report)
    reviewed = reviewed_reference(report, {p: {"include": p == "loud", "reason": "Human review"} for p in PASSAGES})
    assert report == before
    assert [p["id"] for p in reviewed["pairs"]] == ["loud"]
    assert len(reviewed["source_audit"]) == 1
    assert not any(f["id"] == "brightness" for f in reviewed["findings"])
    assert "EGUITAR" not in reviewed["findings"][0]["evidence"]
    space = next(f for f in reviewed["findings"] if f["id"] == "space")
    assert "one approved passage" in space["evidence"] and "untested" in space["evidence"]
    assert not any("/3" in f["evidence"] or "median" in f["evidence"] for f in reviewed["findings"])
    empty = reviewed_reference(report, {})
    assert empty["pairs"] == [] and empty["findings"][0]["id"] == "selection"


@pytest.fixture
def staged(tmp_path):
    review = RehearsalReview(tmp_path/"state", tmp_path)
    identifier = "d"*32
    folder = review.root/"reference-staging"/identifier
    folder.mkdir(parents=True)
    audio, files = {}, {}
    tone = np.tile((.02*np.sin(2*np.pi*440*np.arange(24000)/48000))[:, None], (1, 2)).astype(np.float32)
    for key in REFERENCE_AUDIO:
        name = f"MIX-{key}.wav"
        sf.write(folder/name, tone, 48000, subtype="FLOAT")
        files[name] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
        audio[key] = name
    source = {"matched": {"integrated_lufs": -24, "true_peak_dbtp": -30}, "start_seconds": 32,
              "metrics": {key: 0 for key in ("side_energy_percent", "low_mid_over_presence_db", "sub_over_presence_db",
                                            "low_end_over_low_mid_db", "high_over_presence_db")},
              "url": "https://www.youtube.com/watch?v=_lEAMvR3Pds&t=900s"}
    report = {"kind": "wcfi_reference_study", "audio": audio, "target_lufs": -24,
        "source_audit": [], "session_context": {},
        "pairs": [{"id": p, "label": p, "duration_seconds": .5, "wcfi": copy.deepcopy(source), "bethel": copy.deepcopy(source)} for p in PASSAGES]}
    package = {"kind": "wcfi_reference_package", "schema_version": 1, "reports": {"MIX": report}, "files": files}
    (folder/"study.json").write_text(json.dumps(package))
    yield review, identifier, folder, package
    review.close()


def test_reference_import_is_idempotent_persistent_and_does_not_apply_to_live(staged):
    review, identifier, _, _ = staged
    result = review.import_reference({"study_id": identifier})
    assert result == review.import_reference({"study_id": identifier})
    assert review.snapshot(include_references=False)["jobs"] == []
    assert review.audio(identifier, "MIX", "reference_loud_bethel_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.apply({"job_id": identifier, "track": "MIX", "kind": "software_gain"})
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "../../secret")
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
    finally:
        restored.close()


def test_operator_assessment_survives_restart_and_blocks_excluded_audio(staged):
    review, identifier, _, _ = staged
    review.import_reference({"study_id": identifier})
    data = {"job_id": identifier, "assessments": {
        p: {"include": p == "loud", "classification": "performance" if p == "loud" else "jam",
            "reason": "Coherent performance" if p == "loud" else "Musicians jamming"} for p in PASSAGES}}
    result = review.assess_reference(data)
    assert [p["id"] for p in result["jobs"][0]["reports"]["MIX"]["pairs"]] == ["loud"]
    assert len(review.jobs[identifier]["reports"]["MIX"]["pairs"]) == 3
    for passage in ("quiet", "fills"):
        with pytest.raises(ValueError, match="not approved"):
            review.audio(identifier, "MIX", f"reference_{passage}_wcfi_matched")
    assert review.audio(identifier, "MIX", "reference_loud_wcfi_matched").startswith(b"RIFF")
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
    finally:
        restored.close()
    for assessment in ({"include": "yes", "reason": "Wrong type"}, {"include": True, "reason": ""},
                       {"include": True, "classification": "jam", "reason": "Contradiction"}):
        with pytest.raises(ValueError):
            review.assess_reference({"job_id": identifier, "assessments": {"loud": assessment}})


def test_new_studies_require_performance_confirmation(staged):
    review, identifier, folder, package = staged
    package["reports"]["MIX"]["performance_confirmation_required"] = True
    (folder/"study.json").write_text(json.dumps(package))
    result = review.import_reference({"study_id": identifier})
    assert result["jobs"][0]["reports"]["MIX"]["pairs"] == []
    with pytest.raises(ValueError, match="not approved"):
        review.audio(identifier, "MIX", "reference_loud_wcfi_matched")


def named_package(folder, package):
    """Convert a legacy fixture into one independently named performance pair."""
    package["schema_version"] = 2
    report = package["reports"]["MIX"]
    report.update(reference_key="reference", reference_name="Life.Church", reference_kind="service_broadcast",
        title="WCFI ↔ Life.Church", performance_confirmation_required=True)
    pair = next(p for p in report["pairs"] if p["id"] == "loud")
    pair["reference"] = pair.pop("bethel")
    report["pairs"] = [pair]
    audio, files = {}, {}
    for source in ("wcfi", "reference"):
        key = f"reference_loud_{source}_matched"
        filename = f"MIX-{key}.wav"
        old = f"MIX-reference_loud_{'bethel' if source == 'reference' else source}_matched.wav"
        if filename != old:
            (folder/filename).write_bytes((folder/old).read_bytes())
        audio[key] = filename
        files[filename] = package["files"][old]
    report["audio"], package["files"] = audio, files
    return report


def test_named_reference_is_separate_bounded_and_backward_compatible(staged):
    review, identifier, folder, package = staged
    report = named_package(folder, package)
    (folder/"study.json").write_text(json.dumps(package))
    review.import_reference({"study_id": identifier})
    assert not review.snapshot(include_named_references=False)["jobs"]
    assert review.snapshot()["jobs"][0]["reports"]["MIX"]["reference_name"] == "Life.Church"
    with pytest.raises(ValueError, match="not approved"):
        review.audio(identifier, "MIX", "reference_loud_reference_matched")
    data = {"job_id": identifier, "assessments": {"loud": {"include": True, "reason": "Existing WCFI operator review"}}}
    result = review.assess_reference(data)
    projected = result["jobs"][0]["reports"]["MIX"]
    assert len(projected["pairs"]) == 1
    assert "Life.Church" in projected["findings"][-1]["next_test"]
    assert "Bethel" not in json.dumps(projected)
    assert review.audio(identifier, "MIX", "reference_loud_reference_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "reference_loud_bethel_matched")
    assert report["pairs"][0]["wcfi"]["start_seconds"] == 32
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
    finally:
        restored.close()


@pytest.mark.parametrize("problem", ["key", "name", "kind", "confirmation", "pair", "extra_asset"])
def test_named_reference_rejects_invalid_metadata_and_assets(staged, problem):
    review, identifier, folder, package = staged
    report = named_package(folder, package)
    if problem == "key":
        report["reference_key"] = "../../outside"
    elif problem == "name":
        report["reference_name"] = ""
    elif problem == "kind":
        report["reference_kind"] = "studio_guess"
    elif problem == "confirmation":
        report["performance_confirmation_required"] = False
    elif problem == "pair":
        report["pairs"][0]["id"] = "../outside"
    else:
        package["files"]["MIX-reference_quiet_wcfi_matched.wav"] = "0"*64
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_reference({"study_id": identifier})
    assert not review.snapshot()["jobs"]


@pytest.mark.parametrize("problem", ["checksum", "filename", "duration", "pairs", "loudness", "link", "version"])
def test_invalid_reference_cannot_publish(staged, problem):
    review, identifier, folder, package = staged
    report = package["reports"]["MIX"]
    if problem == "checksum":
        package["files"][next(iter(package["files"]))] = "0"*64
    elif problem == "filename":
        report["audio"][next(iter(report["audio"]))] = "../outside.wav"
    elif problem == "duration":
        report["pairs"][0]["duration_seconds"] = float("nan")
    elif problem == "pairs":
        report["pairs"][0]["id"] = "loud"
    elif problem == "loudness":
        report["pairs"][0]["bethel"]["matched"]["integrated_lufs"] = -12
    elif problem == "link":
        report["pairs"][0]["bethel"]["url"] = "javascript:alert(1)"
    else:
        package["schema_version"] = True
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_reference({"study_id": identifier})
    assert not review.snapshot()["jobs"]


def test_reference_cannot_evict_kit_studies_or_captures_on_restart(staged):
    review, identifier, _, _ = staged
    review.import_reference({"study_id": identifier})
    expected = {identifier}
    for index, source in enumerate(("study",)*4+("nodes",)*4):
        key = f"{index:032x}"
        (review.root/key).mkdir()
        job = {"id": key, "source": source, "status": "ready", "started_at": index, "reports": {}, "applied": {}}
        review.jobs[key] = job
        review._save(job)
        if index not in (0, 4):
            expected.add(key)
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert set(restored.jobs) == expected
        assert restored.audio(identifier, "MIX", "reference_quiet_wcfi_matched").startswith(b"RIFF")
    finally:
        restored.close()


def test_reference_http_is_authenticated_and_hidden_from_older_ui(staged):
    review, identifier, _, _ = staged
    review.import_reference({"study_id": identifier})
    server = EmulatorServer(SimpleNamespace(state_dir=review.root.parent, library_root=review.library_root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, authorized=True):
        headers = {"Authorization": "Bearer "+server.token} if authorized else {}
        return opener.open(urllib.request.Request(server.origin+path, headers=headers), timeout=5)
    try:
        with request("/api/rehearsal") as response:
            assert json.load(response)["jobs"] == []
        with request("/api/rehearsal?include_references=1") as response:
            assert json.load(response)["jobs"][0]["id"] == identifier
        path = f"/api/rehearsal/audio?job={identifier}&track=MIX&variant=reference_loud_bethel_matched"
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(path, False)
        assert exc.value.code == 403
        with request(path) as response:
            assert response.read().startswith(b"RIFF")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
