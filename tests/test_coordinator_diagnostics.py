"""Instrumentation must not change a single output sample or legacy event."""
import json
import subprocess
import sys
from copy import deepcopy

import numpy as np
import pytest

from wcfi_tools.audio.coordinator_diagnostics import (
    DiagnosticMixCoordinator,
    annotate_events,
    explain_bin,
    read_json,
    summarize_events,
)
from wcfi_tools.audio.mix_coordinator import (
    SUPPORT_TRACKS,
    TRACKS,
    CoordinatorSettings,
    MixCoordinator,
)


def settings(**kwargs):
    values = {"baseline_gains_db": {t: 0. for t in TRACKS},
              "reference_support_to_lead_db": {t: -10. for t in SUPPORT_TRACKS},
              "activity_thresholds_dbfs": {t: -60. for t in TRACKS}}
    values.update(kwargs)
    return CoordinatorSettings(**values)


def pcm(levels, rate=8000, channels=1):
    return np.broadcast_to(10 ** (np.asarray(levels)[None, :, None] / 20),
                           (rate // 10, len(TRACKS), channels)).copy()


@pytest.mark.parametrize("rate", [8000, 48000])
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_exact_sample_gain_and_legacy_event_parity(rate, channels, dtype):
    base = MixCoordinator(settings(), rate)
    instrumented = DiagnosticMixCoordinator(settings(), rate)
    for k in range(140):
        levels = [-30.] + [-40.] * 6
        levels[5] += 10. if 20 <= k < 75 else 0.
        if k in (8, 9, 10, 95, 96):
            levels[0] = -90.
        x = pcm(levels, rate, channels).astype(dtype)
        if k == 80:
            x[13, 2, 0] = np.nan
        if k == 100:
            for obj in (base, instrumented):
                obj.set_manual("AC GUITAR", -1.)
        if k == 115:
            for obj in (base, instrumented):
                obj.set_manual("AC GUITAR", None)
        if k == 120:
            for obj in (base, instrumented):
                obj.notify_gap()
        for piece in (x[:137], x[137:]):
            np.testing.assert_array_equal(base.process(piece), instrumented.process(piece))
        assert base.current_correction_db == instrumented.current_correction_db
        assert base.target_correction_db == instrumented.target_correction_db
    stripped = [{key: value for key, value in event.items()
                 if key not in ("diagnostics", "diagnostics_version")}
                for event in instrumented.events]
    assert stripped == base.events


def test_negative_drift_is_not_reported_as_balanced():
    obj = DiagnosticMixCoordinator(settings(), 8000)
    for _ in range(40):
        obj.process(pcm([-30.] + [-45.] * 6))
    row = obj.events[-1]
    assert row["reasons"]["KEYS"] == "hold_deadband"
    d = row["diagnostics"]["KEYS"]
    assert d["reason"] == "hold_no_positive_drift"
    assert d["evidence_sufficient_now"]
    assert d["rolling_median_error_db"] == pytest.approx(-5.)
    assert d["current_correction_db"] == 0
    assert not d["automatic_boost_above_baseline_allowed"]


@pytest.mark.parametrize("changed, expected", [
    ((0,), ["RED"]), ((6,), ["KIT"]), ((0, 6), ["RED", "KIT"]),
])
def test_accent_attribution(changed, expected):
    obj = DiagnosticMixCoordinator(settings(), 8000)
    levels = np.array([-30.] + [-40.] * 6)
    obj.process(pcm(levels))
    levels[list(changed)] += 8.
    obj.process(pcm(levels))
    d = obj.events[-1]["diagnostics"]["KIT"]
    assert d["reason"] == "hold_accent"
    assert d["accent_sources"] == expected
    assert not d["evidence_sufficient_now"]


def test_annotation_matches_live_instrumentation_without_mutation():
    obj = DiagnosticMixCoordinator(settings(), 8000)
    for k in range(50):
        obj.process(pcm([-30.] + [-40. + (8. if k > 30 else 0.)] * 6))
    saved = [{key: value for key, value in e.items()
              if key not in ("diagnostics", "diagnostics_version")} for e in obj.events]
    snapshot = deepcopy(saved)
    assert list(annotate_events(saved, settings())) == obj.events
    assert snapshot == saved
    summary = summarize_events(saved, settings())
    assert summary["tracks"]["KIT"]["bins"] == 50
    json.dumps(summary, allow_nan=False)


def test_missing_and_invalid_observations_break_accent_attribution():
    obj = MixCoordinator(settings(), 8000)
    for _ in range(5):
        obj.process(pcm([-30.] + [-40.] * 6))
    saved = [obj.events[0], obj.events[3], obj.events[4]]
    annotated = list(annotate_events(saved, settings()))
    assert annotated[1]["diagnostics"]["KIT"]["accent_sources"] is None
    invalid = deepcopy(obj.events[0])
    invalid["invalid"] = True
    invalid["reasons"] = dict.fromkeys(TRACKS, "hold_invalid")
    invalid["rolling_median_error_db"] = dict.fromkeys(SUPPORT_TRACKS, None)
    annotated = list(annotate_events([invalid, obj.events[1]], settings()))
    assert annotated[1]["diagnostics"]["KIT"]["accent_sources"] is None


def test_recovery_hysteresis_explained():
    obj = MixCoordinator(settings(), 8000)
    for _ in range(40):
        obj.process(pcm([-30.] + [-40.] * 6))
    event = deepcopy(obj.events[-1])
    event["current_correction_db"]["KIT"] = -.5
    event["target_correction_db"]["KIT"] = -.5
    event["rolling_median_error_db"]["KIT"] = 1.7
    assert explain_bin(event, settings())["KIT"]["reason"] == "hold_recovery_hysteresis"


def test_cli_refuses_overwrite_and_reads_only_json(tmp_path):
    obj = MixCoordinator(settings(), 8000)
    obj.process(pcm([-30.] + [-40.] * 6))
    decisions, report, output = (tmp_path / n for n in
                                 ("decisions.json", "report.json", "out.json"))
    decisions.write_text(json.dumps(obj.events))
    report.write_text(json.dumps({"protocol": {"coordinator_settings": settings().as_dict()}}))
    command = [sys.executable, "-m", "wcfi_tools.audio.coordinator_diagnostics", str(decisions),
               "--report", str(report), "--output", str(output)]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    before = output.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode != 0
    assert output.read_bytes() == before


def test_json_rejects_nonfinite_and_oversized(tmp_path):
    source = tmp_path / "invalid.json"
    source.write_text('{"x": NaN}')
    with pytest.raises(ValueError):
        read_json(source)
    with pytest.raises(ValueError):
        read_json(source, max_bytes=1)
