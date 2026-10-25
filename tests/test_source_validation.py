"""Signal tests for the controlled interventions, not tests of human/model expertise."""
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.soundcheck.source_validation import (
    hf_event_boost,
    peak_eq,
    prepare_comparisons,
    source_variants,
)


def stereo_signal(rate=24000, seconds=10):
    t = np.arange(rate * seconds) / rate
    # Known vocal-like harmonic signal with separate HF events, and stereo sides.
    lead = .06 * np.sin(2 * np.pi * 220 * t)
    lead += .02 * np.sin(2 * np.pi * 6000 * t) * ((t % 2) < .15)
    return np.column_stack([lead, lead * .7])


def test_quiet_lead_changes_only_that_source_and_sham_is_identical():
    lead = stereo_signal()
    t = np.arange(len(lead)) / 24000
    backing = np.column_stack([.1 * np.sin(2 * np.pi * 440 * t)] * 2)
    variants = {name: audio for name, audio, _ in source_variants(lead, backing, 24000)}
    for reduction in (6, 12):
        reconstructed = (variants[f"lead_minus_{reduction}"] - variants["control"])
        reconstructed /= 10 ** (-reduction / 20) - 1
        np.testing.assert_allclose(reconstructed, lead, atol=1e-14)
    np.testing.assert_array_equal(variants["control"], variants["sham_control"])


def test_silent_backing_cannot_be_used_to_validate_vocal_balance():
    lead = stereo_signal()
    with pytest.raises(ValueError, match="Both lead and backing"):
        source_variants(lead, np.zeros_like(lead), 24000)


def test_peak_eq_has_correct_center_gain_without_collapsing_stereo():
    rate = 48000
    t = np.arange(rate * 2) / rate
    signal = np.column_stack([np.sin(2 * np.pi * 300 * t)] * 2)
    signal[:, 1] *= .3
    actual = peak_eq(signal, rate, 300, 9)[rate:]
    measured_gain = 10 * np.log10(np.mean(actual[:, 0] ** 2) /
                                 np.mean(signal[rate:, 0] ** 2))
    assert measured_gain == pytest.approx(9, abs=.01)
    np.testing.assert_allclose(actual[:, 1], actual[:, 0] * .3, atol=1e-12)


def test_hf_intervention_survives_the_actual_mono_16k_conversion():
    from scipy.signal import butter, resample_poly, sosfiltfilt

    lead = stereo_signal()
    changed, facts = hf_event_boost(lead, 24000)
    assert facts["phonemes_human_verified"] is False
    assert 0 < facts["selected_frame_fraction"] < .5
    band = butter(3, [4500, 7500], fs=16000, btype="band", output="sos")
    before = sosfiltfilt(band, resample_poly(lead.mean(axis=1), 2, 3))
    after = sosfiltfilt(band, resample_poly(changed.mean(axis=1), 2, 3))
    assert 10 * np.log10(np.mean(after ** 2) / np.mean(before ** 2)) > 6
    assert np.isfinite(changed).all()


def test_duplicate_sources_are_refused_before_output(tmp_path):
    stem = tmp_path / "stem.wav"
    sf.write(stem, stereo_signal(), 24000)
    config = {"kind": "wcfi_stem_validation", "mix_basis": "test",
              "starts": [0, 1], "lead": {"file": "stem.wav", "role": "lead"},
              "backing": [{"file": "stem.wav", "role": "instruments"}]}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(config))
    output = tmp_path / "trial"
    with pytest.raises(ValueError, match="distinct"):
        prepare_comparisons(profile, output, ffmpeg=stem)
    assert not output.exists()


def test_unaligned_stems_are_refused_before_output(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, stereo_signal(), 24000)
    sf.write(b, stereo_signal(seconds=11), 24000)
    config = {"kind": "wcfi_stem_validation", "mix_basis": "test",
              "starts": [0, 1], "lead": {"file": "a.wav", "role": "lead"},
              "backing": [{"file": "b.wav", "role": "instruments"}]}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(config))
    output = tmp_path / "trial"
    with pytest.raises(ValueError, match="frame count"):
        prepare_comparisons(profile, output, ffmpeg=a)
    assert not output.exists()
