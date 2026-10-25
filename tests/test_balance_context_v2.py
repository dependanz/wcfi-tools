"""Sustained musical families should not require amplitude onsets to be usable."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from wcfi_tools.audio.balance_activity import vector
from wcfi_tools.audio.balance_context import assess_context as assess_previous_context
from wcfi_tools.audio.balance_context_v2 import assess_context

RATE = 16000
TIME = np.arange(6 * RATE) / RATE


def normalize(x, rms=.08):
    return (x * rms / np.sqrt(np.mean(x ** 2))).astype('float32')


def tone(frequency, partials=6):
    return normalize(sum(np.sin(2 * np.pi * frequency * k * TIME + .13 * k) / k
                         for k in range(1, partials + 1)))


def backing():
    return normalize((1.1 + .8 * np.sin(2 * np.pi * .71 * TIME))
                     * np.sin(2 * np.pi * 997 * TIME))


def assess_signal(signal, role='other'):
    raw = {role: signal, 'vocals' if role != 'vocals' else 'other': backing()}
    previous = assess_previous_context(raw, RATE)
    result = assess_context(raw, RATE)
    assert result['raw_evidence'] == previous['raw_evidence']
    assert result['retained_roles'] == previous['retained_roles']
    return result, previous


@pytest.mark.parametrize('signal,role', [
    (tone(55), 'bass'),
    (tone(82.4069), 'bass'),
    (tone(110), 'other'),
    (tone(174.614), 'vocals'),
    (normalize(tone(130.8128) + tone(164.8138) + tone(195.9977)), 'other'),
    (normalize(sum(tone(f, 3) for f in (220, 220.31, 277.18, 329.63))), 'other'),
])
def test_sustained_bass_vowel_drone_chords_and_detuned_pad_can_support_activity(signal, role):
    result, previous = assess_signal(signal, role)
    assert result['roles'][role]['evidence_state'] == 'supported'
    # The detuned pad can already qualify through natural amplitude beating.
    if 'stationary_energy_or_sustained_arrangement' in previous['roles'][role]['reasons']:
        assert result['roles'][role]['sustained_signal_evidence']['sustained_structure_supported']
    else:
        assert result['roles'][role]['temporal_structure_observed']
    assert result['automatic_balance_allowed']
    assert not result['auto_apply_allowed']


def test_pitch_sequence_and_changing_harmonics_do_not_need_amplitude_variation():
    notes = [110, 146.832, 164.814, 196, 220, 261.626]
    sections = []
    for index, frequency in enumerate(notes):
        segment = tone(frequency, 3 + index % 3)[:RATE]
        sections.append(normalize(segment))
    result, previous = assess_signal(np.concatenate(sections))
    assert 'stationary_energy_or_sustained_arrangement' in previous['roles']['other']['reasons']
    evidence = result['roles']['other']['sustained_signal_evidence']
    assert result['roles']['other']['evidence_state'] == 'supported'
    assert evidence['centroid_range_hz'] > 50


@pytest.mark.parametrize('color', ['white', 'pink', 'brown', 'band_limited'])
def test_stationary_noise_families_remain_ambiguous(color):
    rng = np.random.default_rng(61)
    white = rng.normal(size=len(TIME))
    spectrum = np.fft.rfft(white)
    frequency = np.fft.rfftfreq(len(white), 1 / RATE)
    if color == 'pink':
        spectrum /= np.sqrt(np.maximum(frequency, 30))
    elif color == 'brown':
        spectrum /= np.maximum(frequency, 30)
    elif color == 'band_limited':
        spectrum *= np.exp(-.5 * ((frequency - 900) / 180) ** 2)
    signal = np.fft.irfft(spectrum)
    if color == 'brown':
        # Brown noise naturally has slow energy swings. This negative control
        # represents a steady noise floor after AGC, distinct from the unchanged
        # temporal-activity policy for a fluctuating signal.
        blocks = signal.reshape(-1, RATE // 10)
        signal = (blocks / np.sqrt(np.mean(blocks ** 2, axis=1, keepdims=True))).reshape(-1)
    result, previous = assess_signal(normalize(signal))
    assert 'stationary_energy_or_sustained_arrangement' in previous['roles']['other']['reasons']
    assert result['roles']['other']['evidence_state'] == 'ambiguous'
    assert not result['automatic_balance_allowed']


@pytest.mark.parametrize('frequency,partials', [(50, 1), (60, 1), (50, 8), (60, 8)])
def test_mains_fundamental_and_harmonic_hum_are_not_excused_as_sustained_notes(frequency, partials):
    result, _ = assess_signal(tone(frequency, partials), 'bass')
    assert 'stationary_mains_overlap_needs_context' in result['roles']['bass']['reasons']
    assert result['roles']['bass']['sustained_signal_evidence']['mains_like_dominance']
    assert not result['automatic_balance_allowed']


def test_note_near_mains_remains_uncertain_without_diagnosing_contamination():
    result, _ = assess_signal(tone(49, 1), 'bass')
    assert 'stationary_mains_overlap_needs_context' in result['roles']['bass']['reasons']
    assert result['roles']['bass']['performer_identity'] == 'unconfirmed'
    assert 'bass' in result['retained_roles']


def test_quiet_real_instrument_remains_retained_with_weak_signal_objections():
    result, previous = assess_signal(tone(110) * .005)
    row = result['roles']['other']
    assert row['sustained_signal_evidence']['sustained_structure_supported']
    assert 'weak_absolute_energy' in row['reasons']
    assert row['evidence_state'] == 'ambiguous'
    assert result['retained_roles'] == previous['retained_roles']
    assert not result['automatic_balance_allowed']


def test_quiet_duplicate_bleed_cannot_be_excused_by_musical_spectral_structure():
    source = tone(220)
    result = assess_context({'vocals': source, 'other': source * .005}, RATE)
    assert result['roles']['other']['sustained_signal_evidence']['sustained_structure_supported']
    assert 'signal_similarity_needs_context' in result['roles']['other']['reasons']
    assert result['roles']['other']['similar_signals'] == ['vocals']
    assert not result['automatic_balance_allowed']


def test_stationary_drum_channel_remains_uncertain_even_when_tonal():
    result, _ = assess_signal(tone(110), 'drums')
    assert 'stationary_drums_need_percussive_context' in result['roles']['drums']['reasons']
    assert not result['automatic_balance_allowed']


def test_raw_context_is_independent_of_candidate_level_and_mute():
    result, _ = assess_signal(tone(110))
    unchanged = copy.deepcopy(result)
    evidence = result['raw_evidence']
    baseline = vector(evidence)
    powers = np.asarray(evidence['powers'])
    assert np.allclose(vector(evidence, powers=powers * .01)['relative_db'], baseline['relative_db'])
    powers[:, 3] = 0
    muted = vector(evidence, powers=powers)
    assert muted['context'] == baseline['context']
    assert muted['shared_frames'] == baseline['shared_frames']
    assert result == unchanged
    json.dumps(result, allow_nan=False)


def test_stereo_polarity_is_retained_and_invalid_length_rejected():
    musical = tone(110)
    result, _ = assess_signal(np.column_stack([musical, -musical]))
    assert result['roles']['other']['evidence_state'] == 'supported'
    with pytest.raises(ValueError, match='synchronized'):
        assess_context({'other': musical, 'vocals': backing()[:-1]}, RATE)


def test_spectral_support_does_not_erase_insufficient_duration():
    raw = {'other': tone(110)[:RATE + RATE // 2], 'vocals': backing()[:RATE + RATE // 2]}
    result = assess_context(raw, RATE)
    assert result['roles']['other']['sustained_signal_evidence']['sustained_structure_supported']
    assert result['roles']['other']['evidence_state'] == 'insufficient'
    assert 'insufficient_energy_duration' in result['roles']['other']['reasons']
    assert not result['automatic_balance_allowed']
