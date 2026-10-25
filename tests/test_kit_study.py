"""Local musical hypotheses must not masquerade as native renders or arbitrary paths."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_study import (
    BYPASS_STUDY_AUDIO,
    STUDY_AUDIO,
    align_cycle,
    allowed_session_change,
    build_study,
    low_shelf,
    match_kit_contribution,
    study_audio_keys,
    validate_linear_path,
    verify_mix_delta,
)
from wcfi_tools.audio.review_service import RehearsalReview


@pytest.fixture
def signal():
    rng = np.random.default_rng(721)
    return rng.normal(0, .05, (48000, 2)).astype(np.float32)


def test_cycle_alignment_establishes_a_sample_match_not_latency(signal):
    matched, result = align_cycle(signal, np.roll(signal, 7913, axis=0))
    assert np.array_equal(matched, signal)
    assert result["roll_target_samples"] == -7913
    assert "not_latency" in result["method"]
    with pytest.raises(ValueError):
        align_cycle(signal, np.flip(signal, axis=0))


def test_linear_null_rejects_wrong_gain_and_nonlinear_master_path(signal):
    result, error = verify_mix_delta(signal, np.roll(signal, 128, axis=0))
    assert result["roll_target_samples"] == -128 and error < 1e-10
    with pytest.raises(ValueError, match="did not null"):
        verify_mix_delta(signal*.8, signal)
    with pytest.raises(ValueError):
        verify_mix_delta(np.clip(signal, -.04, .04), signal)
    with pytest.raises(ValueError):
        verify_mix_delta(np.zeros_like(signal), signal)


def test_low_shelf_is_bounded_stereo_and_periodic(signal):
    source = signal.copy()
    zero = low_shelf(signal, 48000, gain_db=0)
    np.testing.assert_array_equal(zero, signal)
    cut = low_shelf(signal, 48000)
    assert cut.shape == signal.shape and np.isfinite(cut).all()
    np.testing.assert_allclose(low_shelf(np.roll(signal, 713, axis=0), 48000),
                               np.roll(cut, 713, axis=0), atol=1e-7, rtol=0)
    np.testing.assert_array_equal(source, signal)
    with pytest.raises(ValueError):
        low_shelf(signal, 48000, gain_db=6)


def test_low_shelf_has_expected_low_and_high_frequency_limits():
    t = np.arange(48000) / 48000
    for hz, target in ((10, -2), (5000, 0)):
        audio = np.column_stack((np.sin(2*np.pi*hz*t), -np.sin(2*np.pi*hz*t))).astype(np.float32)
        out = low_shelf(audio, 48000)
        change = 20*np.log10(np.sqrt(np.mean(out.astype(float)**2))/np.sqrt(np.mean(audio.astype(float)**2)))
        assert abs(change-target) < .01
        np.testing.assert_array_equal(out[:, 0], -out[:, 1])


@pytest.fixture
def snapshot():
    def track(name, kind="track"):
        return {"id": name, "name": name, "kind": kind, "mute": False, "solo": False,
            "volume": {"display": "0.0 dB", "automation_state": 0}, "pan": {"value": 0},
            "group_id": None, "routing": {"output_routing_type": "Master"}, "sends": [], "devices": []}
    kit, group, master = track("KIT"), track("INSTRUMENTS"), track("Master", "master")
    kit["group_id"] = group["id"]
    kit["routing"]["output_routing_type"] = group["name"]
    kit["devices"] = [{"class_name": "Compressor2", "parameters": [
        {"name": "Attack", "value": .4, "display": "1.00 ms"},
        {"name": "Output Gain", "value": -5.88, "display": "-5.88 dB"}]}]
    master["devices"] = [{"name": "WCFI Analysis Node"}, {"name": "WCFI Analysis Node"}]
    return {"tracks": [kit, group, master], "session_id": "session", "inspected_at": 1}


def test_only_exact_native_attack_change_is_accepted(snapshot):
    candidate = copy.deepcopy(snapshot)
    attack = candidate["tracks"][0]["devices"][0]["parameters"][0]
    attack.update(value=.635, display="15.0 ms")
    assert allowed_session_change(snapshot, candidate)[0]["after"] == "15.0 ms"
    candidate["tracks"][0]["devices"][0]["parameters"][1]["value"] = 0
    with pytest.raises(ValueError):
        allowed_session_change(snapshot, candidate)


def test_native_bypass_cannot_hide_changes_to_output_gain(snapshot):
    parameters = snapshot["tracks"][0]["devices"][0]["parameters"]
    parameters.append({"name": "Device On", "value": 1, "display": "On"})
    candidate = copy.deepcopy(snapshot)
    candidate["tracks"][0]["devices"][0]["parameters"][-1].update(value=0, display="Off")
    assert allowed_session_change(snapshot, candidate, parameter="Device On")[0]["after"] == "Off"
    candidate["tracks"][0]["devices"][0]["parameters"][1]["value"] = 0
    with pytest.raises(ValueError):
        allowed_session_change(snapshot, candidate, parameter="Device On")
    with pytest.raises(ValueError):
        allowed_session_change(snapshot, snapshot, parameter="Device On")


def test_matching_kit_removes_pure_gain_confounds_without_changing_band(signal):
    background = np.roll(signal, 97, axis=0)*.4
    kits = {"current": signal, "bypass": signal*2, "compression": signal*.5}
    mixes = {v: background+kit for v, kit in kits.items()}
    matched, gains = match_kit_contribution(kits, mixes,
        {"current": -20, "bypass": -20+20*np.log10(2), "compression": -20+20*np.log10(.5)})
    for v, kit in kits.items():
        np.testing.assert_allclose(matched[v], background+signal, atol=1e-7, rtol=0)
        np.testing.assert_allclose(matched[v]-kit*10**(gains[v]/20), background, atol=1e-7, rtol=0)
    measured = {v: -24-level for v, level in {"current": -20,
        "bypass": -20+20*np.log10(2), "compression": -20+20*np.log10(.5)}.items()}
    calibrated, _ = match_kit_contribution(kits, mixes, {v: -20 for v in kits}, measured_match_gains=measured)
    np.testing.assert_allclose(calibrated["bypass"], background+signal, atol=1e-7, rtol=0)
    with pytest.raises(ValueError):
        match_kit_contribution(kits, mixes, {"current": -20})
    with pytest.raises(ValueError):
        match_kit_contribution(kits, mixes, {"current": -20, "bypass": float("nan"), "compression": -100})


def test_audio_version_allowlists_are_finite_and_legacy_compatible():
    assert study_audio_keys(1) == STUDY_AUDIO and len(STUDY_AUDIO) == 16
    assert study_audio_keys(2) == BYPASS_STUDY_AUDIO and len(BYPASS_STUDY_AUDIO) == 20
    for version in (None, 0, 3, True, "2"):
        with pytest.raises(ValueError):
            study_audio_keys(version)


def test_bypass_state_metadata_does_not_replace_sample_proof(tmp_path, snapshot, signal):
    snapshot["transport"] = {"is_playing": False, "record_mode": False}
    snapshot["tracks"][0]["devices"][0]["parameters"].append({"name": "Device On", "value": 1, "display": "On"})
    folders = {}
    for variant, gain in (("baseline", .5), ("compression", .6), ("bypass", .8)):
        folder = tmp_path/variant
        folder.mkdir()
        folders[variant] = folder
        state = copy.deepcopy(snapshot)
        params = state["tracks"][0]["devices"][0]["parameters"]
        if variant == "compression":
            params[0].update(value=.635, display="15.0 ms")
        elif variant == "bypass":
            params[-1].update(value=0, display="Off")
        (folder/"KIT.json").write_text(json.dumps({"session_snapshot": state}))
        for filename, audio in (("KIT-before", signal), ("KIT-after", signal*gain),
                                ("MASTER-after", signal*(gain+.2)), ("RED-after", signal*.2)):
            sf.write(folder/f"{filename}.wav", audio, 48000, subtype="FLOAT")
    with pytest.raises(ValueError, match="bypass did not reproduce"):
        build_study(folders["baseline"], folders["compression"], tmp_path/"output", ffmpeg="unused",
            restored_snapshot=snapshot, bypass_folder=folders["bypass"],
            passage={"id": "quiet", "duration_seconds": 1})
    assert not (tmp_path/"output").exists()


@pytest.mark.parametrize("problem", ["send", "group_fx", "pan", "fader", "route"])
def test_unmodelled_kit_path_cannot_authorize_mix_substitution(snapshot, problem):
    assert validate_linear_path(snapshot)["kit_contribution_gain"] == 1
    kit, group, _ = snapshot["tracks"]
    if problem == "send":
        kit["sends"] = [{"value": .1}]
    elif problem == "group_fx":
        group["devices"] = [{"name": "Compressor"}]
    elif problem == "pan":
        kit["pan"]["value"] = .3
    elif problem == "fader":
        group["volume"]["display"] = "-3.0 dB"
    else:
        kit["routing"]["output_routing_type"] = "Ext. Out"
    with pytest.raises(ValueError):
        validate_linear_path(snapshot)


@pytest.fixture
def staged(tmp_path):
    review = RehearsalReview(tmp_path / "state", tmp_path)
    identifier = "c"*32
    folder = review.root / "study-staging" / identifier
    folder.mkdir(parents=True)
    files, audio = {}, {}
    for key in STUDY_AUDIO:
        name = "KIT-"+key+".wav"
        sf.write(folder/name, np.zeros((24000, 2), np.float32), 48000, subtype="FLOAT")
        files[name] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
        audio[key] = name
    package = {"kind": "wcfi_kit_study_package", "schema_version": 1,
        "reports": {"KIT": {"kind": "wcfi_kit_context_study", "comparison": {"duration_seconds": .5},
                            "audio": audio, "proposals": [], "provenance": {"post_fx_kind": "captured_context_study"}}},
        "files": files}
    (folder/"study.json").write_text(json.dumps(package), encoding="utf-8")
    yield review, identifier, folder, package
    review.close()


def test_study_publish_is_idempotent_and_audio_stays_authenticated(staged):
    review, identifier, _, _ = staged
    result = review.import_study({"study_id": identifier})
    assert result["jobs"][0]["source"] == "study"
    assert review.import_study({"study_id": identifier}) == result
    assert review.audio(identifier, "KIT", "study_mix_current_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.audio(identifier, "KIT", "../../../secret")
    with pytest.raises(ValueError):
        review.import_study({"study_id": "../outside"})


def test_bypass_study_publishes_bounded_passage_and_only_declared_audio(staged):
    review, identifier, folder, package = staged
    for key in BYPASS_STUDY_AUDIO-STUDY_AUDIO:
        name = f"KIT-{key}.wav"
        sf.write(folder/name, np.zeros((24000, 2), np.float32), 48000, subtype="FLOAT")
        package["files"][name] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
        package["reports"]["KIT"]["audio"][key] = name
    package["schema_version"] = 2
    passage = {"id": "quiet", "label": "Quieter playing", "start_seconds": 110, "duration_seconds": .5}
    package["reports"]["KIT"]["comparison"]["passage"] = passage
    for invalid in (passage | {"start_seconds": float("nan")}, passage | {"id": "../secret"},
                    passage | {"duration_seconds": 20}, passage | {"label": "x"*101}):
        package["reports"]["KIT"]["comparison"]["passage"] = invalid
        (folder/"study.json").write_text(json.dumps(package))
        with pytest.raises(ValueError):
            review.import_study({"study_id": identifier})
    package["reports"]["KIT"]["comparison"]["passage"] = passage
    (folder/"study.json").write_text(json.dumps(package))
    result = review.import_study({"study_id": identifier})
    assert result["jobs"][0]["request"]["start"] == 110
    assert review.audio(identifier, "KIT", "study_mix_bypass_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.audio(identifier, "KIT", "study_mix_unknown")


def test_new_captures_cannot_evict_saved_comparison_even_after_restart(staged):
    review, identifier, _, _ = staged
    review.import_study({"study_id": identifier})
    created = time.time()
    captures = []
    for index in range(6):
        capture_id = f"{index:032x}"
        captures.append(capture_id)
        folder = review.root / capture_id
        folder.mkdir()
        job = {"id": capture_id, "source": "nodes", "status": "ready", "started_at": created+index,
               "reports": {}, "applied": {}}
        review.jobs[capture_id] = job
        review._save(job)
        os.utime(folder, (created+index+1, created+index+1))
        review._prune()
        assert review.audio(identifier, "KIT", "study_mix_current_matched").startswith(b"RIFF")
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert set(restored.jobs) == {identifier, *captures[-3:]}
        assert restored.audio(identifier, "KIT", "study_mix_current_matched").startswith(b"RIFF")
    finally:
        restored.close()


def test_restart_enforces_separate_finite_capture_and_comparison_budgets(tmp_path):
    review = RehearsalReview(tmp_path / "state", tmp_path)
    created = time.time()
    expected = {}
    for source in ("study", "nodes"):
        identifiers = []
        for index in range(4):
            identifier = f"{len(review.jobs):032x}"
            identifiers.append(identifier)
            folder = review.root / identifier
            folder.mkdir()
            job = {"id": identifier, "source": source, "status": "ready", "started_at": created+len(review.jobs),
                   "reports": {}, "applied": {}}
            review.jobs[identifier] = job
            review._save(job)
            os.utime(folder, (job["started_at"], job["started_at"]))
        expected[source] = set(identifiers[-3:])
    restored = RehearsalReview(review.root.parent, tmp_path)
    try:
        for source, identifiers in expected.items():
            assert {key for key, value in restored.jobs.items() if value["source"] == source} == identifiers
        assert {p.name for p in restored.root.iterdir()} == set(restored.jobs)
    finally:
        review.close()
        restored.close()


@pytest.mark.parametrize("problem", ["filename", "checksum", "duration"])
def test_untrusted_study_manifest_cannot_publish_bad_assets(staged, problem):
    review, identifier, folder, package = staged
    if problem == "filename":
        package["reports"]["KIT"]["audio"]["study_mix_current"] = "../secret.wav"
    elif problem == "checksum":
        package["files"]["KIT-study_mix_current.wav"] = "0"*64
    else:
        package["reports"]["KIT"]["comparison"]["duration_seconds"] = 20
    (folder/"study.json").write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(ValueError):
        review.import_study({"study_id": identifier})
    assert not review.snapshot()["jobs"]
