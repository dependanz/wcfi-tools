"""Benchmark denominators and swapped-miss gates must not flatter abstention."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wcfi_tools.soundcheck.balance_activity_validation import (
    NAMES,
    _frozen_inputs,
    acquisition_status,
    control_result,
    paired_comparison,
    results_for,
    summarize,
)


def assessment(alert=False, score=0., role='vocals'):
    return {'alert': alert, 'score': score,
        'alerts': [{'role': role, 'direction': 'dominant'}] if alert else []}


def record(identifier='one', original=None, eligible=True):
    original = assessment() if original is None else original
    return {'id': identifier, 'dataset': 'fixture', 'eligible': eligible,
        **{name: original.copy() for name in NAMES}, 'variants': []}


def variant(**detections):
    return {'role': 'vocals', 'gain_db': 12., **{name: {
        'unknown': False, 'any_alert': detections.get(name, True),
        'affected_alert': detections.get(name, True),
        'strict_original_win': detections.get(name, True)} for name in NAMES}}


def test_unknown_original_or_candidate_counts_as_missed_control():
    unknown = assessment(None, None)
    bad = assessment(True, 9.)
    for original, candidate in ((unknown, bad), (assessment(), unknown)):
        result = control_result(original, candidate, 'vocals', 12.)
        assert result['unknown']
        assert not result['any_alert']
        assert not result['affected_alert']
        assert not result['strict_original_win']


def test_unknown_is_not_clean_and_single_role_is_separate_coverage():
    unknown = record('unknown', assessment(None, None), eligible=False)
    alert = record('alert', assessment(True, 2.))
    summary = summarize([unknown, alert], 'v3')
    assert summary['original_alert_fraction'] == 1.
    assert summary['assessed_originals'] == summary['unknown_originals'] == 1
    assert summary['eligible_coverage'] == 1.
    unknown['eligible'] = True
    assert summarize([unknown, alert], 'v3')['eligible_coverage'] == .5


def test_swapped_misses_fail_gate_despite_identical_aggregate_recall():
    a, b = record('a'), record('b')
    a['variants'] = [variant(legacy=True, v2=True, v3=False)]
    b['variants'] = [variant(legacy=False, v2=False, v3=True)]
    results = results_for([a, b])
    assert results['all']['legacy']['severe']['any_alert_recall'] == .5
    assert results['all']['v3']['severe']['any_alert_recall'] == .5
    assert not results['gate_pass']
    assert results['paired']['legacy']['severe_changes']['any_alert']['new_misses'] == 1
    assert results['paired']['legacy']['severe_changes']['any_alert']['recoveries'] == 1


def test_clean_context_requires_both_models_to_assess_original_clean():
    clean, bad, unknown = record('clean'), record('bad'), record('unknown')
    bad['legacy'] = assessment(True, 5.)
    unknown['v3'] = assessment(None, None)
    for row in (clean, bad, unknown):
        row['variants'] = [variant()]
    pair = paired_comparison([clean, bad, unknown], 'legacy')
    assert pair['matched_original_clean_ids'] == ['clean']
    assert pair['matched_original_clean_controls']['v3']['controls'] == 1


def test_acquisition_failure_or_missing_planned_recording_cannot_pass():
    failed = {'id': 'planned-one', 'reason': 'download unavailable'}
    result = acquisition_status({'records': [{}], 'failures': [failed]}, {'expected_recording_count': 2})
    assert not result['complete']
    assert result['failures'] == [failed]
    assert result['acquired_recordings'] == result['failed_recordings'] == 1
    assert not acquisition_status({'records': [{}]}, {'expected_recording_count': 2})['complete']
    assert acquisition_status({'records': [{}]}, {'expected_recording_count': 1})['complete']


def frozen_fixture(tmp_path):
    import copy

    import numpy as np
    import soundfile as sf

    from wcfi_tools.audio import balance_activity as v3
    from wcfi_tools.audio import balance_calibration as v2
    from wcfi_tools.audio import source_evaluation as legacy
    from wcfi_tools.soundcheck import balance_activity_validation as runner
    from wcfi_tools.soundcheck.profiles import file_hash

    rng = np.random.default_rng(31)
    raw = {r: rng.normal(0, .02, (32000, 1)).astype('float32') for r in ('vocals', 'other')}
    evidence = v2.frame_evidence(raw, 16000)
    rows = []
    for i in range(50):
        e = copy.deepcopy(evidence)
        e['powers'] = (np.array(e['powers'])*10**(rng.normal(0, 1., 4)/10)).tolist()
        rows.append({'id': str(i), 'artist': str(i), 'evidence': e})
    bank = legacy.SourceBank(raw, {r: r for r in raw}, 16000)
    models = {'legacy': legacy.fit_ranges([bank.measure(raw)]*5, training_groups=list('abcde')),
        'v2': v2.fit(rows[:25], rows[25:]), 'v3': v3.fit(rows[:25], rows[25:])}
    paths = {}
    for name, model in models.items():
        paths[name] = str(tmp_path/(name+'.json'))
        Path(paths[name]).write_text(json.dumps(model), encoding='utf-8')
    sources = {}
    for role, pcm in raw.items():
        path = tmp_path/(role+'.wav')
        sf.write(path, pcm, 16000, subtype='FLOAT')
        sources[role] = {'file': path.name, 'sha256': file_hash(path),
            'sample_rate': 16000, 'frames': 32000, 'channels': 1}
    entries = [{'id': 'both', 'dataset': 'unit', 'sources': sources,
        'title': 'Fixture title', 'recording_protocol': 'Aligned test source', 'reconstruction': {'residual': 0.}},
        {'id': 'single', 'dataset': 'unit', 'sources': {'other': sources['other']}}]
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'records': entries}), encoding='utf-8')
    protocol = tmp_path/'protocol.json'
    protocol.write_text(json.dumps({'controls': list(runner.CONTROLS), 'model_paths': paths,
        'model_sha256': {name: file_hash(Path(path)) for name, path in paths.items()},
        'code_sha256': {module.__file__: file_hash(Path(module.__file__)) for module in (v2, v3, legacy, runner)},
        'manifest_sha256': file_hash(manifest)}), encoding='utf-8')
    return manifest, protocol


def test_saved_source_runner_checks_controls_and_preserves_unknowns(tmp_path):
    from wcfi_tools.soundcheck.balance_activity_validation import run

    manifest, protocol = frozen_fixture(tmp_path)
    result = run(manifest, tmp_path/'run', protocol, progress=lambda _: None)
    assert result['audit']['network_attempts'] == 0
    assert result['audit']['pcm_power_controls_checked'] == 10
    summary = result['results']['all']['v3']
    assert summary['unknown_originals'] == 1
    assert summary['eligible_originals'] == summary['eligible_assessed_originals'] == 1
    assert summary['severe']['controls'] == 4
    assert not result['results']['auto_apply_allowed']
    assert Path(result['report']).is_file()
    prediction = json.loads((tmp_path/'run'/'predictions.json').read_text())['records'][0]
    assert prediction['title'] == 'Fixture title'
    assert prediction['recording_protocol'] == 'Aligned test source'
    assert prediction['reconstruction']['residual'] == 0.
    with pytest.raises(ValueError, match='immutable'):
        run(manifest, tmp_path/'run', protocol)


def test_frozen_manifest_tampering_rejected(tmp_path):
    manifest, protocol = frozen_fixture(tmp_path)
    manifest.write_text('{"records": []}', encoding='utf-8')
    with pytest.raises(ValueError, match='manifest changed'):
        _frozen_inputs(manifest, protocol)
