"""Full-mix attribution and immutable, authenticated local experiment packages."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.mix_balance import (
    AUDIO_KEYS,
    balance_audio_keys,
    bell_cut,
    rank_low_mid,
    substitute_source,
)
from wcfi_tools.audio.review_capture import capture_pairs
from wcfi_tools.audio.review_service import RehearsalReview


def test_correlated_bleed_and_cancellation_are_not_misreported_as_positive_power_shares():
    t = np.arange(48000)/48000
    a = np.repeat((.2*np.sin(2*np.pi*300*t)+.02*np.sin(2*np.pi*2200*t))[:, None], 2, axis=1)
    b = np.repeat((-.1*np.sin(2*np.pi*300*t)+.01*np.sin(2*np.pi*1000*t))[:, None], 2, axis=1)
    ranked, validation = rank_low_mid(np.roll(a+b, 1543, axis=0), {"A": a, "B": b})
    assert validation["sum_relative_error"] < 1e-10
    assert validation["attribution_sum_percent"] == pytest.approx(100)
    assert ranked[0]["signed_mix_low_mid_percent"] == pytest.approx(200, abs=.01)
    assert ranked[1]["signed_mix_low_mid_percent"] == pytest.approx(-100, abs=.01)
    with pytest.raises(ValueError):
        rank_low_mid(a+b+.01*np.sin(2*np.pi*450*t)[:, None], {"A": a, "B": b})


def test_targeted_bell_attenuates_centre_and_preserves_upper_guitar_and_other_tracks():
    t = np.arange(48000)/48000
    guitar = np.repeat((.1*np.sin(2*np.pi*330*t)+.04*np.sin(2*np.pi*2000*t))[:, None],2,axis=1).astype(np.float32)
    other = np.repeat((.05*np.sin(2*np.pi*1500*t))[:, None],2,axis=1).astype(np.float32)
    eq = bell_cut(guitar)
    spectrum = np.abs(np.fft.rfft(eq[:,0]))/np.maximum(np.abs(np.fft.rfft(guitar[:,0])),1e-20)
    assert 20*np.log10(spectrum[330]) == pytest.approx(-3,abs=.001)
    assert abs(20*np.log10(spectrum[2000])) < .1
    assert bell_cut(guitar,gain_db=0) == pytest.approx(guitar,abs=1e-7)
    result = substitute_source(guitar+other,guitar,eq,sum_relative_error=.00004)
    assert result-eq == pytest.approx(other,abs=2e-8)
    with pytest.raises(ValueError):
        substitute_source(guitar+other,guitar,eq,sum_relative_error=.02)
    for settings in ({"gain_db":3},{"hz":60},{"q":20},{"hz":float("nan")}):
        with pytest.raises(ValueError):
            bell_cut(guitar,**settings)


def test_generic_capture_has_no_red_dependency_and_never_writes_or_controls_transport():
    snapshot = {"session_id": "test", "tracks": [{"id": "g", "name": "EGUITAR", "kind": "track"},
        {"id": "m", "name": "Master", "kind": "master"}]}
    calls, closed, seen = [], [], set()
    class Client:
        def __init__(self):
            self.descriptor = {"pid": 42}
        def call(self, operation):
            calls.append(operation)
            assert operation == "inspect_session"
            return copy.deepcopy(snapshot)
    class Nodes:
        def discover(self):
            return ["EGUITAR0", "EGUITAR1", "MASTER0", "MASTER1"]
        def open(self, identity):
            return identity
        def close(self, handle):
            closed.append(handle)
        def clock_ns(self):
            return 1_000_000
        def read(self, handle, cursor):
            count = 4800 if handle in seen else 0
            seen.add(handle)
            status = SimpleNamespace(label=handle[:-1].encode(), position=int(handle[-1]), pid=42,
                sample_rate=48000, channels=2, flags=0, active=1, callback_ns=self.clock_ns(),
                first_frame=cursor, written_frame=cursor+count, block_size=count, epoch=1)
            return np.full((count,2), .01, np.float32), status
    result = capture_pairs(Client(), .5, bridge_factory=Nodes, labels=("EGUITAR", "MASTER"), guard_session=True)
    assert set(result) == {"EGUITAR", "MASTER"}
    assert result["MASTER"]["after"].shape == (24000,2)
    assert len(closed) == 4 and calls == ["inspect_session", "inspect_session"]
    with pytest.raises(ValueError):
        capture_pairs(Client(), .5, labels=("RED", "RED"))


@pytest.fixture
def staged(tmp_path):
    review = RehearsalReview(tmp_path/"state", tmp_path)
    identifier = "b"*32
    folder = review.root/"balance-staging"/identifier
    folder.mkdir(parents=True)
    audio, files = {}, {}
    tone = np.full((24000,2), .01, np.float32)
    for key in AUDIO_KEYS:
        filename = f"MIX-{key}.wav"
        sf.write(folder/filename, tone, 48000, subtype="FLOAT")
        audio[key] = filename
        files[filename] = hashlib.sha256((folder/filename).read_bytes()).hexdigest()
    report = {"kind": "wcfi_mix_balance_study", "title": "Fader experiment", "duration_seconds": .5,
        "target_lufs": -24, "restored": True, "preference_established": False, "audio": audio,
        "passage": {"start_seconds": 32},
        "matched": {v: {"integrated_lufs": -24, "true_peak_dbtp": -30} for v in ("before", "after")}}
    package = {"kind": "wcfi_mix_balance_package", "schema_version": 1, "reports": {"MIX": report}, "files": files}
    (folder/"study.json").write_text(json.dumps(package))
    yield review, identifier, folder, package
    review.close()


def test_balance_import_persists_idempotently_and_cannot_apply_or_evict_other_studies(staged):
    review, identifier, _, _ = staged
    result = review.import_balance({"study_id": identifier})
    assert result == review.import_balance({"study_id": identifier})
    assert not review.snapshot(include_balance=False)["jobs"]
    assert review.audio(identifier, "MIX", "balance_after_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.apply({"job_id": identifier, "track": "MIX", "kind": "software_gain"})
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "../../outside")
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
        for index, source in enumerate(("reference",)*3+("study",)*3+("nodes",)*3):
            key = f"{index:032x}"
            (restored.root/key).mkdir()
            job = {"id": key, "source": source, "status": "ready", "started_at": index, "reports": {}, "applied": {}}
            restored.jobs[key] = job
            restored._save(job)
        restored._prune()
        assert len(restored.jobs) == 10
    finally:
        restored.close()


@pytest.mark.parametrize("problem", ["checksum", "filename", "duration", "matching", "restored", "version"])
def test_invalid_balance_packages_cannot_publish(staged, problem):
    review, identifier, folder, package = staged
    report = package["reports"]["MIX"]
    if problem == "checksum":
        package["files"][next(iter(package["files"]))] = "0"*64
    elif problem == "filename":
        report["audio"][next(iter(report["audio"]))] = "../outside.wav"
    elif problem == "duration":
        report["duration_seconds"] = float("nan")
    elif problem == "matching":
        report["matched"]["after"]["integrated_lufs"] = -6
    elif problem == "restored":
        report["restored"] = False
    else:
        package["schema_version"] = True
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_balance({"study_id": identifier})
    assert not review.snapshot()["jobs"]


def eq_package(folder,package):
    package['schema_version']=2
    report=package['reports']['MIX']
    report['variants']=[{'id':v,'label':v,'description':'Finite comparison excerpt',
        'origin':'local_eq_preview' if v=='eq' else 'native_capture'} for v in ('before','fader','eq')]
    report['matched']['fader']=report['matched'].pop('after')
    report['matched']['eq']=copy.deepcopy(report['matched']['fader'])
    report['audio']={key:f'MIX-{key}.wav' for key in balance_audio_keys(2)}
    template=(folder/'MIX-balance_before.wav').read_bytes()
    package['files']={}
    for filename in report['audio'].values():
        (folder/filename).write_bytes(template)
        package['files'][filename]=hashlib.sha256(template).hexdigest()
    return report


def test_three_way_eq_study_has_separate_assets_provenance_and_legacy_visibility(staged):
    review,identifier,folder,package=staged
    report=eq_package(folder,package)
    (folder/'study.json').write_text(json.dumps(package))
    result=review.import_balance({'study_id':identifier})
    assert result['jobs'][0]['study_schema_version']==2
    assert not review.snapshot(balance_schema=1)['jobs']
    assert [v['origin'] for v in report['variants']]==['native_capture','native_capture','local_eq_preview']
    for variant in ('before','fader','eq'):
        assert review.audio(identifier,'MIX',f'balance_{variant}_matched').startswith(b'RIFF')
    with pytest.raises(ValueError):
        review.audio(identifier,'MIX','balance_after_matched')
    restored=RehearsalReview(review.root.parent,review.library_root)
    try:
        assert restored.snapshot()==result
    finally:
        restored.close()


def test_performance_review_is_persistent_and_cannot_be_inferred_from_activity(staged):
    review,identifier,folder,package=staged
    report=eq_package(folder,package)
    report['passage'].update(operator_approved=False,selection_evidence='Concurrent source activity only')
    (folder/'study.json').write_text(json.dumps(package))
    review.import_balance({'study_id':identifier})
    assert not review.snapshot(include_candidates=False)['jobs']
    immutable=copy.deepcopy(review.jobs[identifier]['reports'])
    result=review.assess_balance({'job_id':identifier,'include':True,'reason':'Heard coherent playing'})
    assert result['jobs'][0]['reports']['MIX']['passage']['operator_approved']
    assert review.snapshot(include_candidates=False)['jobs']
    assert review.jobs[identifier]['reports']==immutable
    restored=RehearsalReview(review.root.parent,review.library_root)
    try:
        assert restored.snapshot()==result
        unchecked=restored.assess_balance({'job_id':identifier,'include':False,'reason':'Musicians jamming'})
        assert not unchecked['jobs'][0]['reports']['MIX']['passage']['operator_approved']
        assert not restored.snapshot(include_candidates=False)['jobs']
        assert restored.audio(identifier,'MIX','balance_before_matched').startswith(b'RIFF')
        for assessment in ({'include':'yes','reason':'bad type'},{'include':True,'reason':''}):
            with pytest.raises(ValueError):
                restored.assess_balance({'job_id':identifier,**assessment})
    finally:
        restored.close()


@pytest.mark.parametrize('problem',['provenance','missing_variant','extra_audio','description'])
def test_eq_study_cannot_mislabel_local_processing_as_a_native_capture(staged,problem):
    review,identifier,folder,package=staged
    report=eq_package(folder,package)
    if problem=='provenance':
        report['variants'][2]['origin']='native_capture'
    elif problem=='missing_variant':
        report['variants'].pop()
    elif problem=='description':
        report['variants'][2]['description']=None
    else:
        report['audio']['balance_after_matched']='MIX-balance_after_matched.wav'
    (folder/'study.json').write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_balance({'study_id':identifier})
    assert not review.snapshot()['jobs']


def targeted_package(folder, package):
    report = eq_package(folder, package)
    package["schema_version"] = 3
    report.update(target_track="AC GUITAR",
        comparison_summary="Original and level captures; local EQ on the lowered acoustic source.",
        comparison_listen_note="Compare level first, then the incremental EQ change.",
        source_findings=["Full-scale plateaus are already present in the recorded input."])
    return report


def test_targeted_study_preserves_source_identity_and_is_hidden_from_eguitar_only_clients(staged):
    review, identifier, folder, package = staged
    targeted_package(folder, package)
    (folder/"study.json").write_text(json.dumps(package))
    result = review.import_balance({"study_id": identifier})
    assert result["jobs"][0]["reports"]["MIX"]["target_track"] == "AC GUITAR"
    assert not review.snapshot(balance_schema=2)["jobs"]
    assert review.audio(identifier, "MIX", "balance_eq_matched").startswith(b"RIFF")
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
    finally:
        restored.close()


@pytest.mark.parametrize("field,value", [("target_track", ""), ("target_track", None),
    ("comparison_summary", " "), ("comparison_listen_note", 12),
    ("source_findings", []), ("source_findings", [None])])
def test_targeted_study_requires_explicit_source_and_findings(staged, field, value):
    review, identifier, folder, package = staged
    report = targeted_package(folder, package)
    report[field] = value
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_balance({"study_id": identifier})
    assert not review.snapshot()["jobs"]


def test_eight_balance_studies_fit_and_ninth_only_evicts_oldest_balance(staged):
    review, identifier, _, _ = staged
    review.import_balance({"study_id": identifier})
    other = "c"*32
    (review.root/other).mkdir()
    review.jobs[other] = {"id": other, "source": "study", "status": "ready", "reports": {}}
    review._save(review.jobs[other])
    for index in range(8):
        key = f"{index:032x}"
        (review.root/key).mkdir()
        review.jobs[key] = {"id": key, "source": "balance", "status": "ready", "reports": {}}
        review._save(review.jobs[key])
        review._prune()
        assert sum(j["source"] == "balance" for j in review.jobs.values()) == min(index+2, 8)
        assert other in review.jobs
    assert identifier not in review.jobs


def four_way_package(folder, package):
    variants = ("before", "ac_down", "kit_up", "combined")
    package["schema_version"] = 4
    report = package["reports"]["MIX"]
    report["variants"] = [{"id": v, "label": v, "origin": "native_capture",
                           "description": "Actual fader comparison"} for v in variants]
    report["matched"] = {v: {"integrated_lufs": -24, "true_peak_dbtp": -30} for v in variants}
    template = (folder/"MIX-balance_before.wav").read_bytes()
    report["audio"] = {key: f"MIX-{key}.wav" for key in balance_audio_keys(4)}
    package["files"] = {}
    for filename in report["audio"].values():
        (folder/filename).write_bytes(template)
        package["files"][filename] = hashlib.sha256(template).hexdigest()
    report["evaluator"] = {"model": "test fixture", "device": "cpu", "cpu_threads": 1,
        "logical_cpus_allowed": 1, "gpu_initialized": False, "audio_uploads": 0,
        "human_preference_validated": False, "median_inference_seconds": 5,
        "identical_control_passed": True, "repeat_controls_passed": True, "note": "Fixture, not real ratings",
        "rows": [{"variant": v, "scores": {"PQ": 5.0+i/10, "CE": 5.0, "CU": 5.0, "PC": 5.0},
                  "pq_delta_from_current": i/10,
                  "matched_audio_sha256": package["files"][f"MIX-balance_{v}_matched.wav"]}
                 for i, v in enumerate(variants)]}
    return report


def test_four_way_native_balance_with_cpu_scores_is_hidden_from_older_clients(staged):
    review, identifier, folder, package = staged
    report = four_way_package(folder, package)
    (folder/"study.json").write_text(json.dumps(package))
    result = review.import_balance({"study_id": identifier})
    assert result["jobs"][0]["study_schema_version"] == 4
    assert not review.snapshot(balance_schema=3)["jobs"]
    assert report["preference_established"] is False
    for variant in ("before", "ac_down", "kit_up", "combined"):
        assert review.audio(identifier, "MIX", f"balance_{variant}_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.apply({"job_id": identifier, "track": "MIX", "kind": "software_gain"})
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "balance_eq_matched")


@pytest.mark.parametrize("problem", ["gpu", "threads", "missing_variant", "local_preview",
    "nan", "delta", "wrong_audio", "missing_evaluator", "timing"])
def test_four_way_model_results_cannot_publish_wrong_provenance_or_scores(staged, problem):
    review, identifier, folder, package = staged
    report = four_way_package(folder, package)
    evaluator = report["evaluator"]
    if problem == "gpu": evaluator["gpu_initialized"] = True
    elif problem == "threads": evaluator["cpu_threads"] = 8
    elif problem == "missing_variant": report["variants"].pop()
    elif problem == "local_preview": report["variants"][1]["origin"] = "local_eq_preview"
    elif problem == "nan": evaluator["rows"][1]["scores"]["PQ"] = float("nan")
    elif problem == "delta": evaluator["rows"][1]["pq_delta_from_current"] = 8
    elif problem == "wrong_audio": evaluator["rows"][1]["matched_audio_sha256"] = "0"*64
    elif problem == "missing_evaluator": report["evaluator"] = None
    else: evaluator["median_inference_seconds"] = None
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_balance({"study_id": identifier})
    assert not review.snapshot()["jobs"]


def sweep_package(folder, package):
    from wcfi_tools.audio.mix_balance import balance_variants

    report = four_way_package(folder, package)
    variants = balance_variants(5)
    package["schema_version"] = 5
    report["variants"] = [{"id": v, "label": v, "origin": "native_capture",
                           "description": "Native level sweep fixture"} for v in variants]
    report["matched"] = {v: {"integrated_lufs": -24, "true_peak_dbtp": -30} for v in variants}
    template = (folder/"MIX-balance_before.wav").read_bytes()
    report["audio"] = {key: f"MIX-{key}.wav" for key in balance_audio_keys(5)}
    package["files"] = {}
    for filename in report["audio"].values():
        (folder/filename).write_bytes(template)
        package["files"][filename] = hashlib.sha256(template).hexdigest()
    report["evaluator"]["rows"] = [
        {"variant": v, "scores": {"PQ": 5.0+i/10, "CE": 5.0, "CU": 5.0, "PC": 5.0},
         "pq_delta_from_current": i/10,
         "matched_audio_sha256": package["files"][f"MIX-balance_{v}_matched.wav"]}
        for i, v in enumerate(variants)]
    report["balance_grid"] = {"default_variant": "ac12_kit6", "points": [
        {"variant": f"ac{abs(ac)}_kit{kit}", "ac_db": ac, "kit_db": kit}
        for ac in (-24, -18, -12) for kit in (0, 3, 6)]}
    return report


def test_native_sweep_exposes_all_twelve_balances_only_to_capable_clients(staged):
    review, identifier, folder, package = staged
    sweep_package(folder, package)
    (folder/"study.json").write_text(json.dumps(package))
    result = review.import_balance({"study_id": identifier})
    assert result["jobs"][0]["study_schema_version"] == 5
    assert not review.snapshot(balance_schema=4)["jobs"]
    for ac in (24, 18, 12):
        for kit in (0, 3, 6):
            assert review.audio(identifier, "MIX", f"balance_ac{ac}_kit{kit}_matched").startswith(b"RIFF")
    for variant in ("before", "ac_down", "combined"):
        assert review.audio(identifier, "MIX", f"balance_{variant}_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "balance_ac6_kit9_matched")


@pytest.mark.parametrize("problem", ["grid_missing", "duplicate", "wrong_gain", "bad_default", "wrong_rank", "missing_score"])
def test_sweep_rejects_mislabelled_levels_and_incomplete_scores(staged, problem):
    review, identifier, folder, package = staged
    report = sweep_package(folder, package)
    if problem == "grid_missing":
        report.pop("balance_grid")
    elif problem == "duplicate":
        report["balance_grid"]["points"][1] = report["balance_grid"]["points"][0]
    elif problem == "wrong_gain":
        report["balance_grid"]["points"][0]["ac_db"] = 6
    elif problem == "bad_default":
        report["balance_grid"]["default_variant"] = "before"
    elif problem == "wrong_rank":
        report["balance_grid"]["default_variant"] = "ac18_kit3"
    else:
        report["evaluator"]["rows"].pop()
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_balance({"study_id": identifier})


def foundation_package(folder, package):
    variants = ("before", "bass_0", "bass_p3", "bass_p6")
    package["schema_version"] = 6
    report = package["reports"]["MIX"]
    report["variants"] = [{"id": v, "label": v, "origin": "native_capture",
                           "description": "Actual native rhythm foundation fixture"} for v in variants]
    report["foundation"] = {"anchor_track": "KIT", "added_track": "BASS", "starting_variant": "bass_p3",
        "applied_variant": "bass_p3", "selection_method": "engineering_judgment", "audio_models_run": 0,
        "original_controls_saved": True, "explanation": "Measured provisional starting balance; no preference claim."}
    report["matched"] = {v: {"integrated_lufs": -24, "true_peak_dbtp": -30} for v in variants}
    template = (folder/"MIX-balance_before.wav").read_bytes()
    report["audio"] = {key: f"MIX-{key}.wav" for key in balance_audio_keys(6, report)}
    package["files"] = {}
    for filename in report["audio"].values():
        (folder/filename).write_bytes(template)
        package["files"][filename] = hashlib.sha256(template).hexdigest()
    return report


def test_foundation_compares_native_levels_without_an_audio_model_or_fake_ratings(staged):
    review, identifier, folder, package = staged
    foundation_package(folder, package)
    (folder/"study.json").write_text(json.dumps(package))
    result = review.import_balance({"study_id": identifier})
    report = result["jobs"][0]["reports"]["MIX"]
    assert report["foundation"]["audio_models_run"] == 0
    assert report["preference_established"] is False
    assert "evaluator" not in report
    assert not review.snapshot(balance_schema=5)["jobs"]
    for variant in ("before", "bass_0", "bass_p3", "bass_p6"):
        assert review.audio(identifier, "MIX", f"balance_{variant}_matched").startswith(b"RIFF")
    with pytest.raises(ValueError):
        review.audio(identifier, "MIX", "balance_bass_p9_matched")


@pytest.mark.parametrize("problem", ["duplicate", "path", "too_many", "missing_before", "local_preview",
    "missing_decision", "wrong_application", "model_scores", "model_run", "no_saved_controls", "blank_explanation"])
def test_foundation_rejects_unsafe_variant_names_and_misrepresented_decisions(staged, problem):
    review, identifier, folder, package = staged
    report = foundation_package(folder, package)
    if problem == "duplicate":
        report["variants"][1]["id"] = "before"
    elif problem == "path":
        report["variants"][1]["id"] = "../../outside"
    elif problem == "too_many":
        report["variants"] *= 2
    elif problem == "missing_before":
        report["variants"][0]["id"] = "first"
    elif problem == "local_preview":
        report["variants"][1]["origin"] = "local_eq_preview"
    elif problem == "missing_decision":
        report.pop("foundation")
    elif problem == "wrong_application":
        report["foundation"]["applied_variant"] = "bass_0"
    elif problem == "model_scores":
        report["evaluator"] = {"PQ": 9}
    elif problem == "model_run":
        report["foundation"]["audio_models_run"] = 1
    elif problem == "no_saved_controls":
        report["foundation"]["original_controls_saved"] = False
    else:
        report["foundation"]["explanation"] = " "
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_balance({"study_id": identifier})
    assert not review.snapshot()["jobs"]
