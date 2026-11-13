"""Verified reference assets, honest difference gates and legacy-safe mix reports."""
import copy
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_diagnostic import validate_package
from wcfi_tools.audio.reference_study import describe_mix
from wcfi_tools.audio.review_service import RehearsalReview
from wcfi_tools.audio.updated_reference import AUDIO, IDS, build_package, findings


def signals():
    t = np.arange(576000)/48000
    mid = .06*np.sin(2*np.pi*200*t)+.03*np.sin(2*np.pi*2000*t)
    side = .04*np.sin(2*np.pi*500*t)
    mono = np.repeat(mid[:, None], 2, axis=1)
    wide = np.stack([mid+side, mid-side], axis=1)
    return {'before': mono, 'updated': mono*.9, 'bethel': wide, 'maverick': wide*.8}


def scanner(path, _ffmpeg):
    # Controlled RMS stub, not a BS.1770 implementation.
    a, _ = sf.read(path, always_2d=True)
    return {'integrated_lufs': float(10*np.log10(max(np.mean(a*a), 1e-20))),
            'true_peak_dbtp': float(20*np.log10(max(np.max(np.abs(a)), 1e-10)))}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr('wcfi_tools.audio.updated_reference.scan_loudness', scanner)
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No host controls'))
    identifier = 'd'*32;folder = review.root/'diagnostic-staging'/identifier
    sources = {v: {'sha256': 'a'*64, 'duration_seconds': 12} for v in IDS}
    for v in ('bethel', 'maverick'):
        sources[v]['metadata'] = {'url': 'https://www.youtube.com/watch?v=example'}
    context = {'start_seconds': 40, 'duration_seconds': 12,
        'validation': {'source_packages_verified': True, 'only_RED_differs_between_WCFI_versions': True}}
    p = build_package(signals(), folder, context=context, sources=sources, ffmpeg='unused')
    yield review, identifier, folder, p
    review.close()


def test_identical_audio_does_not_report_a_gap_and_scalar_gain_is_not_quality():
    a = signals()['bethel'];m = describe_mix(a, 48000)
    metrics = {v: copy.deepcopy(m) for v in IDS}
    assert not any(f['screened_gap'] for f in findings(metrics))
    assert 'nearly mono' not in findings(metrics)[0]['interpretation']
    metrics['updated'] = describe_mix(a*2, 48000)
    assert not any(f['screened_gap'] for f in findings(metrics))
    assert metrics['updated']['side_energy_percent'] == pytest.approx(m['side_energy_percent'])


def test_matching_is_scalar_only_and_all_four_assets_have_the_same_measured_level(staged):
    _, _, folder, p = staged;r = p['reports']['MIX'];original = signals()
    for v in IDS:
        a, _ = sf.read(folder/AUDIO[f'updatedref_{v}_matched'], always_2d=True)
        np.testing.assert_allclose(a, original[v]*10**(r['matching']['gain_db'][v]/20), atol=1e-8)
        assert r['matching']['measured'][v]['integrated_lufs'] == pytest.approx(-24, abs=.15)
    assert r['findings'][0]['screened_gap']


def test_updated_reference_is_an_authenticated_read_only_mix_report(staged):
    review, identifier, folder, _ = staged;validate_package(folder)
    result = review.import_diagnostic({'study_id': identifier})
    assert review.import_diagnostic({'study_id': identifier}) == result
    assert set(result['jobs'][0]['reports']) == {'MIX'}
    assert not review.snapshot(include_updated_reference=False)['jobs']
    assert not review.snapshot(include_diagnostics=False)['jobs']
    assert review.snapshot(include_red_lowmid=False, include_presence=False, include_overlap=False)['jobs']
    for key in AUDIO:
        assert review.audio(identifier, 'MIX', key).startswith(b'RIFF')
        with pytest.raises(ValueError):
            review.audio(identifier, 'RED', key)
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'MIX', 'kind': 'software_gain'})


@pytest.mark.parametrize('problem', ['scope', 'choice', 'control', 'finding', 'forged_metrics', 'url', 'loudness',
    'checksum', 'path', 'nan', 'provenance', 'missing_reference'])
def test_unverified_or_misleading_comparisons_cannot_publish(staged, problem):
    review, identifier, folder, p = staged;r = p['reports']['MIX']
    if problem == 'scope':
        r['scope'] = 'fully_mixed_WCFI_service'
    elif problem == 'choice':
        r['choices']['red']['gain_db'] = -8
    elif problem == 'control':
        r['context']['validation']['only_RED_differs_between_WCFI_versions'] = False
    elif problem == 'finding':
        r['findings'][0]['evidence'] = 'Professional quality proven'
    elif problem == 'forged_metrics':
        r['metrics']['updated']['side_energy_percent'] = 40
        r['findings'] = findings(r['metrics'])
    elif problem == 'url':
        r['sources']['bethel']['metadata']['url'] = 'javascript:alert(1)'
    elif problem == 'loudness':
        r['matching']['measured']['updated']['integrated_lufs'] += 4
    elif problem == 'checksum':
        p['files'][next(iter(p['files']))] = '0'*64
    elif problem == 'path':
        r['audio']['updatedref_updated_matched'] = '../outside.wav'
    elif problem == 'nan':
        r['extra'] = float('nan')
    elif problem == 'provenance':
        r['context']['validation']['source_packages_verified'] = False
    else:
        r['sources'].pop('maverick')
    (folder/'study.json').write_text(json.dumps(p), encoding='utf-8')
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']
