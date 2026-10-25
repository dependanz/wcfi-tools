"""DSP behavior and role-specific hypotheses; no audio device, cloud or DAW."""
import copy

import numpy as np
import pytest

from wcfi_tools.audio.role_processing import compress, decisions, deess, equalize, render_variants


def tone(amplitude=.2, seconds=1, hz=300):
    t = np.arange(round(seconds*48000))/48000
    return np.repeat((amplitude*np.sin(2*np.pi*hz*t))[:, None], 2, axis=1).astype(np.float32)


def test_quiet_bleed_does_not_trigger_processing_and_kit_never_runs_deessing():
    for role in ("red", "kit"):
        audio = tone(1e-6)
        recipe = decisions(audio, role, kit_context=audio if role == "red" else None)
        assert not recipe["eq"]["enabled"]
        assert not recipe["compression"]["enabled"]
        assert not recipe["deess"]["enabled"]
        versions, _ = render_variants(audio, recipe)
        assert all(np.array_equal(v, audio) for v in versions.values())
    recipe = decisions(tone(.8, hz=7000), "kit", kit_context=tone(.8))
    assert not recipe["deess"]["enabled"]
    assert recipe["deess"]["confirmed_sibilance"] is False


def test_bell_has_expected_centre_attenuation_and_preserves_stereo_and_disabled_identity():
    audio = tone(hz=300)
    recipe = {"enabled": True, "bands": [{"kind": "bell", "hz": 300, "gain_db": -2, "q": .7}]}
    output, _ = equalize(audio, recipe)
    assert 20*np.log10(np.sqrt(np.mean(output**2))/np.sqrt(np.mean(audio**2))) == pytest.approx(-2, abs=.001)
    assert np.array_equal(output[:, 0], output[:, 1])
    disabled, stats = equalize(audio, {"enabled": False})
    assert np.array_equal(disabled, audio) and stats["identical_control"]
    recipe["bands"][0]["gain_db"] = -20
    with pytest.raises(ValueError):
        equalize(audio, recipe)


def compression_recipe():
    return {"enabled": True, "ratio": 2, "threshold_dbfs": -20, "knee_db": 6,
        "attack_ms": 15, "release_ms": 100, "maximum_reduction_db": 3, "makeup_gain_db": 0}


def test_compression_has_no_boost_and_linked_stereo_preserves_channel_balance():
    audio = tone(.8)
    audio[:, 1] *= .4
    result, stats = compress(audio, compression_recipe())
    assert np.max(np.abs(result)-np.abs(audio)) <= 1e-7
    assert result[:, 1] == pytest.approx(result[:, 0]*.4, abs=1e-7)
    assert 2.9 < stats["maximum_reduction_db"] <= 3
    assert result.shape == audio.shape and np.isfinite(result).all()
    invalid = compression_recipe() | {"makeup_gain_db": 6}
    with pytest.raises(ValueError):
        compress(audio, invalid)


def test_compression_attack_and_release_do_not_behave_like_instantaneous_clipping():
    audio = tone(.01)
    audio[9600:19200] *= 80
    result, _ = compress(audio, compression_recipe())
    def relative_gain(start, end):
        return np.sqrt(np.mean(result[start:end]**2)/np.mean(audio[start:end]**2))
    assert relative_gain(9600, 10080) > relative_gain(16800, 17280)
    assert relative_gain(30000, 31000) > relative_gain(19500, 20500)


def fricative_example(with_drum_overlap):
    audio = tone(.12, hz=220)
    hf = tone(.35, hz=7000)
    audio[24000:30000] += hf[24000:30000]
    kit = tone(.03, hz=60)
    if with_drum_overlap:
        kit[24000:30000] += hf[24000:30000]
    return audio, kit


def test_deessing_requires_context_and_vetoes_coincident_bright_drums():
    audio, quiet_kit = fricative_example(False)
    assert not decisions(audio, "red")["deess"]["enabled"]
    candidate = decisions(audio, "red", kit_context=quiet_kit)["deess"]
    assert candidate["enabled"] and candidate["eligible_events"] >= 1
    assert candidate["confirmed_sibilance"] is False
    _, bright_kit = fricative_example(True)
    vetoed = decisions(audio, "red", kit_context=bright_kit)["deess"]
    assert not vetoed["enabled"]
    assert vetoed["rejected_kit_overlap_events"] >= 1
    assert np.array_equal(decisions(audio, "red", kit_context=quiet_kit)["deess"]["events"], candidate["events"])


def test_deessing_is_bounded_and_unrelated_phrases_return_to_unity():
    audio, kit = fricative_example(False)
    recipe = decisions(audio, "red", kit_context=kit)["deess"]
    result, stats = deess(audio, recipe)
    assert 0 < stats["maximum_reduction_db"] <= 2.5
    assert np.max(np.abs(result)-np.abs(audio)) <= 1e-7
    assert result[-2400:] == pytest.approx(audio[-2400:], abs=1e-5)
    invalid = copy.deepcopy(recipe)
    invalid["events"][0]["end_seconds"] = 100
    with pytest.raises(ValueError):
        deess(audio, invalid)
