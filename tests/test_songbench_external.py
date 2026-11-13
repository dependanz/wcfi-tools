"""Meaningful ranking, pairing and conversion checks without loading a neural model."""
import hashlib
import math

import pytest

from wcfi_tools.soundcheck.songbench_external import (
    AXES,
    POLICY,
    _med_ratings,
    _model_wave,
    cluster_interval,
    credit,
    evaluate_rows,
    paired_interval,
    paired_ratings,
)


def test_ratings_not_universal_or_missing_filled_with_zero():
    ratings = _med_ratings({"evaluations": [{"participant_id": "a", "rating": .4},
        {"participant_id": "a", "rating": .6}, {"participant_id": "b", "rating": None}]})
    assert ratings == {"a": .5}
    with pytest.raises(ValueError):
        _med_ratings({"evaluations": [{"participant_id": "x", "rating": float("nan")}]})


def test_paired_humans_and_either_mix_author_excluded():
    a = {"name": "McG-A", "ratings": {"McG-A_2014": .9, "McG-B_2014": .8,
        "listener": .6, "unpaired": .1}}
    b = {"name": "McG-B", "ratings": {"McG-A_2014": .1, "McG-B_2014": .2, "listener": .3}}
    assert paired_ratings(a, b, "MED").tolist() == [.3]
    assert len(paired_ratings(a, b, "ODAQ")) == 3


def test_unknown_year_suffix_is_an_author_id_not_an_independent_listener():
    a = {"name": "PXL-S1", "ratings": {"PXL-S1_unknown": 1., "PXL-S3_unknown": .1,
                                       "PXL-S10_unknown": .5, "listener": .6}}
    b = {"name": "PXL-S3", "ratings": {"PXL-S1_unknown": .1, "PXL-S3_unknown": 1.,
                                       "PXL-S10_unknown": .2, "listener": .3}}
    assert paired_ratings(a, b, "MED").tolist() == [.3, .3]


def test_ambiguous_human_pair_is_not_a_forced_winner():
    import numpy as np

    interval = paired_interval(np.array([-.1, .1]*8))
    assert interval[0] < 0 < interval[1]
    assert paired_interval(np.ones(7)) is None
    assert paired_interval(np.ones(8)) == [1., 1.]
    assert credit(0, 1) == .5
    assert credit(-.1, 1) == 0


def test_macro_aggregation_not_inflated_by_number_of_versions():
    rows, scores = [], {}
    for dataset in ("MED", "ODAQ"):
        for group, n, forward in (("many_versions", 8, True), ("few_versions", 2, False)):
            for i in range(n):
                identifier = f"{dataset}-{group}-{i}"
                rows.append({"dataset": dataset, "group": group, "id": identifier,
                    "name": f"mix-{i}", "ratings": {f"listener-{r}": i/n for r in range(12)},
                    "source_rms_dbfs": -30+i})
                scores[identifier] = {axis: float(i if forward else -i) for axis in AXES}
    result = evaluate_rows(rows, scores)
    for value in result["datasets"].values():
        assert value["macro_accuracy"] == .5
        assert value["decisive_pairs"] == 29
        assert not value["screening_passed"]


def test_one_content_cannot_pass_gate_with_many_correlated_pairs():
    assert POLICY["screening_gate"]["minimum_groups_per_dataset"] == 6
    assert cluster_interval([.5]*6) == [.5, .5]


def test_gate_requires_external_ranking_on_both_datasets():
    rows, scores = [], {}
    for dataset in ("MED", "ODAQ"):
        for group in range(6):
            for version in range(3):
                identifier = f"{dataset}-{group}-{version}"
                rows.append({"dataset": dataset, "group": str(group), "id": identifier,
                    "name": f"mix-{version}", "ratings": {f"listener-{r}": version/3 for r in range(12)},
                    "source_rms_dbfs": -30+version})
                scores[identifier] = {axis: float(version+1) for axis in AXES}
    result = evaluate_rows(rows, scores)
    assert all(d["screening_passed"] for d in result["datasets"].values())
    for identifier, score in scores.items():
        if identifier.startswith("MED"):
            score["Mixing"] = 5-score["Mixing"]
    result = evaluate_rows(rows, scores)
    assert not result["datasets"]["MED"]["screening_passed"]
    assert result["datasets"]["ODAQ"]["screening_passed"]


def test_mono_resampling_does_not_normalize_loudness(tmp_path):
    import numpy as np
    import soundfile as sf

    rate = 48000
    wave = .05*np.sin(np.arange(rate)*2*np.pi*440/rate)
    stereo = np.column_stack((wave, wave*.5)).astype("float32")
    path = tmp_path/"input.wav"
    sf.write(path, stereo, rate, subtype="FLOAT")
    converted, measures = _model_wave(path)
    assert converted.shape == (24000,)
    assert measures["source_channels"] == 2
    assert math.isclose(float(np.sqrt(np.mean(converted**2))), .0375/2**.5, rel_tol=.001)
    assert measures["source_peak"] < .051
    assert len(hashlib.sha256(converted.tobytes()).hexdigest()) == 64
