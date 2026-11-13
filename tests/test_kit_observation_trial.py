import hashlib
import json
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_observation_trial import (
    PairReference,
    PowerWindow,
    calibrate_500ms,
    evaluate,
    load_pair,
)
from wcfi_tools.audio.mix_coordinator import (
    SUPPORT_TRACKS,
    TRACKS,
    CoordinatorSettings,
    MixCoordinator,
)


def settings():
    return CoordinatorSettings(baseline_gains_db={t: 0. for t in TRACKS},
                               reference_support_to_lead_db={t: -10. for t in SUPPORT_TRACKS},
                               activity_thresholds_dbfs={t: -55. for t in TRACKS})


def reference():
    return PairReference(1, (0., 0.), (-55., -55.), -10., -55.)


def test_power_average_not_db_average_and_no_warmup_padding():
    window = PowerWindow(5)
    for _ in range(4):
        assert window.push([.001, .01]) is None
    np.testing.assert_allclose(window.push([.001, .1]), [.001, .028])
    window.clear()
    assert window.push([.001, .01]) is None


@pytest.mark.parametrize("bad", [[-1, 1], [np.nan, 1], [np.inf, 1], [1, 2, 3]])
def test_invalid_power_rejected_and_window_cleared(bad):
    w = PowerWindow(5)
    for _ in range(4):
        w.push([.001, .01])
    with pytest.raises(ValueError):
        w.push(bad)
    assert w.push([.001, .01]) is None


def test_causal_prefix_invariance():
    powers = np.tile([.001, .0001], (300, 1))
    c = calibrate_500ms(powers, reference())
    first = list(evaluate(powers, c, settings(), "original"))
    changed = powers.copy()
    changed[100:, 1] *= 100
    second = list(evaluate(changed, c, settings(), "original"))
    assert first[:100] == second[:100]


def test_isolated_hit_vs_sustained_step_and_pause_and_gap():
    powers = np.tile([.001, .0001], (800, 1))
    c = calibrate_500ms(powers[:300], reference())
    null = list(evaluate(powers, c, settings(), "original"))
    hit = list(evaluate(powers, c, settings(), "kit_single_hit_plus10"))
    step = list(evaluate(powers, c, settings(), "kit_step_plus10"))
    pause = list(evaluate(powers, c, settings(), "lead_pause"))
    gap = list(evaluate(powers, c, settings(), "gap"))
    assert not any(r["cut_evidence_ready"] for r in null)
    assert not any(r["cut_evidence_ready"] for r in hit)
    assert any(r["cut_evidence_ready"] for r in step[200:500])
    assert all(r["reason"] == "hold_lead_inactive" for r in pause[200:250])
    assert gap[200]["reason"] == "gap"
    assert gap[201]["evidence_bins"] == 0
    assert all(not r["cut_evidence_ready"] for r in gap)
    assert not any(r["gain_applied"] for r in step)


def test_calibration_rejects_silence():
    with pytest.raises(ValueError, match="jointly supported"):
        calibrate_500ms(np.zeros((300, 2)), reference())


def test_measurement_only_baseline_matches_core_on_synthetic_pcm():
    rng = np.random.default_rng(12)
    obj = MixCoordinator(settings(), 8000)
    powers = []
    for _ in range(200):
        levels = np.array([-30., -40., -40., -40., -40., -40., -40.])
        levels[[0, 6]] += rng.normal(0, 9, size=2)
        x = np.broadcast_to(10 ** (levels[None, :, None] / 20), (800, 7, 1)).copy()
        powers.append(np.mean(x[:, [0, 6], 0] ** 2, axis=0))
        obj.process(x)
    from wcfi_tools.audio.coordinator_diagnostics import BALANCE_REASONS
    for row, saved in zip(evaluate(powers, reference(), settings(), "original"), obj.events):
        reason = saved["reasons"]["KIT"]
        assert row["reason"] == ("eligible" if reason in BALANCE_REASONS else reason)
        assert row["evidence_bins"] == saved["evidence_bins"]["KIT"]
        assert row["relative_error_db"] == pytest.approx(saved["raw_relative_error_db"]["KIT"])


def fixture_report(tmp_path):
    rate, start, cal_end, end = 48000, 48000, 11 * 48000, 23 * 48000
    report = {"protocol": {"sample_rate": rate,
                           "calibration_source_frames": [start, cal_end],
                           "evaluation_source_frames": [cal_end, end],
                           "coordinator_settings": settings().as_dict()}, "source_provenance": {}}
    for t, amplitude in (("RED", .01), ("KIT", .003)):
        path = tmp_path / (t + ".wav")
        x = np.full((end + 1, 2), amplitude, np.float32)
        sf.write(path, x, rate, subtype="FLOAT")
        report["source_provenance"][t] = {
            "path": str(path), "channels": 2, "frames": end + 1,
            "decoded_window_sha256": hashlib.sha256(
                x[start:end].astype("<f4").tobytes()).hexdigest()}
    return report


def test_window_reads_verify_exact_saved_pcm(tmp_path):
    report = fixture_report(tmp_path)
    cal, evaluation, provenance = load_pair(report)
    assert cal.shape == (100, 2) and evaluation.shape == (120, 2)
    assert all(v["verified_against_saved_trial"] for v in provenance.values())
    report["source_provenance"]["KIT"]["decoded_window_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        load_pair(report)


def test_cli_preserves_existing_directory(tmp_path):
    report = fixture_report(tmp_path)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    old = tmp_path / "old"
    old.mkdir()
    marker = old / "keep.txt"
    marker.write_text("keep")
    outcome = subprocess.run([sys.executable, "-m", "wcfi_tools.audio.kit_observation_trial",
                              "--report", str(path), "--decisions", "unused.json",
                              "--output", str(old)], capture_output=True, text=True, check=False)
    assert outcome.returncode != 0
    assert marker.read_text() == "keep"
    assert not (old / "protocol.json").exists()


def test_cli_end_to_end_and_baseline_mismatch_refusal(tmp_path):
    report = fixture_report(tmp_path)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    obj = MixCoordinator(settings(), 48000)
    values = np.array([.01] + [.003] * 6, dtype=np.float32)
    audio = np.broadcast_to(values[None, :, None], (4800, 7, 2)).copy()
    for _ in range(120):
        obj.process(audio)
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(obj.events))
    output = tmp_path / "study"
    command = [sys.executable, "-m", "wcfi_tools.audio.kit_observation_trial",
               "--report", str(path), "--decisions", str(decisions), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["baseline_measurement_replay_matches_saved_trace"] is True
    assert summary["gain_policy_changed"] is False
    assert len(list(output.glob("*.jsonl"))) == 10
    counts = summary["comparisons"]["500ms"]["scenarios"]["original"]["reason_counts"]
    assert sum(counts.values()) == 120
    obj.events[80]["evidence_bins"]["KIT"] = 999
    decisions.write_text(json.dumps(obj.events))
    command[-1] = str(tmp_path / "mismatched")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Baseline measurement replay mismatch" in result.stderr
    assert not (tmp_path / "mismatched" / "summary.json").exists()
