"""Shared-event balance vectors must describe one coherent source composition."""
from __future__ import annotations

import copy

import numpy as np
import pytest

from wcfi_tools.audio.balance_activity import ROLES, assess, fit, vector


def evidence(powers=None, activity=None, context=ROLES):
    if powers is None:
        powers = np.tile([.001, .003, .002, .004], (12, 1))
    powers = np.asarray(powers, dtype=float)
    if activity is None:
        activity = np.ones_like(powers, dtype=bool)
    return {
        'powers': powers.tolist(), 'activity': np.asarray(activity, dtype=bool).tolist(),
        'present': list(context), 'frames': len(powers), 'sample_rate': 16000,
    }


def shares(report):
    ratios = np.asarray(report['relative_db'])
    return 1 / (1 + 10 ** (-ratios / 10))


def training():
    rng = np.random.default_rng(85)
    rows = []
    for i in range(50):
        powers = np.tile(10 ** (rng.normal(-25, 1.5, 4) / 10), (12, 1))
        rows.append({'id': str(i), 'artist': 'artist-' + str(i), 'evidence': evidence(powers)})
    return rows[:25], rows[25:]


def disjoint_evidence():
    activity = np.zeros((16, 4), dtype=bool)
    for i in range(4):
        activity[4 * i:4 * (i + 1), i] = True
    return evidence(np.ones((16, 4)), activity)


def test_geometric_composition_uses_one_population_and_closes_for_four_roles():
    powers = np.asarray([[1, 2, 9, 4], [4, 1, 2, 8], [10, 6, 1, 1], [2, 10, 4, 3]], dtype=float)
    powers = np.tile(powers, (3, 1))
    actual = vector(evidence(powers))
    geometric = np.exp(np.mean(np.log(powers), axis=0))
    expected = geometric / geometric.sum()
    assert actual['status'] == 'supported'
    assert actual['shared_frames'] == 12
    assert actual['shared_seconds'] == 3.
    assert np.allclose(shares(actual), expected, atol=1e-12)
    assert np.isclose(shares(actual).sum(), 1., atol=1e-12)


def test_two_role_ratios_are_reciprocal_despite_different_solo_activity():
    powers = np.tile([.001, .002, .004, .01], (12, 1))
    activity = np.zeros_like(powers, dtype=bool)
    activity[:8, 0] = True
    activity[4:, 3] = True
    # Only frames 4:8 are co-active. Solo passages are not the same population.
    powers[:4, 0] *= 1e6
    powers[8:, 3] *= 1e6
    report = vector(evidence(powers, activity, ('vocals', 'other')))
    assert report['status'] == 'supported'
    assert report['shared_frames'] == 4
    assert report['shared_seconds'] == 1.
    assert np.allclose(report['relative_db'], [-10., 10.], atol=1e-12)
    assert np.isclose(sum(report['relative_db']), 0., atol=1e-12)


def test_noncoactive_sources_are_unknown_instead_of_clean_or_silently_union_pooled():
    report = vector(disjoint_evidence())
    assert report['status'] == 'insufficient_coactivity'
    assert report['relative_db'] is None
    assert report['shared_frames'] == 0
    model = {'kind': 'wcfi_joint_source_balance_v3', 'contexts': {}}
    result = assess(report, model)
    assert result['alert'] is None
    assert result['score'] is None
    assert not result['auto_apply_allowed']


def test_minimum_shared_duration_is_enforced_even_if_each_role_is_present():
    activity = np.ones((12, 4), dtype=bool)
    activity[3:, 0] = False
    result = vector(evidence(activity=activity))
    assert result['status'] == 'insufficient_coactivity'
    assert result['shared_frames'] == 3
    assert result['shared_seconds'] == .75


def test_fixed_raw_support_candidate_mute_and_common_gain_invariance():
    original = evidence()
    candidate = np.asarray(original['powers'])
    candidate[:, 0] = 0.
    muted = vector(original, powers=candidate)
    quiet = vector(original, powers=candidate * 1e-8)
    assert muted['context'] == list(ROLES)
    assert muted['shared_frames'] == vector(original)['shared_frames']
    assert muted['relative_db'][0] < -60
    assert np.allclose(muted['relative_db'], quiet['relative_db'], atol=1e-10)
    assert np.isclose(shares(muted).sum(), 1., atol=1e-12)
    # Only the frozen support, not raw source ratios, may influence prediction.
    different_raw = copy.deepcopy(original)
    different_raw['powers'] = (np.asarray(original['powers']) * [100., .01, 3., 2.]).tolist()
    assert vector(different_raw, powers=candidate) == muted


def test_frame_permutation_and_unselected_frames_do_not_change_features():
    rng = np.random.default_rng(53)
    powers = np.exp(rng.normal(-7, 1, (12, 4)))
    activity = np.ones((12, 4), dtype=bool)
    activity[8:, 0] = False
    baseline = vector(evidence(powers, activity))
    order = rng.permutation(12)
    permuted = vector(evidence(powers[order], activity[order]))
    assert np.allclose(baseline['relative_db'], permuted['relative_db'], atol=1e-12)
    powers[8:] *= 1e10
    outside = vector(evidence(powers, activity))
    assert np.allclose(baseline['relative_db'], outside['relative_db'], atol=1e-12)


@pytest.mark.parametrize('geometry', ['axis', 'covariance', 'neighbors'])
def test_fit_drops_unsupported_events_without_using_them_for_calibration(geometry):
    train, calibration = training()
    train[0]['evidence'] = disjoint_evidence()
    calibration[0]['evidence'] = disjoint_evidence()
    model = fit(train, calibration, geometry=geometry, alpha=.25)
    assert model['kind'] == 'wcfi_joint_source_balance_v3'
    entry = model['contexts']['|'.join(ROLES)]
    assert train[0]['id'] not in entry['training_ids']
    assert calibration[0]['id'] not in entry['calibration_ids']
    assert len(entry['training_ids']) == 24
    assert len(entry['calibration_ids']) == 24
    raw = evidence()
    before = assess(vector(raw), model)
    candidate = np.asarray(raw['powers'])
    candidate[:, 0] *= 100
    after = assess(vector(raw, powers=candidate), model)
    assert after['score'] > before['score']
    assert after['alert']
    assert not after['auto_apply_allowed']


def test_fit_requires_twenty_supported_songs_not_just_twenty_input_rows():
    train, calibration = training()
    for row in train[:6]:
        row['evidence'] = disjoint_evidence()
    with pytest.raises(ValueError):
        fit(train, calibration)


def test_song_and_artist_leakage_remain_rejected():
    train, calibration = training()
    with pytest.raises(ValueError, match='Song leakage'):
        fit(train, calibration + [train[0]])
    calibration[0]['artist'] = train[0]['artist']
    with pytest.raises(ValueError, match='Artist leakage'):
        fit(train, calibration)


def test_invalid_candidate_or_support_arrays_are_rejected():
    raw = evidence()
    for invalid in (np.ones((11, 4)), np.full((12, 4), np.nan), -np.ones((12, 4))):
        with pytest.raises(ValueError):
            vector(raw, powers=invalid)
    raw['activity'] = [[True] * 4] * 11
    with pytest.raises(ValueError):
        vector(raw)


def test_all_silent_candidates_are_unknown_without_fabricating_equal_balance():
    raw = evidence()
    report = vector(raw, powers=np.zeros((12, 4)))
    assert report['status'] == 'silent_candidate_frames'
    assert report['context'] == list(ROLES)
    assert report['shared_frames'] == 12
    assert report['relative_db'] is None
    assert 'composition' not in report
    result = assess(report, {'kind': 'wcfi_joint_source_balance_v3', 'contexts': {}})
    assert result['alert'] is None
    assert result['score'] is None
    assert not result['auto_apply_allowed']


def test_supported_activity_with_uncalibrated_context_still_returns_unknown():
    report = vector(evidence(context=('vocals', 'other')))
    assert report['status'] == 'supported'
    result = assess(report, {'kind': 'wcfi_joint_source_balance_v3', 'contexts': {}})
    assert result['status'] == 'uncalibrated_context'
    assert result['alert'] is None
    assert result['score'] is None
    assert not result['auto_apply_allowed']
    with pytest.raises(ValueError, match='v3 model'):
        assess(report, {'kind': 'wcfi_joint_source_balance_v2', 'contexts': {}})


@pytest.mark.parametrize('silent', [False, True])
def test_inspect_sources_routes_v3_model_and_preserves_raw_support(tmp_path, silent):
    import json

    import soundfile as sf

    from wcfi_tools.soundcheck.source_manifest import inspect

    train, calibration = training()
    model = fit(train, calibration)
    model_path = tmp_path / 'model.json'
    model_path.write_text(json.dumps(model), encoding='utf-8')
    rng = np.random.default_rng(914)
    vocal = rng.normal(0, .02, (32000, 2)).astype('float32')
    keys = rng.normal(0, .02, (32000, 2)).astype('float32')
    for name, audio in [('vocal', vocal), ('keys', keys)]:
        sf.write(tmp_path / (name + '-raw.wav'), audio, 16000, subtype='FLOAT')
        candidate = np.zeros_like(audio) if silent else audio
        sf.write(tmp_path / (name + '-candidate.wav'), candidate, 16000, subtype='FLOAT')
    master = np.zeros_like(vocal) if silent else vocal + keys
    sf.write(tmp_path / 'master.wav', master, 16000, subtype='FLOAT')
    specification = {
        'kind': 'wcfi_source_contributions_v1', 'stage': 'master_pre_fx',
        'alignment_verified': True, 'duration_seconds': 2, 'master_pre': 'master.wav',
        'sources': [
            {'id': 'RED', 'role': 'vocals', 'raw': 'vocal-raw.wav', 'contribution': 'vocal-candidate.wav'},
            {'id': 'KEYS', 'role': 'keys', 'raw': 'keys-raw.wav', 'contribution': 'keys-candidate.wav'},
        ],
    }
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps(specification), encoding='utf-8')
    result = inspect(manifest, model=model_path)
    assessment = result['joint_balance_assessment']
    assert result['routing_reconstruction_checked']
    assert result['joint_evaluation_role_mapping']['keys'] == 'other'
    assert assessment['evidence']['context'] == ['vocals', 'other']
    assert assessment['evidence']['shared_seconds'] == 2.
    assert not assessment['auto_apply_allowed']
    if silent:
        assert assessment['status'] == 'silent_candidate_frames'
        assert assessment['alert'] is None
        assert assessment['score'] is None
    else:
        assert assessment['evidence']['status'] == 'supported'
        assert assessment['alert'] is not None
        assert np.isclose(shares(assessment['evidence']).sum(), 1., atol=1e-12)
