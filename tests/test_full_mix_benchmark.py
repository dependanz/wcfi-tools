"""Blindness, invariance, musical-send boundaries and truthful failure reporting."""
import copy
import inspect
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio import full_mix_benchmark as b


def audio():
    rng = np.random.default_rng(34)
    n = 576000
    t = np.arange(n)/48000
    return np.column_stack((.03*np.sin(2*np.pi*220*t)+rng.normal(0, .01, n),
                            .02*np.sin(2*np.pi*440*t)+rng.normal(0, .01, n)))


def test_scalar_gain_does_not_change_style_or_projection():
    a = audio();p = b.SourceProjection({'RED': a, 'KIT': np.roll(a, 111, axis=0)})
    m = b.observations(a);small = b.observations(a*.5)
    assert b.score(m, p.evaluate(a), {'a': m, 'b': m}, b.POLICY)['cost'] == pytest.approx(
        b.score(small, p.evaluate(a*.5), {'a': m, 'b': m}, b.POLICY)['cost'], abs=1e-6)
    assert b.observations(np.column_stack((a[:, 0], -a[:, 0])))['mono_energy_change_db'] < -50


def test_clipping_and_phase_controls_cannot_pass_technical_guards():
    a = audio();projection = b.SourceProjection({'RED': a})
    m = b.observations(a)
    clipped = np.clip(a, -.012, .012)
    out = b.score(b.observations(clipped), projection.evaluate(clipped), {'a': m}, b.POLICY)
    assert not out['eligible'] and 'hard-clipping plateaus' in out['guardrail_failures']
    phase = np.column_stack((a[:, 0], -a[:, 0]))
    out = b.score(b.observations(phase), projection.evaluate(phase), {'a': m}, b.POLICY)
    assert not out['eligible'] and 'mono cancellation' in out['guardrail_failures']


def test_musical_ambience_has_stereo_tail_without_bass_send():
    a = np.zeros((576000, 2));a[48000] = .1
    stems = {name: np.zeros_like(a) for name in b.ROLES};stems['RED'] = a
    wet = b.ambience(stems, .7, -16)
    assert np.max(np.abs(wet[:49000])) < 1e-12
    assert b.power(wet) == pytest.approx(b.power(a)*10**(-16/10), rel=1e-6)
    assert np.max(np.abs(wet[:, 0]-wet[:, 1])) > 0
    # Adding bass alters the prescribed wet/direct normalization but cannot
    # create a bass-only send or a wet waveform when no musical send exists.
    stems = {name: np.zeros_like(a) for name in b.ROLES};stems['BASS'] = a
    np.testing.assert_array_equal(b.ambience(stems, .7, -16), np.zeros_like(a))


def test_opaque_inference_does_not_read_key_and_order_does_not_change_selection():
    source = inspect.getsource(b.run)
    assert 'private-key.json' not in source
    rows = [{'id': 'b', 'evaluation': {'eligible': False, 'cost': 0}},
            {'id': 'c', 'evaluation': {'eligible': True, 'cost': 2}},
            {'id': 'a', 'evaluation': {'eligible': True, 'cost': 1}}]
    assert [r['id'] for r in b.rank(rows)] == ['a', 'c', 'b']
    assert b.rank(rows) == b.rank(rows[::-1])


def test_source_role_silence_and_backing_bleed_are_not_boosted_unbounded():
    a = audio();raw = {r: a for r in b.ROLES}
    raw['YELLOW'] = a*1e-6
    levels = {r: {'integrated_lufs': -30} for r in b.ROLES}
    levels['YELLOW'] = {'integrated_lufs': None};levels['GREEN'] = {'integrated_lufs': -49}
    balanced, shaped, plan = b.source_versions(raw, levels)
    assert plan['YELLOW']['gain_db'] is None
    assert plan['GREEN']['gain_db'] == 6
    assert not balanced['YELLOW'].any() and not shaped['YELLOW'].any()
    assert plan['KIT']['compression']['maximum_reduction_db'] <= 2


def test_package_rejects_claim_of_validated_quality_before_reading_audio(tmp_path):
    package = {'kind': 'wcfi_full_mix_benchmark_package', 'schema_version': 1,
               'reports': {'MIX': {'kind': 'wcfi_full_mix_benchmark', 'professional_quality_judgment_validated': True}}}
    with pytest.raises(ValueError, match='scope'):
        b.validate_package(tmp_path, package, json.dumps(package).encode())


def test_asset_checksums_and_traversal_are_checked_before_scoring(tmp_path):
    (tmp_path/'assets').mkdir();sf.write(tmp_path/'outside.wav', audio(), 48000)
    entry = {'file': '../outside.wav', 'sha256': b.digest(tmp_path/'outside.wav')}
    with pytest.raises(ValueError, match='escaped'):
        b.checked_asset(tmp_path, entry)


def test_frozen_weights_cannot_be_changed_on_an_existing_run(tmp_path):
    policy = copy.deepcopy(b.POLICY);policy['balance_weight'] = 50
    b.save(tmp_path/'policy.json', policy)
    b.save(tmp_path/'blind-inputs.json', {'policy_sha256': b.digest(tmp_path/'policy.json')})
    with pytest.raises(ValueError, match='Frozen'):
        b.run(tmp_path, tmp_path/'run')


@pytest.mark.parametrize('failed_control', [None, 'dominant_acoustic'])
def test_failed_controls_withhold_all_dates_and_passing_controls_never_certify_quality(tmp_path, monkeypatch, failed_control):
    root = tmp_path/'benchmark';root.mkdir();run = tmp_path/'run';run.mkdir()
    rows, entries, labels = [], [], {}
    costs = dict(zip(b.CANDIDATES, (4, 3, 2.5, 2, 1, 1.2, 1.3, 1.4), strict=True))
    for sid in b.SESSIONS:
        for name in (*b.CANDIDATES, 'identical', 'gain_only', *b.DEGRADATIONS):
            identifier = f'{len(rows):024x}'
            cost = costs.get(name, 1 if name in ('identical', 'gain_only') else 2)
            if sid == 'oct11' and name == failed_control:
                cost = .7
            rows.append({'id': identifier, 'session': sid, 'sha256': 'a'*64,
                         'metrics': {}, 'evaluation': {'eligible': True, 'cost': cost,
                             'guardrail_failures': [], 'projection': {'cost': cost}}})
            entries.append({'id': identifier, 'file': f'MIX-fmb_{identifier}.wav', 'sha256': 'a'*64})
            labels[identifier] = {'variant': name, 'label': name, 'candidate': name in b.CANDIDATES}
    b.save(root/'policy.json', b.POLICY)
    b.save(root/'blind-inputs.json', {'items': entries, 'references': {'bethel': {'file': 'b.wav'}, 'maverick': {'file': 'm.wav'}}})
    b.save(root/'private-key.json', {'items': labels, 'plans': {sid: {} for sid in b.SESSIONS},
           'sources': {'sessions': [{'id': sid} for sid in b.SESSIONS], 'references': {}}})
    freeze = {key: b.digest(root/name) for key, name in (
        ('policy_sha256', 'policy.json'), ('inputs_sha256', 'blind-inputs.json'), ('key_sha256', 'private-key.json'))}
    b.save(root/'freeze.json', freeze)
    b.save(run/'blind-results.json', {'policy_sha256': freeze['policy_sha256'], 'inputs_sha256': freeze['inputs_sha256'],
           'rows': rows, 'references': {'bethel': {}, 'maverick': {}}, 'runtime': {},
           'repeats': [{'session': sid, 'exact_repeat': True} for sid in b.SESSIONS], 'order_invariance': True})
    monkeypatch.setattr(b, 'checked_asset', lambda *_: None)
    monkeypatch.setattr(b, 'score', lambda m, p, *_: {'eligible': True, 'cost': p['cost']})
    report = b.summarize(root, run)
    assert report['control_gate_passed'] is (failed_control is None)
    assert len(report['checks']) == 37
    assert report['professional_quality_judgment_validated'] is False
    assert report['processing_applied'] is False
    assert all(s['automatic_recommendation'] == ('experimental_style_candidate' if failed_control is None else 'withheld')
               for s in report['sessions'])
