"""Selection objective, content isolation and reference-free feature contracts."""
import copy

import numpy as np
import pytest

from wcfi_tools.soundcheck.preference_v2 import (
    content_group,
    evaluate,
    fit,
    no_reference_alerts,
    pairs_for,
    predict,
    selection_regret,
    tune,
)
from wcfi_tools.soundcheck.preference_v2_features import ALL_NAMES, extract_native


def fixture():
    rows, x = [], []
    for group in range(4):
        for i in range(3):
            rows.append({'dataset': 'MED', 'group': f'song{group}', 'trial': f'song{group}',
                         'name': f'mix{i}', 'id': f'{group}-{i}',
                         'ratings': {f'listener{r}_unknown': .15+i*.3 for r in range(10)}})
            x.append([float(i), float(group)] + [0.]*68)
    return rows, np.array(x)


def test_repeated_sources_group_across_methods_and_film_remixes():
    assert content_group('LP_AmateurOnPurpose') == content_group('TM_AmateurOnPurpose')
    assert content_group('DE_Meridian_remix1_LD3') == content_group('DE_Meridian_remix2_LD6')
    assert content_group('DE_female_speech_music_1_LD0') == content_group('DE_female_speech_music_3_LD3')
    assert content_group('TM_02_violin') == content_group('UN_21_violin')
    assert content_group('LP_11_guitar') != content_group('LP_23_jazz')


def test_pairs_stay_within_trial_even_when_source_content_repeats():
    rows, _ = fixture()
    for i, row in enumerate(rows):
        row['group'] = 'shared_song'
        row['trial'] = f'condition_family{i//3}'
    pairs = pairs_for(rows)
    assert len(pairs) == 12
    assert all(rows[p['a']]['trial'] == rows[p['b']]['trial'] for p in pairs)
    assert {p['group'] for p in pairs} == {'shared_song'}


def test_fresh_author_exclusion_keeps_three_listeners_not_self_ratings():
    rows = [{'dataset': 'MED', 'group': 'fresh', 'trial': 'fresh', 'name': name,
             'ratings': {f'mix{i}_unknown': i/5 for i in range(5)}} for name in ('mix0', 'mix1')]
    assert not pairs_for(rows)
    assert len(pairs_for(rows, 3)[0]['differences']) == 3


def test_selection_objective_detects_bad_winner_despite_many_correct_pairs():
    rows, x = fixture()
    pairs = pairs_for(rows)
    scores = x[:, 0].copy()
    scores[0] = 2.1  # Mis-rank the worst above the best.
    regret, _ = selection_regret(scores, pairs, 'song0')
    assert regret == pytest.approx(.6)
    scores[0] = 0
    assert selection_regret(scores, pairs, 'song0')[0] == 0


def test_training_and_tuning_cannot_use_held_out_features_or_labels():
    rows, x = fixture()
    pairs = pairs_for(rows)
    groups = ['song0', 'song1', 'song2']
    config = {'representation': 'extended_concave', 'regularization': .1}
    model = fit(x, rows, pairs, groups, config)
    changed_x, changed_rows = x.copy(), copy.deepcopy(rows)
    changed_x[9:] = 1e8
    for row in changed_rows[9:]:
        row['ratings'] = {k: 1-v for k, v in row['ratings'].items()}
    changed = fit(changed_x, changed_rows, pairs_for(changed_rows), groups, config)
    assert model == changed
    assert tune(x, rows, pairs, groups) == tune(changed_x, changed_rows, pairs_for(changed_rows), groups)
    assert all(i not in model['training_ids'] for i in ['3-0', '3-1', '3-2'])


def test_concave_model_does_not_reward_spectral_extremes_without_bound():
    rows, x = fixture()
    model = fit(x, rows, pairs_for(rows), [f'song{i}' for i in range(4)],
                {'representation': 'extended_concave', 'regularization': .1})
    width = len(model['mean'])
    assert max(model['coefficient'][width:]) <= 0
    assert np.isfinite(predict(model, x)).all()
    assert predict(model, np.full((2, 70), 1e9)).tolist()[0] == predict(model, np.full((2, 70), 1e9)).tolist()[1]


def test_artifact_fit_uses_candidate_features_not_names_or_reference_fields():
    rows, x = fixture()
    for row in rows:
        row['dataset'] = 'ODAQ'
        row['ratings'] = {k: v*100 for k, v in row['ratings'].items()}
    config = {'representation': 'extended_linear', 'regularization': .1}
    groups = [f'song{i}' for i in range(4)]
    model = fit(x, rows, pairs_for(rows), groups, config, artifact=True)
    renamed = copy.deepcopy(rows)
    for row in renamed:
        row['name'] = 'reference' if row['name'] != 'mix2' else 'broken'
        row['reference_audio'] = 'not read'
    assert model == fit(x, renamed, pairs_for(renamed), groups, config, artifact=True)
    assert predict(model, x)[0] < predict(model, x)[2]


def test_evaluation_does_not_hide_bad_forced_choices_by_abstaining():
    rows, x = fixture()
    scores = x[:, 0].copy()
    scores[:3] *= -1
    result = evaluate(rows, scores)
    assert result['selection_failure_trials'] == 1
    assert result['macro_accuracy'] == pytest.approx(.75)


def test_signal_alerts_require_extreme_candidate_only_statistics():
    values = {'mains50_concentration': 0., 'mains60_concentration': 0., 'relative_flat_top_fraction': 0.}
    assert no_reference_alerts(values) == []
    assert 'mains' in no_reference_alerts(values | {'mains60_concentration': 2.5})[0]
    assert 'flat peaks' in no_reference_alerts(values | {'relative_flat_top_fraction': .05})[0]


def test_native_features_repeat_gain_invariance_and_mains_sensitivity():
    rate = 48000
    t = np.arange(rate*2)/rate
    rng = np.random.default_rng(9)
    audio = (.1*rng.normal(size=(len(t), 2)) + .2*np.sin(2*np.pi*530*t)[:, None]).astype('float32')
    features, _ = extract_native(audio, rate)
    repeated, _ = extract_native(audio.copy(), rate)
    quieter, _ = extract_native(audio*.1, rate)
    assert tuple(features) == ALL_NAMES
    assert features == repeated
    assert np.allclose(list(features.values()), list(quieter.values()), atol=1e-4, rtol=1e-4)
    hummed = audio + .2*sum(np.sin(2*np.pi*f*t) for f in (60, 120, 180))[:, None]
    noisy, _ = extract_native(hummed, rate)
    assert noisy['mains60_concentration'] > features['mains60_concentration'] + 1
    with pytest.raises(ValueError):
        extract_native(audio, 24000)
