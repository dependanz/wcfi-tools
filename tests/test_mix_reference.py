"""Synthetic mechanism checks; these do not establish musical preference."""
import math

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from wcfi_tools.audio.mix_reference import POLICY, compare, describe, select_candidates


@pytest.fixture(scope='module')
def audio():
    rate = 24000
    rng = np.random.default_rng(320)
    t = np.arange(rate*5)/rate
    envelope = .15+.85*(.5+.5*np.sin(2*np.pi*.37*t))**2
    shared = np.sin(2*np.pi*140*t)+.5*np.sin(2*np.pi*1800*t)
    left = (shared+.5*rng.normal(size=len(t)))*envelope
    right = (shared+.5*rng.normal(size=len(t)))*envelope
    return np.column_stack((left, right)), rate


def _distance(**values):
    return {'kind': POLICY['kind'], 'status': 'supported',
            **{key: 2. for key in POLICY['improvement']}, **values}


def test_identical_and_gain_invariance(audio):
    x, rate = audio
    reference = describe(x, rate)
    assert reference['status'] == 'supported'
    assert all(math.isfinite(v) for v in reference['spectral_band_db'])
    for gain in (1., .003, -8.):
        distance = compare(describe(x*gain, rate), reference)
        assert distance['status'] == 'supported'
        for key in POLICY['improvement']:
            assert distance[key] == pytest.approx(0., abs=1e-9)


def test_stereo_fold_filter_and_dynamic_damage_have_separate_evidence(audio):
    x, rate = audio
    reference = describe(x, rate)
    mono = np.repeat(x.mean(axis=1, keepdims=True), 2, axis=1)
    folded = compare(describe(mono, rate), reference)
    assert folded['side_fraction'] > .03
    assert folded['lr_correlation'] > .1
    filtered = sosfilt(butter(4, 1500, fs=rate, output='sos'), x, axis=0)
    lowpassed = compare(describe(filtered, rate), reference)
    assert lowpassed['spectrum_db'] > 3
    squashed = compare(describe(np.tanh(7*x), rate), reference)
    assert squashed['crest_db'] > 1
    assert squashed['dynamic_range_db'] > 1
    clipped = compare(describe(np.clip(x, -.2, .2), rate), reference)
    assert clipped['crest_db'] > 1


def test_joint_tradeoffs_cannot_hide_stereo_or_dynamic_regressions():
    rows = [{'id': 'unity', 'distances': _distance()},
            {'id': 'spectrum_only', 'distances': _distance(spectrum_db=.1, side_fraction=2.02)},
            {'id': 'squash', 'distances': _distance(spectrum_db=.1, crest_db=2.3)},
            {'id': 'pumping', 'distances': _distance(spectrum_db=.1, envelope_db=2.2)}]
    result = select_candidates(rows)
    assert result['status'] == 'abstain'
    assert result['reason'] == 'no_joint_improvement'
    assert set(result['rejected_ids']) == {'spectrum_only', 'squash', 'pumping'}
    assert result['evidence']['squash']['material_regressions'] == ['crest_db']


def test_joint_unique_winner_and_ties_are_order_independent():
    baseline = {'id': 'unity', 'distances': _distance()}
    a = {'id': 'a', 'distances': _distance(spectrum_db=1.)}
    b = {'id': 'b', 'distances': _distance(spectrum_db=.5)}
    assert select_candidates([baseline, a, b])['selected_id'] == 'b'
    identical_b = {'id': 'copy_b', 'distances': dict(b['distances'])}
    for rows in ([a, baseline, identical_b, b], [b, identical_b, baseline, a]):
        result = select_candidates(rows)
        assert result['status'] == 'abstain'
        assert result['frontier_ids'] == ['b', 'copy_b']


def test_incomparable_improvements_and_unchanged_abstain():
    baseline = {'id': 'unity', 'distances': _distance()}
    a = {'id': 'a', 'distances': _distance(spectrum_db=1.)}
    b = {'id': 'b', 'distances': _distance(envelope_db=1.)}
    assert select_candidates([baseline, a, b])['reason'] == 'ambiguous_joint_improvements'
    unchanged = {'id': 'copy', 'distances': _distance()}
    assert select_candidates([baseline, unchanged])['reason'] == 'no_joint_improvement'


def test_unsupported_inputs_and_comparisons(audio):
    x, rate = audio
    for bad, bad_rate in [(np.zeros_like(x), rate), (x[:100], rate), (x, 22050),
                          (x, math.nan), (x, True), (x, 24000.5),
                          (np.ones((rate*4, 3)), rate), (['x'], rate),
                          (np.full_like(x, np.nan), rate), (np.ones_like(x), rate),
                          (np.column_stack((x[:, 0], np.zeros(len(x)))), rate)]:
        assert describe(bad, bad_rate)['status'] == 'unsupported'
    supported = describe(x, rate)
    assert describe(x[:, 0], rate)['status'] == 'supported'
    assert compare(supported, describe(x[:-1000], rate))['status'] == 'unsupported'
    assert compare(supported, {})['status'] == 'unsupported'
    corrupted = dict(supported, crest_quantiles_db=[math.inf, 2])
    assert compare(corrupted, supported)['status'] == 'unsupported'
    for key, value in [('duration_seconds', math.nan), ('dynamic_range_db', -1),
                       ('side_energy_fraction', 1.1), ('lr_correlation', -1.1)]:
        assert compare(dict(supported, **{key: value}), supported)['status'] == 'unsupported'


def test_unknown_candidates_and_baseline_never_count_as_clean():
    baseline = {'id': 'unity', 'distances': _distance()}
    unknown = {'id': 'bad', 'distances': {'status': 'unsupported'}}
    result = select_candidates([baseline, unknown])
    assert result['status'] == 'abstain' and result['unknown_ids'] == ['bad']
    assert select_candidates([baseline, unknown], 'bad')['reason'] == 'unsupported_baseline'
    with pytest.raises(ValueError):
        select_candidates([baseline, baseline])
    with pytest.raises(ValueError):
        select_candidates([unknown])


def test_threshold_boundaries_are_explicit():
    baseline = {'id': 'unity', 'distances': _distance()}
    edge = {'id': 'edge', 'distances': _distance(spectrum_db=1.5, envelope_db=2.125)}
    result = select_candidates([baseline, edge])
    assert result['selected_id'] == 'edge'
    edge['distances']['envelope_db'] += .001
    assert select_candidates([baseline, edge])['selected_id'] is None


def test_activity_only_in_discarded_partial_envelope_is_unsupported(audio):
    x, rate = audio
    partial = np.zeros((round(4.5*rate), 2))
    partial[4*rate:] = x[:rate//2]
    result = describe(partial, rate)
    assert result['status'] == 'unsupported'
    assert result['reasons'] == ['no_supported_envelope_energy']


def test_comparison_requires_matching_analysis_rates(audio):
    x, rate = audio
    a = describe(x, rate)
    b = dict(a, sample_rate=48000)
    assert compare(a, b)['reasons'] == ['equal_analysis_sample_rates_required']


def test_numerical_pareto_cycle_abstains_without_denying_eligibility():
    eps = POLICY['numeric_tolerance']
    a = {'id': 'a', 'distances': _distance(spectrum_db=1., envelope_db=1., crest_db=1.)}
    b = {'id': 'b', 'distances': _distance(
        spectrum_db=1.+1.5*eps, envelope_db=1.-.75*eps, crest_db=1.-.75*eps)}
    c = {'id': 'c', 'distances': _distance(
        spectrum_db=1.+.75*eps, envelope_db=1.+.75*eps, crest_db=1.-1.5*eps)}
    result = select_candidates([{'id': 'unity', 'distances': _distance()}, a, b, c])
    assert result['eligible_ids'] == ['a', 'b', 'c']
    assert result['status'] == 'abstain'
    assert result['reason'] == 'ambiguous_numerical_frontier'


def test_rescaling_all_candidate_audio_preserves_eligibility_and_selection(audio):
    x, rate = audio
    candidates = {'base': sosfilt(butter(4, 1500, fs=rate, output='sos'), x, axis=0),
                  'wider': sosfilt(butter(4, 5000, fs=rate, output='sos'), x, axis=0),
                  'reference_copy': x.copy(),
                  'mono': np.repeat(x.mean(axis=1, keepdims=True), 2, axis=1)}

    def choose(gain):
        reference = describe(x * gain, rate)
        result = select_candidates([{'id': name, 'distances': compare(describe(y * gain, rate), reference)}
                                    for name, y in candidates.items()], 'base')
        return {key: result[key] for key in ('status', 'selected_id', 'eligible_ids', 'frontier_ids', 'unknown_ids')}

    assert choose(1.) == choose(.003) == choose(-8.)
