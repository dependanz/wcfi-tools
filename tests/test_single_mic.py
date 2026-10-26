"""Wrapper validation and direct checks of the RED-specific native export path."""
from dataclasses import replace
import os
from pathlib import Path

import pytest

from wcfi_tools.audio.single_mic import MicSettings, NativeMicDsp


@pytest.mark.parametrize("change", [{"trim_db": 6.01}, {"trim_db": -6.01},
    {"max_boost_db": 7}, {"max_cut_db": 7}, {"auto_enabled": 1}, {"target_dbfs": float("nan")}])
def test_bounded_settings_rejected_before_native_loading(change):
    with pytest.raises(ValueError):
        MicSettings(**change)


@pytest.fixture
def dll():
    configured = os.environ.get("WCFI_MIC_TEST_DLL")
    if not configured:
        pytest.skip("Set WCFI_MIC_TEST_DLL to run native RED wrapper integration")
    path = Path(configured)
    assert path.is_file(), "Explicit native integration DLL is missing"
    return path


def test_native_calibration_export_and_manual_takeover(dll):
    import numpy as np
    sample = np.full((4800, 2), 10 ** (-30 / 20), np.float32)
    settings = MicSettings(auto_enabled=True)
    with NativeMicDsp(dll, settings) as dsp:
        for _ in range(49):
            assert np.array_equal(dsp.process(sample), sample)
        before = dsp.status()
        assert before["state"] == 2 and before["reserved"] == 49
        assert np.array_equal(dsp.process(sample), sample)
        locked = dsp.status()
        assert locked["state"] == 3 and locked["reserved"] == 50
        assert locked["target_gain_db"] == pytest.approx(4, abs=1e-6)
        for _ in range(10):
            dsp.process(sample)
        assert dsp.status()["applied_gain_db"] == pytest.approx(1, abs=1e-7)
        dsp.configure(replace(settings, auto_enabled=False, trim_db=-3))
        assert dsp.status()["state"] == 0 and dsp.status()["auto_gain_db"] == 0
        for _ in range(5):
            dsp.process(sample)
        assert dsp.status()["applied_gain_db"] == pytest.approx(-3, abs=1e-7)
        assert dsp.status()["target_gain_db"] == -3


def test_wrapper_invalid_audio_cannot_mutate_native_state(dll):
    import numpy as np
    with NativeMicDsp(dll) as dsp:
        before = dsp.status()
        for bad in (np.zeros((2, 3)), np.zeros((0, 2)), np.full((2, 2), float("nan"))):
            with pytest.raises(ValueError):
                dsp.process(bad)
            assert dsp.status() == before
        assert np.array_equal(dsp.process(np.zeros((20, 2), np.float32)), np.zeros((20, 2), np.float32))
    with pytest.raises(RuntimeError):
        dsp.status()
    with pytest.raises(RuntimeError):
        dsp.configure(MicSettings())


def test_native_manual_mode_keeps_sample_guard(dll):
    import numpy as np
    with NativeMicDsp(dll, MicSettings(trim_db=6)) as dsp:
        out = dsp.process(np.ones((48000, 2), np.float32))
        assert np.max(np.abs(out)) <= 10 ** (-1 / 20) + 1e-7
        assert dsp.status()["headroom_restraints"] > 0
        assert dsp.status()["state"] == 0
