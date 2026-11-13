"""RAW context must not turn uncertain bleed/quiet performance into gain advice."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from wcfi_tools.audio.balance_activity import frame_evidence, vector
from wcfi_tools.audio.balance_context import assess_context

RATE = 16000


def sources():
    t = np.arange(8 * RATE) / RATE
    # Independently modulated, comfortably supported deterministic test signals.
    return {
        role: (.12 * (1.1 + .8 * np.sin(2 * np.pi * (.43 + .17 * i) * t))
               * np.sin(2 * np.pi * (190 + 137 * i) * t)).astype('float32')
        for i, role in enumerate(('vocals', 'drums', 'bass', 'other'))
    }


def test_strong_distinct_modulated_sources_have_no_observed_context_objection():
    raw = sources()
    result = assess_context(raw, RATE)
    assert result['automatic_balance_allowed']
    assert not result['auto_apply_allowed']
    assert all(row['evidence_state'] == 'supported' for row in result['roles'].values())
    assert all(row['performer_identity'] == 'unconfirmed' for row in result['roles'].values())
    assert result['raw_evidence'] == frame_evidence(raw, RATE)
    json.dumps(result, allow_nan=False)


def test_very_quiet_but_structured_drums_are_retained_ambiguous_not_absent():
    raw = sources()
    raw['drums'] *= .005
    result = assess_context(raw, RATE)
    drums = result['roles']['drums']
    assert drums['temporal_structure_observed']
    assert drums['energy_supported_by_frozen_model']
    assert drums['evidence_state'] == 'ambiguous'
    assert 'weak_absolute_energy' in drums['reasons']
    assert 'drums' in result['retained_roles']
    assert not result['automatic_balance_allowed']


def test_quiet_copied_signal_flags_similarity_without_claiming_bleed_direction():
    raw = sources()
    raw['drums'] = raw['vocals'] * .005
    result = assess_context(raw, RATE)
    assert result['roles']['drums']['similar_signals'] == ['vocals']
    assert result['roles']['vocals']['similar_signals'] == ['drums']
    assert not result['automatic_balance_allowed']
    assert result['raw_evidence'] == frame_evidence(raw, RATE)


def test_stationary_noise_is_not_sufficient_evidence_of_drumming():
    raw = sources()
    raw['drums'] = np.random.default_rng(17).normal(0, .08, len(raw['drums'])).astype('float32')
    result = assess_context(raw, RATE)
    assert not result['roles']['drums']['temporal_structure_observed']
    assert 'stationary_energy_or_sustained_arrangement' in result['roles']['drums']['reasons']
    assert not result['automatic_balance_allowed']


def test_silent_source_retained_with_insufficient_energy_not_absence_claim():
    raw = sources()
    raw['bass'][:] = 0
    result = assess_context(raw, RATE)
    assert 'bass' in result['retained_roles']
    assert 'bass' not in result['energy_supported_roles']
    assert result['roles']['bass']['evidence_state'] == 'insufficient'
    assert not result['automatic_balance_allowed']


def test_candidate_common_gain_and_muting_cannot_change_cached_raw_context():
    context = assess_context(sources(), RATE)
    original = copy.deepcopy(context)
    raw_evidence = context['raw_evidence']
    powers = np.asarray(raw_evidence['powers'])
    baseline = vector(raw_evidence)
    quieter = vector(raw_evidence, powers=powers * .0001)
    assert np.allclose(baseline['relative_db'], quieter['relative_db'])
    powers[:, 1] = 0
    muted = vector(raw_evidence, powers=powers)
    assert muted['context'] == baseline['context']
    assert muted['shared_frames'] == baseline['shared_frames']
    assert muted['relative_db'][1] < -100
    assert context == original


def test_short_partial_activity_and_all_silence_abstain_without_dropping_roles():
    raw = sources()
    raw['drums'][RATE:] = 0
    result = assess_context(raw, RATE)
    assert result['roles']['drums']['evidence_state'] == 'insufficient'
    assert not result['automatic_balance_allowed']
    result = assess_context({r: np.zeros_like(x) for r, x in raw.items()}, RATE)
    assert result['retained_roles'] == list(raw)
    assert not result['automatic_balance_allowed']
    json.dumps(result, allow_nan=False)


def test_duplicate_stereo_and_invalid_input_are_handled():
    raw = {r: np.column_stack([x, -x]) for r, x in sources().items()}
    raw['drums'] = raw['vocals'].copy()
    result = assess_context(raw, RATE)
    assert result['signal_similarity_clues']
    assert not result['automatic_balance_allowed']
    with pytest.raises(ValueError):
        assess_context({'vocals': np.array([float('nan')])}, RATE)


def test_mixed_mono_stereo_matches_duplicated_mono_energy_without_cancellation():
    raw = sources()
    raw['drums'] = np.column_stack([raw['vocals'], raw['vocals']])
    raw['bass'] = np.column_stack([raw['bass'], -raw['bass']])
    result = assess_context(raw, RATE)
    expanded = {r: np.column_stack([x, x]) if x.ndim == 1 else x for r, x in raw.items()}
    expected = frame_evidence(expanded, RATE)
    actual = result['raw_evidence']
    assert np.allclose(actual['powers'], expected['powers'], rtol=1e-14, atol=1e-18)
    assert {k: v for k, v in actual.items() if k != 'powers'} == {
        k: v for k, v in expected.items() if k != 'powers'}
    assert result['roles']['drums']['similar_signals'] == ['vocals']
    assert result['roles']['vocals']['similar_signals'] == ['drums']
    assert result['roles']['bass']['mean_level_dbfs'] > -30
    assert not result['automatic_balance_allowed']


def test_mismatched_lengths_are_rejected_even_with_same_frame_count():
    raw = sources()
    raw['drums'] = raw['drums'][:-1]
    with pytest.raises(ValueError, match='synchronized'):
        assess_context(raw, RATE)
