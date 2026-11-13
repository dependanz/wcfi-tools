import copy

import numpy as np
import pytest

from wcfi_tools.audio.mix_coordinator import TRACKS
from wcfi_tools.audio.mix_coordinator_trial import calibration, stereo, validate_sources


def test_absent_backing_channels_are_not_normalized_or_enabled():
    levels = np.tile(np.array([-30., -110., -110., -32., -35., -34., -25.]), (300, 1))
    plan = calibration(levels)
    for name in ("YELLOW", "GREEN"):
        assert plan["baseline_gains_db"][name] == 0
        assert name not in plan["enabled_support_tracks"]
    assert set(plan["baseline_gains_db"]) == set(TRACKS)
    assert "BASS" not in plan["baseline_gains_db"]


def test_silent_lead_cannot_define_a_mix():
    with pytest.raises(ValueError, match="lead energy"):
        calibration(np.full((300, len(TRACKS)), -100.))


def test_low_energy_backing_reference_is_held_without_muting():
    levels = np.tile(np.array([-30., -46., -50., -32., -35., -34., -25.]), (300, 1))
    plan = calibration(levels)
    for name in ("YELLOW", "GREEN"):
        assert plan["active_bins"][name] >= 50
        assert plan["baseline_gains_db"][name] == 0
        assert name not in plan["enabled_support_tracks"]
        assert plan["automatic_eligibility"][name] == "backing_reference_unverified_low_energy"


def test_exact_exclusions_and_january_rejected_before_file_access(tmp_path):
    with pytest.raises(ValueError, match="January"):
        validate_sources({"sources": []}, "2026-01-24")
    sources = [{"track": name} for name in TRACKS]
    for excluded in ("BASS", "LAPEL", "MDMIC Talkback"):
        bad = copy.deepcopy(sources)
        bad[-1]["track"] = excluded
        with pytest.raises(ValueError, match="seven allowed"):
            validate_sources({"sources": bad, "identity_mapping": True}, "2026-10-04")


def test_mono_conversion_preserves_amplitude_and_stereo_is_unchanged():
    mono = np.array([[.3], [-.4]], dtype=np.float32)
    result = stereo(mono)
    np.testing.assert_array_equal(result[:, 0], mono[:, 0])
    np.testing.assert_array_equal(result[:, 1], mono[:, 0])
    pair = np.array([[.2, -.6]], dtype=np.float32)
    assert stereo(pair) is pair
