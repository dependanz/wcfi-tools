"""Saved-source inspection keeps model diagnostics separate from RAW uncertainty."""
import json

import numpy as np
import soundfile as sf

from wcfi_tools.audio import balance_activity
from wcfi_tools.soundcheck.source_manifest import inspect


def test_uncertain_raw_withholds_advice_without_hiding_muted_candidate(tmp_path, monkeypatch):
    rate = 16000
    t = np.arange(4 * rate) / rate
    vocal = .1 * (1 + .8 * np.sin(2 * np.pi * .7 * t)) * np.sin(2 * np.pi * 310 * t)
    drums = .0015 * (1 + .8 * np.sin(2 * np.pi * 1.3 * t)) * np.sin(2 * np.pi * 190 * t)
    for name, value in [('vocal', vocal), ('drums', drums)]:
        sf.write(tmp_path / f'{name}-raw.wav', value, rate, subtype='FLOAT')
        sf.write(tmp_path / f'{name}-candidate.wav', value, rate, subtype='FLOAT')
    model = tmp_path / 'model.json'
    model.write_text(json.dumps({'kind': 'wcfi_joint_source_balance_v3'}), encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({
        'kind': 'wcfi_source_contributions_v1', 'stage': 'master_pre_fx',
        'alignment_verified': True, 'duration_seconds': 4,
        'sources': [{'id': name, 'role': role, 'raw': f'{name}-raw.wav',
                     'contribution': f'{name}-candidate.wav'}
                    for name, role in [('vocal', 'vocals'), ('drums', 'drums')]],
    }), encoding='utf-8')
    monkeypatch.setattr(balance_activity, 'assess', lambda evidence, _model:
        {'status': 'diagnostic_fixture', 'score': 7., 'evidence': evidence, 'auto_apply_allowed': False})
    baseline = inspect(manifest, model=model)
    sf.write(tmp_path / 'drums-candidate.wav', np.zeros_like(drums), rate, subtype='FLOAT')
    changed = inspect(manifest, model=model)
    for result in (baseline, changed):
        assert result['source_context_qualification']['kind'] == 'wcfi_raw_balance_context_v2'
        assert result['source_prior_recommendation_status'] == 'withheld_uncertain_context'
        assert result['source_context_qualification']['retained_roles'] == ['vocals', 'drums']
        assert result['source_context_qualification']['roles']['drums']['evidence_state'] == 'ambiguous'
        assert result['joint_balance_assessment']['score'] == 7.
        assert result['joint_balance_assessment']['evidence']['context'] == ['vocals', 'drums']
        assert not result['auto_apply_allowed']
    assert baseline['source_context_qualification'] == changed['source_context_qualification']
    assert changed['joint_balance_assessment']['evidence']['relative_db'][1] < -100


def test_sustained_keys_route_through_v2_without_altering_model_diagnostics(tmp_path, monkeypatch):
    rate = 16000
    t = np.arange(4 * rate) / rate
    vocal = .1 * (1 + .8 * np.sin(2 * np.pi * .7 * t)) * np.sin(2 * np.pi * 310 * t)
    keys = .04 * sum(np.sin(2 * np.pi * f * t) for f in (220., 277.18, 329.63))
    for name, value in [('vocal', vocal), ('keys', keys)]:
        sf.write(tmp_path / f'{name}.wav', value, rate, subtype='FLOAT')
    model = tmp_path / 'model.json'
    model.write_text(json.dumps({'kind': 'wcfi_joint_source_balance_v3'}), encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({
        'kind': 'wcfi_source_contributions_v1', 'stage': 'master_pre_fx',
        'alignment_verified': True, 'duration_seconds': 4,
        'sources': [{'id': name, 'role': role, 'raw': f'{name}.wav', 'contribution': f'{name}.wav'}
                    for name, role in [('vocal', 'vocals'), ('keys', 'keys')]],
    }), encoding='utf-8')
    monkeypatch.setattr(balance_activity, 'assess', lambda evidence, _model:
        {'status': 'diagnostic_fixture', 'score': 7., 'evidence': evidence, 'auto_apply_allowed': False})
    result = inspect(manifest, model=model)
    assert result['source_prior_recommendation_status'] == 'context_has_no_observed_objection'
    keys_context = result['source_context_qualification']['roles']['other']
    assert keys_context['evidence_state'] == 'supported'
    assert keys_context['sustained_signal_evidence']['sustained_structure_supported']
    assert result['joint_balance_assessment']['score'] == 7.
    assert not result['auto_apply_allowed']
