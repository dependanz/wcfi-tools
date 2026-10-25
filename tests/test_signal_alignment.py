"""Waveform alignment rejects quiet, ambiguous and unrelated signals."""
import numpy as np
import pytest

from wcfi_tools.audio.alignment import RATE, estimate_delay


@pytest.mark.parametrize("delay,scale", [(0, .2), (4800, 3), (-2400, -2)])
def test_delay_recovers_timing_under_gain_and_polarity_changes(delay, scale):
    source = np.random.default_rng(37).normal(0, .1, RATE * 2)
    target = np.roll(source, delay) * scale
    result = estimate_delay(source, target)
    assert result["available"]
    assert result["lag_ms"] == pytest.approx(delay / RATE * 1000, abs=.18)
    assert result["correlation"] > .99
    assert result["polarity"] == ("inverted" if scale < 0 else "same")


def test_ambiguous_tone_and_quiet_or_invalid_audio_are_not_latency_measurements():
    tone = np.sin(np.arange(RATE) * (2 * np.pi * 200 / RATE)) * .1
    result = estimate_delay(tone, np.roll(tone, 4800))
    assert not result["available"] and "ambiguous" in result["reason"]
    assert not estimate_delay(np.zeros(RATE), tone)["available"]
    assert not estimate_delay(np.full(RATE, np.nan), tone)["available"]
    assert not estimate_delay(tone[:500], tone[:500])["available"]


def test_independent_signals_do_not_create_a_fake_delay():
    rng = np.random.default_rng(112)
    assert not estimate_delay(rng.normal(size=RATE), rng.normal(size=RATE))["available"]

