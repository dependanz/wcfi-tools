"""Synthetic ranking checks, not evidence that utility is perceptual quality."""
import copy

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from wcfi_tools.audio.mix_reference import POLICY as REFERENCE_POLICY
from wcfi_tools.audio.mix_reference import compare, describe
from wcfi_tools.audio.mix_reference_rank import POLICY, rank_candidates


def _rows(**candidate_units):
    baseline = {'kind': REFERENCE_POLICY['kind'], 'status': 'supported',
                **{key: 10*threshold for key, threshold in POLICY['dimensions'].items()}}
    rows = [{'id': 'unity', 'distances': baseline}]
    for identifier, units in candidate_units.items():
        values = {key: baseline[key]-unit*threshold for (key, threshold), unit
                  in zip(POLICY['dimensions'].items(), units, strict=True)}
        rows.append({'id': identifier, 'distances': baseline | values})
    return rows


def test_robust_winner_between_incomparable_options():
    result = rank_candidates(_rows(balanced=[2]*6, spectral=[3, 0, 0, 0, 0, 0]))
    assert result['frontier_ids'] == ['balanced', 'spectral']
    assert result['selected_id'] == 'balanced'
    assert result['minimum_winner_margin'] == pytest.approx(.5)
    assert result['auto_apply_allowed'] is False
    for row in result['weight_results'].values():
        assert row['winner_id'] == 'balanced'
        assert any(r['id'] == 'unity' and r['utility'] == 0 for r in row['ranking'])


def test_modest_weight_changes_can_force_abstention():
    result = rank_candidates(_rows(spectrum=[3, 0, 0, 0, 0, 0], dynamics=[0, 3, 3, 3, 0, 0]))
    assert result['status'] == 'abstain'
    assert result['selected_id'] is None
    assert result['weight_results']['spectrum_focus']['winner_id'] == 'spectrum'
    assert result['weight_results']['dynamics_focus']['winner_id'] == 'dynamics'
    assert [r['rank'] for r in result['ordered_shortlist']] == [1, 1]


def test_dominated_option_does_not_displace_guarded_winner():
    result = rank_candidates(_rows(better=[2]*6, dominated=[1]*6))
    assert result['selected_id'] == 'better'
    assert result['frontier_ids'] == ['better']
    assert result['eligible_ids'] == ['better', 'dominated']
    assert result['minimum_winner_margin'] == pytest.approx(1.)
    assert result['ordered_shortlist'][1]['id'] == 'dominated'
    assert result['ordered_shortlist'][1]['is_pareto_frontier'] is False


def test_spectral_win_cannot_rescue_catastrophic_regression():
    result = rank_candidates(_rows(disaster=[9, 0, 0, 0, -.51, 0], balanced=[2]*6))
    assert result['rejected_ids'] == ['disaster']
    assert result['selected_id'] == 'balanced'
    assert result['guard_result']['evidence']['disaster']['material_regressions'] == ['side_fraction']


def test_caps_and_negative_effects_remain_explicit():
    result = rank_candidates(_rows(balanced=[2]*6, large_spectrum=[9, 0, 0, 0, 0, 0]))
    scores = {r['id']: r for r in result['ordered_shortlist']}
    assert scores['large_spectrum']['normalized_improvements']['spectrum_db'] == pytest.approx(9)
    assert scores['large_spectrum']['capped_improvements']['spectrum_db'] == 3.
    assert result['selected_id'] == 'balanced'
    adverse = rank_candidates(_rows(spectral_cost=[1, -.5, -.5, -.5, -.5, -.5]))
    assert adverse['status'] == 'abstain'
    assert adverse['ordered_shortlist'][0]['family_utilities']['dynamics'] == pytest.approx(-.5)
    assert adverse['ordered_shortlist'][0]['family_utilities']['stereo'] == pytest.approx(-.5)
    assert any(r['winner_id'] == 'unity' for r in adverse['weight_results'].values())


def test_single_eligible_candidate_still_needs_margin_over_baseline():
    result = rank_candidates(_rows(weak=[0, 1, 0, 0, 0, 0]))
    assert result['frontier_ids'] == ['weak']
    assert result['reason'] == 'insufficient_robust_margin'
    assert result['minimum_winner_margin'] == pytest.approx(1/12)


def test_identical_options_and_small_numeric_differences_cannot_break_ties():
    tied = rank_candidates(_rows(a=[2]*6, b=[2]*6))
    assert tied['reason'] == 'tied_weighted_ranking'
    assert [r['rank'] for r in tied['ordered_shortlist']] == [1, 1]
    near = rank_candidates(_rows(a=[2.001, 2, 2, 2, 2, 2], b=[2, 2.001, 2, 2, 2, 2]))
    assert near['status'] == 'abstain'
    assert near['selected_id'] is None


def test_row_key_id_permutations_do_not_change_selection():
    rows = _rows(a=[2]*6, b=[3, 0, 0, 0, 0, 0])
    original = rank_candidates(rows)
    for row in rows:
        row['distances'] = dict(reversed(list(row['distances'].items())))
    assert rank_candidates(list(reversed(rows))) == original
    renamed = copy.deepcopy(rows)
    ids = {'unity': 'different-baseline', 'a': 'z', 'b': 'a'}
    for row in renamed:
        row['id'] = ids[row['id']]
    result = rank_candidates(renamed, baseline_id='different-baseline')
    assert result['selected_id'] == 'z'
    assert result['minimum_winner_margin'] == original['minimum_winner_margin']


def test_unknowns_fail_closed_but_preserve_supported_shortlist():
    rows = _rows(known=[2]*6)
    rows.append({'id': 'missing', 'distances': {'status': 'unsupported'}})
    result = rank_candidates(rows)
    assert result['reason'] == 'unsupported_candidates'
    assert result['unknown_ids'] == ['missing']
    assert result['ordered_shortlist'][0]['id'] == 'known'
    assert result['selected_id'] is None
    rows[0]['distances'] = {'status': 'unsupported'}
    assert rank_candidates(rows)['reason'] == 'unsupported_baseline'


def test_stereo_measurements_share_one_family_vote():
    result = rank_candidates(_rows(stereo=[0, 0, 0, 0, 2, 2]))
    candidate = result['ordered_shortlist'][0]
    assert candidate['family_utilities'] == pytest.approx(
        {'spectrum': 0., 'dynamics': 0., 'stereo': 2.})
    assert candidate['weight_utilities']['equal'] == pytest.approx(2/3)


def test_rank_with_actual_descriptors_is_invariant_to_common_gain():
    rate = 24000
    rng = np.random.default_rng(731)
    x = rng.normal(size=(rate*3, 2))
    filtered = sosfilt(butter(4, 1500, fs=rate, output='sos'), x, axis=0)
    outputs = []
    for gain in (1., .002):
        reference = describe(x*gain, rate)
        rows = [{'id': identifier, 'distances': compare(describe(audio*gain, rate), reference)}
                for identifier, audio in [('unity', filtered), ('candidate', x)]]
        outputs.append(rank_candidates(rows))
    assert outputs[0]['selected_id'] == outputs[1]['selected_id'] == 'candidate'
    for scenario in POLICY['weights']:
        assert outputs[0]['weight_results'][scenario]['margin'] == pytest.approx(
            outputs[1]['weight_results'][scenario]['margin'], abs=1e-8)


def test_policy_weight_vectors_and_margin_are_explicit():
    assert POLICY['positive_improvement_cap'] == 3.
    assert POLICY['minimum_margin'] == .1
    for weights in POLICY['weights'].values():
        assert sum(weights.values()) == pytest.approx(1.)
        assert set(weights) == set(POLICY['families'])


def test_adding_rejected_candidate_cannot_change_admissible_ranking():
    rows = _rows(balanced=[2]*6, spectral=[3, 0, 0, 0, 0, 0])
    original = rank_candidates(rows)
    rows.append(_rows(rejected=[3, 0, 0, 0, -.6, 0])[-1])
    changed = rank_candidates(rows)
    for key in ('status', 'selected_id', 'ordered_shortlist', 'weight_results',
                'minimum_winner_margin'):
        assert original[key] == changed[key]


def test_tiny_dominated_difference_cannot_create_artificially_large_margin():
    result = rank_candidates(_rows(a=[2]*6, b=[1.999999, 2, 2, 2, 2, 2]))
    assert result['frontier_ids'] == ['a']
    assert result['reason'] == 'insufficient_robust_margin'
    assert result['minimum_winner_margin'] < .000001
    assert result['selected_id'] is None
    assert {row['id'] for row in result['ordered_shortlist']} == {'a', 'b'}


def test_saturation_tie_with_dominated_candidate_still_abstains():
    result = rank_candidates(_rows(a=[4]*6, b=[3]*6))
    assert result['frontier_ids'] == ['a']
    assert result['reason'] == 'tied_weighted_ranking'
    assert result['selected_id'] is None
    assert [row['rank'] for row in result['ordered_shortlist']] == [1, 1]


def test_weight_sensitive_ranking_when_every_scenario_has_unique_winner():
    result = rank_candidates(_rows(a=[3, 0, 0, 0, 0, 0], b=[0, 2.7, 2.7, 2.7, 0, 0]))
    assert result['reason'] == 'weight_sensitive_ranking'
    assert all(row['winner_id'] is not None for row in result['weight_results'].values())
    assert result['selected_id'] is None
