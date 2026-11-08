"""Offline KIT gain envelopes: causal, bounded and isolated from host/audio writes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_gain_trial import (
    VirtualKitGain, run_study, simulate, summarize,
)
from wcfi_tools.audio.kit_observation_trial import PairReference
from wcfi_tools.audio.mix_coordinator import CoordinatorSettings, MixCoordinator, SUPPORT_TRACKS, TRACKS


def settings():
    return CoordinatorSettings(
        baseline_gains_db={t: 0.0 for t in TRACKS},
        reference_support_to_lead_db={t: -10.0 for t in SUPPORT_TRACKS},
        activity_thresholds_dbfs={t: -55.0 for t in TRACKS},
    )


def ref(bins=5):
    return PairReference(bins, (0.0, 0.0), (-55.0, -55.0), -10.0, -55.0)


def row(index, reason="eligible", median=8.0, error=8.0, ready=True):
    return {"bin_index": index, "time_seconds": (index+1)/10,
            "relative_error_db": error, "median_error_db": median,
            "evidence_bins": 35, "evidence_sufficient": ready,
            "cut_evidence_ready": ready and error > 2 and median > 2,
            "reason": reason}


def test_first_observation_only_changes_next_bin_gain():
    ctl = VirtualKitGain(settings())
    first = ctl.feed(row(0))
    assert first["gain_db_at_start"] == 0
    assert first["gain_db_at_end"] == 0
    assert first["next_bin_target_db"] == -3
    assert first["action_after_observation"] == "attenuate"
    second = ctl.feed(row(1))
    assert second["gain_db_at_start"] == 0
    assert second["gain_db_at_end"] == pytest.approx(-.05)
    assert second["next_bin_target_db"] == -3


def test_movement_bounded_and_abstentions_freeze_instead_of_forcing_recovery():
    ctl = VirtualKitGain(settings())
    trace = [ctl.feed(row(i)) for i in range(100)]
    assert min(r["gain_db_at_end"] for r in trace) == pytest.approx(-3)
    assert max(abs(r["gain_delta_db_in_bin"]) for r in trace) <= .05 + 1e-9
    # The previous valid decision can move this current bin, but an invalid
    # observation freezes every following bin immediately after it is read.
    gap = ctl.feed(row(100, reason="gap", median=None, error=None, ready=False))
    held = ctl.feed(row(101, reason="hold_accent", median=None, error=None, ready=False))
    assert gap["gain_db_at_end"] == pytest.approx(-3)
    assert held["gain_db_at_end"] == pytest.approx(-3)
    assert held["next_bin_target_db"] == pytest.approx(-3)
    resume = ctl.feed(row(102, median=-2, error=-2))
    assert resume["action_after_observation"] == "recover"
    assert resume["gain_db_at_end"] == pytest.approx(-3)
    later = ctl.feed(row(103, median=-2, error=-2))
    assert later["gain_db_at_end"] == pytest.approx(-2.95)
    for i in range(104, 180):
        later = ctl.feed(row(i, median=-2, error=-2))
    assert later["gain_db_at_end"] == pytest.approx(0.)


def test_no_fabricated_eligibility_and_order_enforced():
    ctl = VirtualKitGain(settings())
    first = ctl.feed(row(0, reason="collect_evidence", median=-9, error=9, ready=False))
    assert first["next_bin_target_db"] == 0
    with pytest.raises(ValueError, match="consecutive"):
        ctl.feed(row(2))


def test_frozen_fault_scenarios_virtual_only():
    # 120 seconds at one observation per 100 ms: a steady RED and quieter KIT.
    powers = np.array([[.01**2, .003**2]] * 1200)
    base = ref()
    s = settings()
    reference = summarize(simulate(powers, base, s, "original"), s)
    assert reference["nonzero_gain_bins"] == 0
    sustained = summarize(simulate(powers, base, s, "kit_step_plus10"), s)
    assert sustained["first_cut_command_seconds"] is not None
    assert 20 <= sustained["first_cut_command_seconds"] <= 50
    assert sustained["minimum_applied_gain_db"] == pytest.approx(-3)
    assert sustained["first_recovery_command_seconds"] is not None
    assert sustained["gain_bounds_ok"] and sustained["slew_bound_ok"]
    assert sustained["final_gain_db"] == 0
    for scenario in ("kit_single_hit_plus10", "lead_pause", "gap"):
        result = summarize(simulate(powers, base, s, scenario), s)
        assert result["nonzero_gain_bins"] == 0, scenario


def test_scenario_cannot_peek_at_future_bins():
    powers = np.array([[.01**2, .003**2]] * 1200)
    original = list(simulate(powers, ref(), settings(), "original"))
    fault = list(simulate(powers, ref(), settings(), "kit_step_plus10"))
    assert original[:200] == fault[:200]
    # The first fault observation (bin index 200) cannot change that bin's gain.
    assert fault[200]["gain_db_at_end"] == original[200]["gain_db_at_end"]


def fake_saved_trial(tmp_path):
    rate, start, cal_end, end = 48000, 48000, 11 * 48000, 71 * 48000
    s = settings()
    report = {"protocol": {"sample_rate": rate,
                           "calibration_source_frames": [start, cal_end],
                           "evaluation_source_frames": [cal_end, end],
                           "coordinator_settings": s.as_dict()}, "source_provenance": {}}
    for track, amp in (("RED", .01), ("KIT", .003)):
        path = tmp_path / (track + ".wav")
        audio = np.full((end + 1, 2), amp, np.float32)
        sf.write(path, audio, rate, subtype="FLOAT")
        report["source_provenance"][track] = {
            "path": str(path), "channels": 2, "frames": end + 1,
            "decoded_window_sha256": hashlib.sha256(audio[start:end].astype("<f4").tobytes()).hexdigest()}
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    ctl = MixCoordinator(s, rate)
    levels = np.array([.01] + [.003] * 6, dtype=np.float32)
    audio = np.broadcast_to(levels[None, :, None], (rate // 10, 7, 2)).copy()
    for _ in range(600):
        ctl.process(audio)
    decisions_file = tmp_path / "decisions.json"
    decisions_file.write_text(json.dumps(ctl.events), encoding="utf-8")
    return report_file, decisions_file


def test_cli_end_to_end_verifies_hashes_and_never_overwrites(tmp_path):
    report, decisions = fake_saved_trial(tmp_path)
    output = tmp_path / "run"
    command = [sys.executable, "-m", "wcfi_tools.audio.kit_gain_trial", "--report", str(report),
               "--decisions", str(decisions), "--output", str(output)]
    finished = subprocess.run(command, capture_output=True, text=True)
    assert finished.returncode == 0, finished.stderr
    result = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert result["protocol"]["saved_100ms_trace_replay_verified_bins"] == 600
    assert result["protocol"]["source_verification"]["RED"]["verified_against_saved_trial"]
    assert result["audio_rendered"] is False
    assert result["protocol"]["host_writes"] == 0
    assert len(list(output.glob("*-gain.jsonl"))) == 10
    assert result["engineering_controls_pass"]
    assert result["comparisons"]["500ms"]["kit_step_plus10"]["minimum_applied_gain_db"] < 0
    marker = output / "leave-alone.txt"
    marker.write_text("saved", encoding="utf-8")
    replay = subprocess.run(command, capture_output=True, text=True)
    assert replay.returncode != 0
    assert marker.read_text(encoding="utf-8") == "saved"


def test_preflight_rejects_hash_and_saved_baseline_mismatch_before_output(tmp_path):
    report, decisions = fake_saved_trial(tmp_path)
    raw = json.loads(report.read_text(encoding="utf-8"))
    raw["source_provenance"]["KIT"]["decoded_window_sha256"] = "0" * 64
    report.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        run_study(report, decisions, tmp_path / "reject-hash")
    assert not (tmp_path / "reject-hash").exists()
    raw["source_provenance"]["KIT"]["decoded_window_sha256"] = hashlib.sha256(
        sf.read(raw["source_provenance"]["KIT"]["path"], dtype="float32", always_2d=True)[0][48000:71*48000].astype("<f4").tobytes()).hexdigest()
    report.write_text(json.dumps(raw), encoding="utf-8")
    old = json.loads(decisions.read_text(encoding="utf-8"))
    old[80]["evidence_bins"]["KIT"] = 500
    decisions.write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(ValueError, match="baseline mismatch"):
        run_study(report, decisions, tmp_path / "reject-baseline")
    assert not (tmp_path / "reject-baseline").exists()
