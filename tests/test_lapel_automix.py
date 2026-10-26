"""New offline harness contracts only; no native load, host or playback."""
import ctypes as ct
import math

import numpy as np
import pytest

from wcfi_tools.audio.lapel_automix import (
    LevelerSettings, NativeConfig, NativeStatus, dbfs, quantiles,
    replacement_mix, summarize_windows,
)


def test_abi_structures_match_declared_native_v1_layout():
    assert ct.sizeof(NativeConfig) == 48
    assert NativeConfig.trim_db.offset == 8
    assert ct.sizeof(NativeStatus) == 88
    assert NativeStatus.input_rms_dbfs.offset == 16
    assert NativeStatus.samples_processed.offset == 64


@pytest.mark.parametrize("change", [
    {"auto_enabled": 1}, {"trim_db": 25}, {"trim_db": float("nan")},
    {"target_dbfs": -11}, {"max_boost_db": -1}, {"max_cut_db": True},
    {"activity_dbfs": -91},
])
def test_invalid_settings_fail_before_native_load(change):
    with pytest.raises(ValueError):
        LevelerSettings(**change)


def test_defaults_are_manual_unity_and_settings_immutable():
    config = LevelerSettings()
    assert config.auto_enabled is False and config.trim_db == 0
    copy = config.to_dict()
    copy["trim_db"] = 18
    assert config.trim_db == 0


def test_replacement_requires_verified_path_and_avoids_double_counting():
    old = np.asarray([[.1, -.2], [.2, .1]], dtype=np.float32)
    music = np.asarray([[.05, .05], [.02, .03]], dtype=np.float32)
    candidate = old * 2
    with pytest.raises(ValueError, match="verified"):
        replacement_mix(old + music, old, candidate, additive_path_verified=False)
    result = replacement_mix(old + music, old, candidate, additive_path_verified=True)
    assert np.allclose(result, candidate + music, atol=3e-8)
    assert np.allclose(replacement_mix(old + music, old, old,
                       additive_path_verified=True), old + music, atol=1e-8)


def test_mix_rejects_nonfinite_and_misaligned_inputs():
    value = np.zeros((2, 2), np.float32)
    with pytest.raises(ValueError, match="shapes"):
        replacement_mix(value, value[:1], value, additive_path_verified=True)
    value[0, 0] = math.nan
    with pytest.raises(ValueError, match="Nonfinite"):
        replacement_mix(value, value, value, additive_path_verified=True)


def test_activity_and_overlap_metrics_do_not_count_noise_or_music_silence():
    def row(t, voice, music, fixed, candidate):
        return {"start_seconds": t, "input_rms_dbfs": voice,
                "instruments_rms_dbfs": music, "fixed_rms_dbfs": fixed,
                "candidate_rms_dbfs": candidate}
    data = [row(0, -100, -30, -82, -82), row(.1, -50, -40, -32, -26),
            row(.2, -49, -100, -31, -25), row(120, -40, -30, -22, -20)]
    report = summarize_windows(data, 0, 120)
    assert report["window_count"] == 3
    assert report["activity_proxy_windows"] == 2
    assert report["overlapping_music_activity_windows"] == 1
    assert report["candidate"]["lapel_minus_instruments_db"]["p50"] == 14
    assert report["candidate"]["absolute_target_error_db"]["p50"] == .5
    assert report["activity_is_semantic_speech"] is False


def test_empty_and_silent_metrics_stay_explicitly_absent():
    assert quantiles([]) is None
    assert dbfs(0) is None
    assert summarize_windows([], 0, 1)["candidate"]["active_rms_dbfs"] is None
    assert dbfs(.01, power=True) == pytest.approx(-20)
    assert quantiles([0, 10])["p50"] == 5
