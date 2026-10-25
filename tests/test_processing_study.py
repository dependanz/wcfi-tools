"""Local-only comparison provenance, preserved band remainder and review metadata."""
import copy
import hashlib
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.processing_study import SCOPES, processing_audio_keys
from wcfi_tools.audio.rehearsal_suite import PASSAGES
from wcfi_tools.audio.review_service import RehearsalReview


@pytest.fixture
def staged_processing(tmp_path):
    review = RehearsalReview(tmp_path/"state", tmp_path/"sources")
    identifier = "e"*32;folder = review.root/"suite-staging"/identifier
    folder.mkdir(parents=True)
    template = folder/"template.wav"
    sf.write(template, np.full((24000, 2), .01, np.float32), 48000, subtype="FLOAT")
    payload = template.read_bytes();template.unlink()
    files, audio_map = {}, {}
    for key in processing_audio_keys():
        name = f"MIX-{key}.wav";(folder/name).write_bytes(payload)
        files[name] = hashlib.sha256(payload).hexdigest();audio_map[key] = name
    passages = []
    for p in PASSAGES:
        passages.append({"id": p, "duration_seconds": .5, "operator_approved": False,
            "baseline_sum_relative_error": 0,
            "remainder_relative_errors": {v: 0 for v in SCOPES["mix"][1:]},
            "source_loudness_compensation_db": {role: {v: 0 for v in SCOPES[role]} for role in ("red", "kit")},
            "tracks": {role: {"recipe": {"role": role, "application": "local_listening_preview_only",
                "native_plugin_equivalence": False, "deess": {"enabled": False}}} for role in ("red", "kit")},
            "listening": {scope: {"target_lufs": -24, "matched": {v: {"integrated_lufs": -24,
                "true_peak_dbtp": -30} for v in variants}} for scope, variants in SCOPES.items()}})
    report = {"kind": "wcfi_processing_study", "live_writes": 0, "preference_established": False,
              "passages": passages, "audio": audio_map}
    package = {"kind": "wcfi_processing_study_package", "schema_version": 1,
               "reports": {"MIX": report}, "files": files}
    (folder/"study.json").write_text(json.dumps(package))
    yield review, identifier, folder, package
    review.close()


def test_processing_import_preserves_local_identity_and_hides_from_older_suite_clients(staged_processing):
    review, identifier, _, _ = staged_processing
    result = review.import_suite({"study_id": identifier, "processing": True})
    assert review.import_suite({"study_id": identifier, "processing": True}) == result
    assert not review.snapshot(include_processing=False)["jobs"]
    assert len(processing_audio_keys()) == 108
    assert review.audio(identifier, "MIX", "process_oct11_red_deess_matched").startswith(b"RIFF")
    assert review.audio(identifier, "MIX", "process_oct18_mix_both_chain").startswith(b"RIFF")
    immutable = copy.deepcopy(review.jobs[identifier]["reports"])
    review.assess_processing({"job_id": identifier, "passage_id": "oct11", "role": "red", "variant": "deess",
        "context": "mix", "verdict": "worse", "reason": "Consonants sound dull."})
    review.assess_processing({"job_id": identifier, "passage_id": "oct11", "role": "red", "variant": "eq",
        "context": "solo", "verdict": "same", "reason": "No useful difference."})
    review.assess_suite({"job_id": identifier, "passage_id": "oct11", "include": True,
        "verdict": "unsure", "reason": "Confirmed performance only."})
    assert review.jobs[identifier]["reports"] == immutable
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        reviews = restored.snapshot()["jobs"][0]["reports"]["MIX"]["processing_reviews"]
        assert reviews["oct11:red:deess:mix"]["verdict"] == "worse"
        assert reviews["oct11:red:eq:solo"]["verdict"] == "same"
        for bad in ({"role": "kit", "variant": "deess"}, {"role": "both", "context": "solo"}):
            with pytest.raises(ValueError):
                restored.assess_processing({"job_id": identifier, "passage_id": "oct11", "role": "both",
                    "variant": "chain", "context": "mix", "verdict": "better", "reason": "Invalid case", **bad})
        with pytest.raises(ValueError):
            restored.apply({"job_id": identifier, "track": "RED", "kind": "software_gain"})
    finally:
        restored.close()


@pytest.mark.parametrize("problem", ["native", "role", "drum_deess", "remainder", "missing_evidence", "matching", "compensation", "checksum"])
def test_mislabelled_or_unverified_processing_packages_are_rejected(staged_processing, problem):
    review, identifier, folder, package = staged_processing
    passage = package["reports"]["MIX"]["passages"][0]
    if problem == "native":
        passage["tracks"]["red"]["recipe"]["native_plugin_equivalence"] = True
    elif problem == "role":
        passage["tracks"]["kit"]["recipe"]["role"] = "red"
    elif problem == "drum_deess":
        passage["tracks"]["kit"]["recipe"]["deess"]["enabled"] = True
    elif problem == "remainder":
        passage["remainder_relative_errors"]["red_eq"] = .1
    elif problem == "missing_evidence":
        passage["remainder_relative_errors"] = {}
    elif problem == "matching":
        passage["listening"]["red"]["matched"]["eq"]["integrated_lufs"] = -15
    elif problem == "compensation":
        passage["source_loudness_compensation_db"]["red"]["eq"] = 50
    else:
        package["files"][next(iter(package["files"]))] = "0"*64
    (folder/"study.json").write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_suite({"study_id": identifier, "processing": True})
    assert not review.jobs
