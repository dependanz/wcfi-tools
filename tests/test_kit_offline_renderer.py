"""Offline KIT audition rendering: fail closed, preserve originals, no DAW I/O."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio import kit_offline_renderer as renderer


def envelope(i, start=0.0, end=0.0, target=0.0):
    return {"bin_index": i, "time_end_seconds": (i+1)/10,
            "gain_db_at_start": start, "gain_db_at_end": end,
            "next_bin_target_db": target, "gain_delta_db_in_bin": end-start,
            "output_is_virtual_only": True}


def trace(path, rows):
    path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")


def test_gain_trace_blocks_future_observation_and_missing_bins(tmp_path):
    path = tmp_path / "gain.jsonl"
    trace(path, [envelope(0, target=-3), envelope(1, end=-.05, target=-.05),
                 envelope(2, start=-.05, end=-.05, target=-.05)])
    rows = renderer.validate_trace(path, expected_bins=3, maximum_cut_db=3,
                                    slew_db_per_second=.5)
    assert [r["gain_db_at_end"] for r in rows] == [0, -.05, -.05]
    assert renderer.gain_for_audio(rows[1], frames=10)[-1] == pytest.approx(10**(-.05/20))
    assert renderer.gain_for_audio(rows[1], frames=10)[0] > renderer.gain_for_audio(rows[1], frames=10)[-1]
    trace(path, [envelope(0, target=-3), envelope(1, end=-.05, target=-.05),
                 envelope(3, start=-.05, end=-.05, target=-.05)])
    with pytest.raises(ValueError, match="reordered"):
        renderer.validate_trace(path, expected_bins=3, maximum_cut_db=3,
                                slew_db_per_second=.5)
    trace(path, [envelope(0, end=-.05, target=0)])
    with pytest.raises(ValueError, match="causality"):
        renderer.validate_trace(path, expected_bins=1, maximum_cut_db=3,
                                slew_db_per_second=.5)
    trace(path, [envelope(0, target=-3), envelope(1, end=-.3)])
    with pytest.raises(ValueError, match="slew"):
        renderer.validate_trace(path, expected_bins=2, maximum_cut_db=3,
                                slew_db_per_second=.5)


def test_zero_legacy_and_overcuts_rejected(tmp_path):
    path = tmp_path / "gain.jsonl"
    trace(path, [envelope(0, target=-3)])
    with pytest.raises(ValueError, match="must be zero"):
        renderer.validate_trace(path, expected_bins=1, maximum_cut_db=3,
                                slew_db_per_second=.5, must_be_zero=True)
    trace(path, [envelope(0, target=-4)])
    with pytest.raises(ValueError, match="escaped"):
        renderer.validate_trace(path, expected_bins=1, maximum_cut_db=3,
                                slew_db_per_second=.5)
    trace(path, [envelope(0, end=0, target=0)])
    with pytest.raises(ValueError, match="exactly"):
        renderer.validate_trace(path, expected_bins=2, maximum_cut_db=3,
                                slew_db_per_second=.5)


def test_preflight_refuses_output_when_provenance_differs(tmp_path, monkeypatch):
    # Does not need actual WAVs: the frozen report hash gate runs first.
    report = tmp_path / "report.json"
    audit = tmp_path / "audit.json"
    decisions = tmp_path / "decisions.json"
    run = tmp_path / "run"
    run.mkdir()
    for p in (report, audit, decisions):
        p.write_text("{}", encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"protocol": {
        "source_selection": "previously_examined_development_recording",
        "report_sha256": "wrong"}}), encoding="utf-8")
    with pytest.raises((KeyError, ValueError)):
        renderer.preflight(report, audit, decisions, run, Path(__file__))


def test_complete_small_renderer_uses_only_kit_and_preserves_source_files(tmp_path, monkeypatch):
    # This tests the sample-gain renderer with a miniature 100 Hz fixture.
    # Production preflight still enforces the frozen 48 kHz / 120 s contract.
    monkeypatch.setattr(renderer, "RATE", 100)
    monkeypatch.setattr(renderer, "BIN", 10)
    sources = {}
    for j, name in enumerate(renderer.TRACKS):
        path = tmp_path / f"stem{j}.wav"
        n = np.arange(12_000)
        tone = (.01 + j * .001) * np.sin(2 * np.pi * n / (7 + j))
        sf.write(path, tone.astype(np.float32), 100, subtype="FLOAT")
        sources[name] = path
    original_stats = {name: (path.stat().st_size, path.stat().st_mtime_ns)
                      for name, path in sources.items()}
    gain_run = tmp_path / "run"
    gain_run.mkdir()
    (gain_run / "summary.json").write_text("{}", encoding="utf-8")
    report_path = tmp_path / "report.json"
    audit_path = tmp_path / "audit.json"
    decisions_path = tmp_path / "decisions.json"
    for p in (report_path, audit_path, decisions_path):
        p.write_text("{}", encoding="utf-8")
    legacy = [envelope(i) for i in range(1200)]
    candidate = []
    current = 0.0
    for i in range(1200):
        new = -0.05 if i == 220 else current
        candidate.append(envelope(i, start=current, end=new, target=new))
        current = new
    trace(gain_run / "100ms-kit_step_plus10-gain.jsonl", legacy)
    trace(gain_run / "500ms-kit_step_plus10-gain.jsonl", candidate)
    report = {"protocol": {
        "evaluation_source_frames": [0, 12_000],
        "coordinator_settings": {"baseline_gains_db": {t: 0.0 for t in renderer.TRACKS}}},
        "source_provenance": {t: {"source_full_scale_samples": 0} for t in renderer.TRACKS}}
    def fake_preflight(*args):
        return (report, {}, sources, {t: "test" for t in renderer.TRACKS},
                original_stats, legacy, candidate)
    monkeypatch.setattr(renderer, "preflight", fake_preflight)
    def fake_scan(path, ffmpeg):
        samples, rate = sf.read(path, always_2d=True, dtype="float64")
        rms = np.sqrt(np.mean(samples**2))
        return {"integrated_lufs": float(20*math.log10(max(rms, 1e-12))),
                "true_peak_dbtp": float(20*math.log10(max(np.max(np.abs(samples)), 1e-12)))}
    monkeypatch.setattr(renderer, "_scan", fake_scan)
    output = tmp_path / "out"
    result = renderer.render(report_path, audit_path, decisions_path, gain_run,
                             output, Path(__file__))
    assert (output / "summary.json").exists()
    assert result["render_checks"]["candidate_recomposition_max_abs_error"] < 1e-10
    assert result["protocol"]["host_writes"] == 0
    assert result["protocol"]["live_eligible"] is False
    assert len(result["auditions"]) == 3
    assert result["auditions"]["static"]["sha256"] == result["auditions"]["legacy-100ms"]["sha256"]
    sta = sf.read(output / "audition-static.wav", always_2d=True)[0]
    cand = sf.read(output / "audition-candidate-500ms.wav", always_2d=True)[0]
    assert len(sta) == len(cand) == 12_000
    # No candidate correction before its first causal gain movement, but a
    # genuine difference remains after the candidate is allowed to act.
    # Loudness matching uses per-variant scalar gains. Remove them before
    # testing waveform identity ahead of the first virtual gain command.
    static_gain = result["auditions"]["static"]["gain_db"]
    candidate_gain = result["auditions"]["candidate-500ms"]["gain_db"]
    assert np.allclose(sta[:2200] * 10**(-static_gain/20),
                       cand[:2200] * 10**(-candidate_gain/20), atol=1e-5)
    assert not np.array_equal(sta[4000:4500], cand[4000:4500])
    for name, path in sources.items():
        assert (path.stat().st_size, path.stat().st_mtime_ns) == original_stats[name]
    with pytest.raises(ValueError, match="exists"):
        renderer.render(report_path, audit_path, decisions_path, gain_run,
                        output, Path(__file__))


def test_preflight_checks_all_seven_source_hashes(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setattr(renderer, "RATE", 100)
    monkeypatch.setattr(renderer, "BIN", 10)
    inputs = {}
    provenance = {}
    for j, name in enumerate(renderer.TRACKS):
        path = tmp_path / f"stem-{j}.wav"
        wave = (np.sin(np.arange(15_000) / (5 + j)) * .01).astype("float32")
        sf.write(path, wave, 100, subtype="FLOAT")
        inputs[name] = path
        provenance[name] = {"path": str(path), "decoded_window_sha256":
            hashlib.sha256(wave.astype("<f4").tobytes()).hexdigest()}
    monkeypatch.setattr(renderer, "validate_sources", lambda audit, date: inputs)
    report_path = tmp_path / "report.json"
    audit = tmp_path / "audit.json"
    decisions = tmp_path / "decisions.json"
    for p in (audit, decisions):
        p.write_text("{}", encoding="utf-8")
    settings = {"enabled_support_tracks": ["EGUITAR", "AC GUITAR", "KEYS", "KIT"],
                "max_cut_db": 3., "slew_db_per_second": .5}
    trial = {"protocol": {"sample_rate": 100, "tracks": list(renderer.TRACKS),
        "excluded": ["LAPEL", "MDMIC Talkback", "BASS"],
        "source_date": "2026-10-04", "selection_is_development_data": True,
        "source_audit_sha256": renderer.hash_file(audit),
        "calibration_source_frames": [0, 3000],
        "evaluation_source_frames": [3000, 15_000],
        "coordinator_settings": settings}, "source_provenance": provenance,
        "engineering_checks_pass": True, "live_eligible": False,
        "perceptual_improvement_established": False}
    report_path.write_text(json.dumps(trial), encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    rows = [envelope(i) for i in range(1200)]
    trace(run / "100ms-kit_step_plus10-gain.jsonl", rows)
    candidate = []
    for i in range(1200):
        start = 0 if i <= 220 else (-.05 if i == 221 else -.1)
        end = 0 if i <= 219 else (-.05 if i == 220 else -.1)
        target = -.1 if i >= 219 else 0
        row = envelope(i, start=start, end=end, target=target)
        row["action_after_observation"] = "attenuate" if i == 219 else "hold"
        candidate.append(row)
    trace(run / "500ms-kit_step_plus10-gain.jsonl", candidate)
    meta = {"protocol": {"source_selection": "previously_examined_development_recording",
          "sample_rate_hz": 100, "observation_bin_ms": 100,
          "candidate_measurement_window_ms": 500,
          "fault_interval_seconds": [20, 50],
          "report_sha256": renderer.hash_file(report_path),
          "decisions_sha256": renderer.hash_file(decisions),
          "saved_100ms_trace_replay_verified_bins": 1200,
          "settings": settings, "source_verification": {
              name: {"verified_against_saved_trial": True,
                     "decoded_window_sha256": provenance[name]["decoded_window_sha256"]}
              for name in ("RED", "KIT")}},
          "engineering_controls_pass": True,
          "engineering_controls": {name: True for name in (
              "100ms_replay_zero_gain", "500ms_original_no_gain",
              "500ms_sustained_fault_attenuated", "500ms_isolated_hit_no_gain",
              "500ms_red_pause_no_gain", "500ms_gap_no_gain")},
          "audio_rendered": False, "gain_policy_applied_to_host": False,
          "comparisons": {"500ms": {"kit_step_plus10": {
              "minimum_applied_gain_db": -.1, "final_gain_db": -.1,
              "nonzero_gain_bins": 980, "action_counts": {"attenuate": 1}}}}}
    (run / "summary.json").write_text(json.dumps(meta), encoding="utf-8")
    rendered = renderer.preflight(report_path, audit, decisions, run, Path(__file__))
    assert len(rendered[3]) == 7
    # A single modified source invalidates the trial before a WAV output exists.
    sf.write(inputs["GREEN"], np.zeros(15_000), 100, subtype="FLOAT")
    with pytest.raises(ValueError, match="hash mismatch for GREEN"):
        renderer.preflight(report_path, audit, decisions, run, Path(__file__))
