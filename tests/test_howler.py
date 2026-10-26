from itertools import pairwise

import numpy as np
import pytest

from wcfi_tools.audio.howler import HowlerDetector

RATE = 16000


def tone(seconds=3, hz=1373., gain=.1):
    t = np.arange(round(seconds * RATE)) / RATE
    return t, gain * np.sin(2 * np.pi * hz * t)


def detect(audio, *, chunks=777, **kwargs):
    detector = HowlerDetector(RATE, **kwargs)
    events = []
    for start in range(0, len(audio), chunks):
        events.extend(detector.feed(audio[start:start + chunks]))
    events.extend(detector.finish())
    return detector, events


def test_growing_isolated_tone_localizes_and_escalates_without_claiming_cause():
    t, wave = tone()
    signal = wave * 10 ** ((-22 + 10 * t) / 20)
    signal += np.random.default_rng(9).normal(0, .0001, len(t))
    detector, events = detect(signal)
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "end"
    assert events[-1]["classification"] == "feedback_like"
    assert abs(events[-1]["frequency_hz"] - 1373) < 1
    assert events[-1]["growth_db_per_second"] == pytest.approx(10, abs=.3)
    first_growth = next(event for event in events if event["classification"] == "feedback_like")
    assert first_growth["event"] == "update"
    assert first_growth["end_seconds"] < 1.0
    assert events[-1]["score"] < 1
    assert detector.processed_seconds == 3
    assert not detector.configuration["score_is_probability"]


def test_stationary_sine_is_ambiguous_and_preserves_level_and_time():
    _, signal = tone()
    _, events = detect(signal)
    assert {event["classification"] for event in events} == {"sustained_tone"}
    assert events[-1]["peak_dbfs"] == pytest.approx(-20, abs=.5)
    assert events[0]["end_seconds"] < .6
    assert events[-1]["end_seconds"] <= 3
    assert events[-1]["start_seconds"] == 0
    updates = [event["end_seconds"] for event in events if event["event"] != "end"]
    assert all(b - a >= .99999 for a, b in pairwise(updates))


@pytest.mark.parametrize("kind", ["silence", "noise", "brief_tone", "harmonics"])
def test_rejects_silence_broadband_noise_transient_and_harmonic_instrument(kind):
    t, signal = tone()
    if kind == "silence":
        signal[:] = 0
    elif kind == "noise":
        signal = np.random.default_rng(7).normal(0, .04, len(t))
    elif kind == "brief_tone":
        signal[t > .12] = 0
    else:
        signal = sum(.1 / k * np.sin(2 * np.pi * 220 * k * t) for k in range(1, 7))
    assert detect(signal)[1] == []


def test_arbitrary_chunk_boundaries_produce_identical_events_and_final_duration():
    t, wave = tone(seconds=2.531)
    signal = wave * 10 ** ((-18 + 10 * t) / 20)
    _, reference = detect(signal, chunks=len(signal))
    for size in (1, 127, 2048, 30000):
        detector, actual = detect(signal, chunks=size)
        assert actual == reference
        assert detector.samples_received == len(signal)


def test_multiple_tones_are_independent_and_end_after_evidence_not_timeout():
    t, first = tone(hz=1373.)
    _, second = tone(hz=3217.)
    first[t >= 1.5] = 0
    _, events = detect(first + second)
    ends = [event for event in events if event["event"] == "end"]
    assert len(ends) == 2
    ends.sort(key=lambda row: row["frequency_hz"])
    assert ends[0]["end_seconds"] < 1.7
    assert ends[1]["end_seconds"] > 2.9
    assert len({event["id"] for event in ends}) == 2


def test_discontinuity_ends_events_and_resets_persistence_with_unique_ids():
    detector = HowlerDetector(RATE)
    _, signal = tone(seconds=1)
    first = detector.feed(signal)
    ended = detector.reset(start_seconds=2.0)
    assert ended[-1]["event"] == "end"
    assert detector.feed(signal[:1600]) == []
    second = detector.feed(signal[1600:]) + detector.finish()
    assert second[0]["start_seconds"] == 2
    assert second[0]["id"] != first[0]["id"]
    assert detector.processed_seconds == 3
    assert detector.finish() == []
    with pytest.raises(ValueError, match="finished"):
        detector.feed(signal)


@pytest.mark.parametrize("options", [
    {"sample_rate": 0}, {"sample_rate": 16000.0}, {"sample_rate": True},
    {"min_frequency": 10000}, {"min_frequency": float("nan")},
    {"min_duration": 0}, {"threshold_db": float("inf")}, {"threshold_db": 2},
])
def test_rejects_invalid_configuration(options):
    with pytest.raises(ValueError):
        HowlerDetector(**({"sample_rate": RATE} | options))


@pytest.mark.parametrize("samples", [np.zeros((100, 2)), [np.nan], [np.inf],
                                     np.array([1], dtype=np.int16)])
def test_invalid_audio_is_not_accepted_as_silence(samples):
    detector = HowlerDetector(RATE)
    with pytest.raises(ValueError):
        detector.feed(samples)
    assert detector.samples_received == 0


def test_internal_storage_does_not_grow_with_stream_length():
    detector = HowlerDetector(RATE)
    _, signal = tone(seconds=1)
    for _ in range(15):
        detector.feed(signal)
    assert len(detector._tracks) <= 8
    assert all(len(track.history) <= 64 for track in detector._tracks)
    assert detector._buffer.size == detector.window_size
