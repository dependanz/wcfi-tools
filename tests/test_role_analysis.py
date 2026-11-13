"""Role selection, bounded proposals, streaming continuity and real-file replay."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import butter, sosfilt
from typer.testing import CliRunner

from wcfi_tools.audio.role_analysis import analyze_pair, render_proposals
from wcfi_tools.audio.role_replay import analyze_files, analyze_take
from wcfi_tools.audio.role_stream import RoleAnalysisStream, SampleHistory
from wcfi_tools.audio.role_validation import _bursts
from wcfi_tools.audio.role_watch import watch_nodes
from wcfi_tools.audio.roles import get_profile
from wcfi_tools.cli import app
from wcfi_tools.soundcheck.profiles import file_hash

RATE = 48000


@pytest.fixture
def body():
    t = np.arange(8 * RATE) / RATE
    # Known harmonic synthetic source; no claim that it models natural singing.
    return (.03 * np.sin(2*np.pi*220*t) + .02*np.sin(2*np.pi*440*t))[:, None].astype(np.float32)


def test_role_selects_modules_and_different_semantics(body):
    burst, _ = _bursts(body, RATE)
    red = analyze_pair(body, burst, RATE, "red", aligned=True)
    kit = analyze_pair(body, burst, RATE, "kit", aligned=True)
    assert red["after"]["analyses"]["sibilance_candidates"]["candidate_count"] >= 3
    assert not red["after"]["analyses"]["sibilance_candidates"]["confirmed_sibilance"]
    assert "sibilance_candidates" not in kit["after"]["analyses"]
    assert "transient_dynamics" in kit["after"]["analyses"]
    assert "transient_dynamics" not in red["after"]["analyses"]
    assert any(a["kind"] == "hf_burst_reduction" for a in red["proposals"])
    assert not any(a["kind"] == "hf_burst_reduction" for a in kit["proposals"])


def test_correlated_kit_hf_bleed_suppresses_vocal_preview(body):
    burst, _ = _bursts(body, RATE)
    result = analyze_pair(burst, burst, RATE, "red", context=burst)
    candidate = result["after"]["analyses"]["sibilance_candidates"]
    assert candidate["bleed_ambiguous"]
    assert not any(a["kind"] == "hf_burst_reduction" for a in result["proposals"])


@pytest.mark.parametrize("role", ["red", "kit"])
@pytest.mark.parametrize("level", [0, 1e-5])
def test_silence_and_quiet_signal_never_boosted(body, role, level):
    signal = body * level
    result = analyze_pair(signal, signal, RATE, role, aligned=True)
    assert not result["proposals"]
    assert all(abs(v) < 1e-8 for v in result["paired_change"].values())


def test_identity_and_gain_are_not_mistaken_for_drum_transient_loss(body):
    rng = np.random.default_rng(43)
    audio = (rng.normal(size=body.shape) * .02).astype(np.float32)
    for multiplier in (1., .5):
        result = analyze_pair(audio, audio * multiplier, RATE, "kit", aligned=True)
        assert abs(result["paired_change"]["crest_change_db"]) < 1e-5
        assert not any(f["kind"] == "drum_transient_change" for f in result["after"]["findings"])


def test_gain_is_attenuation_only_and_not_clipping_reconstruction(body):
    clipped = np.clip(body * 100, -1, 1)
    result = analyze_pair(body, clipped, RATE, "red", aligned=True)
    actions = [a for a in result["proposals"] if a["kind"] == "software_gain"]
    rendered, applied = render_proposals(clipped, RATE, actions)
    assert applied[0]["gain_db"] <= 0
    assert np.max(np.abs(rendered)) <= 10**(-6/20) + 1e-6
    np.testing.assert_allclose(rendered, clipped * 10**(applied[0]["gain_db"]/20), atol=1e-7)
    assert any("clipping" in f["kind"] for f in result["after"]["findings"])
    again = analyze_pair(rendered, rendered, RATE, "red")
    assert again["before"]["metrics"]["full_scale_samples"] == 0
    assert again["before"]["metrics"]["possible_flat_top_samples"] > 12
    assert any("clipping" in f["kind"] for f in again["before"]["findings"])


def test_float_headroom_above_zero_is_not_proof_of_clipping(body):
    audio = body * 100
    result = analyze_pair(audio, audio, RATE, "kit")
    assert result["before"]["metrics"]["above_full_scale_samples"] > 0
    assert result["before"]["metrics"]["full_scale_samples"] == 0


def test_antiphase_stereo_keeps_energy_and_channels(body):
    stereo = np.column_stack((body[:, 0], -body[:, 0]))
    result = analyze_pair(stereo, stereo, RATE, "kit")
    assert result["before"]["metrics"]["rms_dbfs"] > -50
    assert result["before"]["analyses"]["stereo_health"]["correlation"] == pytest.approx(-1)
    rendered, _ = render_proposals(stereo, RATE, [])
    np.testing.assert_array_equal(rendered, stereo)


def test_hf_preview_is_opt_in_and_attenuates_known_injected_band(body):
    audio, ranges = _bursts(body, RATE)
    result = analyze_pair(body, audio, RATE, "red")
    actions = [a for a in result["proposals"] if a["kind"] == "hf_burst_reduction"]
    unchanged, applied = render_proposals(audio, RATE, actions)
    assert not applied
    np.testing.assert_array_equal(unchanged, audio)
    processed, applied = render_proposals(audio, RATE, actions, include_experimental=True)
    assert applied and processed.shape == audio.shape
    band = butter(4, [6500, 10000], btype="bandpass", fs=RATE, output="sos")
    old, new = sosfilt(band, audio, axis=0), sosfilt(band, processed, axis=0)
    mask = np.zeros(len(audio), bool)
    for lo, hi in ranges:
        mask[round((lo+.03)*RATE):round((hi-.03)*RATE)] = True
    assert np.mean(new[mask]**2) < np.mean(old[mask]**2) * .85
    selected = np.zeros(len(audio), bool)
    for event in actions[0]["events"]:
        selected[round(event["start_seconds"]*RATE):round(event["end_seconds"]*RATE)] = True
    np.testing.assert_array_equal(processed[~selected], audio[~selected])


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_invalid_audio_refuses_proposals(body, bad):
    body[100, 0] = bad
    with pytest.raises(ValueError, match="Non-finite"):
        analyze_pair(body, body, RATE, "red")


def test_unaligned_captures_have_no_change_attribution(body):
    result = analyze_pair(body, body, RATE, "kit")
    assert result["paired_change"] is None
    with pytest.raises(ValueError, match="matching"):
        analyze_pair(body, body[:-1], RATE, "red")


def test_waveform_alignment_accepts_broadband_delay_and_rejects_repetitive_tone(body):
    rng = np.random.default_rng(91)
    a = rng.normal(0, .03, (RATE, 1)).astype(np.float32)
    b = np.concatenate((np.zeros((64, 1), np.float32), a[:-64])) * .5
    result = analyze_pair(a, b, RATE, "red", estimate_alignment=True)
    assert result["alignment"]["available"]
    assert result["alignment"]["lag_samples"] == pytest.approx(64, abs=8)
    assert result["paired_change"]["rms_change_db"] == pytest.approx(-6.0206, abs=.05)
    tone = analyze_pair(body, body, RATE, "red", estimate_alignment=True)
    assert not tone["alignment"]["available"] and tone["paired_change"] is None
    short = analyze_pair(a[:RATE//2], b[:RATE//2], RATE, "red", estimate_alignment=True)
    assert not short["alignment"]["available"] and short["paired_change"] is None


def test_sample_history_wraps_and_rejects_gaps():
    history = SampleHistory(100, 1)
    history.append(np.arange(80, dtype=np.float32)[:, None], 0)
    history.append(np.arange(80, 140, dtype=np.float32)[:, None], 80)
    assert history.start == 40 and history.end == 140
    np.testing.assert_array_equal(history.read(40, 140)[:, 0], np.arange(40, 140))
    assert history.append(np.ones((10, 1), np.float32), 200)
    with pytest.raises(ValueError):
        history.read(130, 140)


def test_streaming_replay_agrees_with_offline_window(body):
    audio = body[:RATE]
    stream = RoleAnalysisStream("red", RATE, 1, seconds=1, interval=.5)
    state = {"devices": [{"gain": -2}]}
    stream.set_chain_state(state)
    state["devices"][0]["gain"] = 10  # Caller cannot mutate captured state.
    for start in range(0, len(audio), 512):
        for stage, data in (("pre_fx", audio), ("post_fx", audio*.5)):
            stream.push(stage, data[start:start+512], start)
    window = stream.window()
    assert window and window["chain_state"]["devices"][0]["gain"] == -2
    result = stream.analyze(window, aligned=True)
    expected = analyze_pair(audio, audio*.5, RATE, "red", aligned=True)
    assert result["before"] == expected["before"]
    assert result["after"] == expected["after"]
    assert result["paired_change"] == expected["paired_change"]
    assert stream.window() is None
    stream.set_chain_state({"devices": [{"gain": 0}]})
    stale = stream.analyze(window, aligned=True)
    assert not stale["state_current_at_completion"] and not stale["proposals"]
    assert stream.window() is None


def test_stream_gaps_epochs_and_bad_clock_discard_old_audio(body):
    stream = RoleAnalysisStream("kit", RATE, 1, seconds=.5)
    audio = body[:RATE//2]
    for stage in ("pre_fx", "post_fx"):
        stream.push(stage, audio, 0)
    assert stream.window()
    stream.push("pre_fx", audio, RATE, epoch=0)
    assert stream.last_reset_reason == "observation_gap" and stream.window() is None
    stream.push("post_fx", audio, RATE, epoch=1)
    assert stream.last_reset_reason == "node_epoch_changed"
    stream.push("pre_fx", audio, RATE, valid=False)
    assert stream.last_reset_reason == "invalid_clock_or_audio" and stream.window() is None


@pytest.fixture
def take(tmp_path, body):
    folder = tmp_path / "take"
    folder.mkdir()
    rng = np.random.default_rng(10)
    kit = body + rng.normal(0, .015, body.shape).astype(np.float32)
    sources = []
    for name, audio, ports in (("RED", body, [1]), ("KIT", np.column_stack((kit, kit*.9)), [2, 3])):
        path = folder / (name + ".wav")
        sf.write(path, audio, RATE, subtype="FLOAT")
        sources.append({"track_name": name, "track_id": len(sources), "file": path.name,
            "sample_rate": RATE, "frames": len(audio), "channels": audio.shape[1],
            "emulator_inputs": ports, "sha256": file_hash(path), "arrangement_start_beat": 0,
            "arrangement_end_beat": 16})
    (folder / "manifest.json").write_text(json.dumps({"kind": "wcfi_real_source_take",
        "schema_version": 1, "take_id": "take", "sample_rate": RATE, "frames": len(body),
        "sources": sources}), encoding="utf-8")
    return folder


def test_recorded_take_is_read_only_and_profiles_follow_names(take, tmp_path):
    stamps = {p: file_hash(p) for p in take.glob("*.wav")}
    out = tmp_path / "analysis"
    summary = analyze_take(take, out, start=0, seconds=2, previews=True)
    assert summary["daw_writes"] == 0 and summary["audio_uploads"] == 0
    assert (out / "report.html").is_file()
    red, kit = [json.loads((out / (n + ".json")).read_text()) for n in ("RED", "KIT")]
    assert red["provenance"]["post_fx_kind"].startswith("bypass_reference")
    assert kit["profile"]["role"] == "drum_bus"
    assert "sibilance_candidates" not in kit["after"]["analyses"]
    assert all(file_hash(p) == digest for p, digest in stamps.items())
    with pytest.raises(ValueError, match="immutable"):
        analyze_take(take, out, start=0, seconds=2)
    with pytest.raises(ValueError, match="beyond"):
        analyze_take(take, tmp_path/"beyond", start=7, seconds=2)


def test_manifest_corruption_is_rejected(take, tmp_path):
    path = take / "RED.wav"
    sf.write(path, np.zeros(RATE), RATE)
    with pytest.raises(ValueError, match="checksum"):
        analyze_take(take, tmp_path/"bad", start=0, seconds=1)


def test_silent_preview_and_missing_alignment_contract(tmp_path):
    path = tmp_path/"silent.wav"
    sf.write(path, np.zeros(RATE), RATE)
    analyze_files(path, tmp_path/"silence", "red", seconds=1, previews=True)
    result = json.loads((tmp_path/"silence/RED.json").read_text())
    assert not result["proposals"]
    rendered, _ = sf.read(tmp_path/"silence/RED-candidate.wav", always_2d=True)
    assert not np.any(rendered)


def test_cli_profiles_and_file_are_headless(tmp_path, body, monkeypatch):
    from wcfi_tools.commands import role_analysis
    monkeypatch.setattr(role_analysis, "limit_resources", lambda: None)
    runner = CliRunner()
    result = runner.invoke(app, ["audio", "analyze", "profiles"])
    assert result.exit_code == 0 and '"lead_vocal"' in result.output
    path = tmp_path/"raw.wav"
    sf.write(path, body, RATE, subtype="FLOAT")
    result = runner.invoke(app, ["audio", "analyze", "file", str(path), "--profile", "red",
        "--seconds", "1", "--output", str(tmp_path/"cli")])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path/"cli/RED.json").read_text())["automatic_daw_writes"] is False


def test_unknown_role_does_not_fall_back_to_vocal():
    with pytest.raises(ValueError):
        get_profile("electric_guitar")


def test_passive_watch_uses_read_only_state_and_never_saves_audio(tmp_path):
    class Bridge:
        def __init__(self):
            self.cursors = {"a": 0, "b": 0}
            self.closed = []

        def discover(self):
            return list(self.cursors)

        def open(self, identity):
            return identity

        def close(self, handle):
            self.closed.append(handle)

        def clock_ns(self):
            return time.monotonic_ns()

        def read(self, handle, _cursor):
            start = self.cursors[handle]
            count, rate = 480, 48000
            self.cursors[handle] += count
            t = np.arange(start, start+count)/rate
            block = (.03*np.sin(2*np.pi*220*t))[:, None].astype(np.float32)
            status = SimpleNamespace(label=b"RED", position=0 if handle == "a" else 1,
                written_frame=start+count, first_frame=start, pid=42, sample_rate=rate,
                channels=1, flags=1, active=1, callback_ns=time.monotonic_ns(),
                host_sample=start, block_size=count, epoch=1)
            return block, status

    class Client:
        def __init__(self):
            self.descriptor = {"pid": 42}

        def call(self, operation, **_kwargs):
            assert operation == "inspect_session"
            return {"session_id": "test", "tracks": [{"name": "RED", "devices": []}]}

    bridge = Bridge()
    out = tmp_path/"watch"
    result = watch_nodes(out, seconds=1.2, window_seconds=.5, interval=.2,
                         bridge_factory=lambda: bridge, client=Client())
    assert result["windows"] > 0 and not result["errors"]
    assert result["daw_writes"] == 0 and not result["audio_saved"]
    assert set(bridge.closed) == {"a", "b"}
    assert not list(out.glob("*.wav"))
    report = json.loads(next(out.glob("0001-*.json")).read_text())
    assert report["paired_change"] is None and report["chain_revision"]
    assert report["analysis_window_copy_to_completion_seconds"] >= 0
