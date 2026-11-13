"""Passage selection must preserve uncertainty and avoid silence/overlap."""
import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_passages import select_passages


def test_distinct_active_candidates_preserve_uncertain_musical_labels(tmp_path):
    rate = 32000
    seconds = 80
    t = np.arange(rate*seconds)/rate
    # Repeated hits, with a deliberately quieter and a louder region.
    envelope = .3+.7*np.exp(-((t % .4)/.04))
    level = np.where(t < 24, .03, np.where(t < 50, .3, .08))
    audio = level*envelope*np.sin(2*np.pi*100*t)
    path = tmp_path/"kit.wav"
    sf.write(path, audio, rate)
    passages = select_passages(path)
    assert [p["id"] for p in passages] == ["quiet", "loud", "fills"]
    assert passages[1]["rms_dbfs"]-passages[0]["rms_dbfs"] > 12
    for index, passage in enumerate(passages):
        assert not passage["musical_label_verified"]
        assert passage["detected_energy_attacks"] >= 8
        assert all(abs(passage["start_seconds"]-other["start_seconds"]) >= 14 for other in passages[:index])


def test_silence_does_not_become_a_quiet_drumming_example(tmp_path):
    path = tmp_path/"silence.wav"
    sf.write(path, np.zeros(32000*40, np.float32), 32000)
    with pytest.raises(ValueError, match="Not enough active"):
        select_passages(path)
