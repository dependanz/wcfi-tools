"""Contract tests for content-held-out preference training and stereo features."""
import copy

import numpy as np
import pytest

from wcfi_tools.soundcheck.mix_preference import (
    fit_ranker,
    listener_differences,
    make_pairs,
    nested_predictions,
    predict,
    score_predictions,
    select_regularization,
)
from wcfi_tools.soundcheck.preference_features import (
    ARTIFACT_NAMES,
    FEATURE_NAMES,
    artifact_errors,
    extract,
)


def signal():
    rate = 24000
    t = np.arange(rate*2)/rate
    envelope = .3+.2*np.sin(2*np.pi*3*t)
    left = envelope*(np.sin(2*np.pi*140*t)+.2*np.sin(2*np.pi*3000*t))
    right = envelope*(np.sin(2*np.pi*140*t+.3)+.15*np.sin(2*np.pi*3100*t))
    return np.column_stack((left, right)).astype('float32'), rate


def fixture():
    records, x = [], []
    for group in range(5):
        for version in range(3):
            records.append({'dataset': 'MED', 'group': f'song{group}', 'name': f'mix{version}',
                            'id': f'{group}-{version}',
                            'ratings': {f'listener{i}_unknown': .1+version*.3 for i in range(10)}})
            x.append([float(version), float(group)])
    return records, np.array(x), make_pairs(records)


def test_preference_features_repeat_and_global_gain_invariance():
    audio, rate = signal()
    original, health = extract(audio, rate)
    repeat, _ = extract(audio.copy(), rate)
    quieter, health_quieter = extract(audio*.1, rate)
    assert tuple(original) == FEATURE_NAMES
    assert original == repeat
    assert np.allclose(list(original.values()), list(quieter.values()), rtol=3e-5, atol=3e-5)
    assert health['rms_dbfs']-health_quieter['rms_dbfs'] == pytest.approx(20, abs=1e-5)


def test_stereo_changes_are_visible_even_with_identical_mono_sum():
    audio, rate = signal()
    mid = audio.mean(axis=1)
    mono = np.column_stack((mid, mid))
    original, _ = extract(audio, rate)
    narrowed, _ = extract(mono, rate)
    assert np.allclose(audio.mean(axis=1), mono.mean(axis=1))
    assert original['side_energy_fraction'] > narrowed['side_energy_fraction']


@pytest.mark.parametrize('bad', ['silence', 'nan', 'too_long'])
def test_invalid_audio_rejected(bad):
    audio, rate = signal()
    if bad == 'silence':
        audio[:] = 0
    elif bad == 'nan':
        audio[0, 0] = np.nan
    else:
        rate = 100
    with pytest.raises(ValueError):
        extract(audio, rate)


def test_artifact_identity_gain_and_filtered_audio():
    from scipy.signal import butter, sosfilt

    audio, rate = signal()
    same = artifact_errors(audio, audio.copy(), rate)
    gain_only = artifact_errors(audio*.1, audio, rate)
    filtered = sosfilt(butter(4, 300, fs=rate, output='sos'), audio, axis=0)
    worse = artifact_errors(filtered, audio, rate)
    assert tuple(same) == ARTIFACT_NAMES
    assert max(same.values()) < 1e-6
    assert max(gain_only.values()) < 1e-5
    assert worse['log_spectral_error'] > same['log_spectral_error'] + .001
    with pytest.raises(ValueError):
        artifact_errors(audio[:-1], audio, rate)


def test_unknown_and_numeric_author_ids_excluded():
    a = {'dataset': 'MED', 'name': 'PXL-S1', 'ratings': {'PXL-S1_unknown': 1, 'DU-O_2014': .4, 'listener': .7}}
    b = {'dataset': 'MED', 'name': 'DU-O', 'ratings': {'PXL-S1_unknown': 0, 'DU-O_2014': .6, 'listener': .2}}
    assert listener_differences(a, b).tolist() == pytest.approx([.5])


def test_scaler_training_and_labels_exclude_held_out_song():
    records, x, pairs = fixture()
    groups = ['song0', 'song1', 'song2', 'song3']
    model = fit_ranker(x, records, pairs, groups, .1)
    changed = x.copy()
    changed[12:] = 1e9
    altered_records = copy.deepcopy(records)
    for row in altered_records[12:]:
        row['ratings'] = {key: 1-value for key, value in row['ratings'].items()}
    other = fit_ranker(changed, altered_records, make_pairs(altered_records), groups, .1)
    assert model == other
    assert set(model['training_ids']).isdisjoint({r['id'] for r in records[12:]})
    regularization, search = select_regularization(x, records, pairs, groups)
    regularization2, search2 = select_regularization(changed, altered_records, make_pairs(altered_records), groups)
    assert (regularization, search) == (regularization2, search2)
    assert all('song4' not in s['train'] and s['test'] != 'song4' for c in search for s in c['folds'])


def test_nested_holdout_predictions_and_frozen_test_songs():
    records, x, pairs = fixture()
    scores, folds, model = nested_predictions(x, records, pairs, progress=lambda _: None)
    assert np.isfinite(scores).all()
    assert model['training_groups'] == [f'song{i}' for i in range(5)]
    for fold in folds:
        assert set(fold['test_ids']).isdisjoint(fold['model']['training_ids'])
        assert fold['test_group'] not in fold['model']['training_groups']
        assert fold['inner_search']
    assert all(scores[i] < scores[i+1] for i in range(0, 15, 3))


def test_pair_scores_are_antisymmetric_and_identical_ties():
    records, x, pairs = fixture()
    model = fit_ranker(x, records, pairs, [f'song{i}' for i in range(5)], .1)
    scores = predict(model, x)
    assert scores[0]-scores[1] == -(scores[1]-scores[0])
    assert predict(model, x[[0, 0]]).tolist()[0] == predict(model, x[[0, 0]]).tolist()[1]


def test_artifact_ranker_cannot_reward_larger_errors():
    records, x, _pairs = fixture()
    for record in records:
        record['dataset'] = 'ODAQ'
        record['ratings'] = {k: 1-v for k, v in record['ratings'].items()}
    model = fit_ranker(x, records, make_pairs(records), [f'song{i}' for i in range(5)], .1, artifact=True)
    assert min(model['coefficient']) >= 0
    assert predict(model, [[0, 0]])[0] >= predict(model, [[10, 10]])[0]


def test_reporting_averages_contents_instead_of_generated_pairs():
    records, x, pairs = fixture()
    scores = x[:, 0].copy()
    scores[0:3] *= -1
    result = score_predictions(records, pairs, scores, [0]*len(records))
    assert result['macro_accuracy'] == pytest.approx(.8)
    assert result['groups_with_decisive_pairs'] == 5
    assert result['macro_rms_accuracy'] == .5
