"""Fixed foundation, conservative channel roles and source-preserving placement."""
import copy
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.full_band import PLAN, build_package, findings, full_band_mix, place_mono
from wcfi_tools.audio.kit_diagnostic import validate_package
from wcfi_tools.audio.review_service import RehearsalReview
from wcfi_tools.audio.updated_reference import AUDIO, IDS


def inputs():
    t = np.arange(576000)/48000
    stereo = lambda x: np.repeat(x[:, None], 2, axis=1)
    foundation = stereo(.06*np.sin(2*np.pi*200*t)+.02*np.sin(2*np.pi*2000*t))
    additions = {p['track']: stereo(.03*np.sin(2*np.pi*hz*t)) for p, hz in zip(PLAN, (800, 400, 600, 1200), strict=True)}
    references = {'bethel': foundation.copy(), 'maverick': foundation*.8}
    return foundation, additions, references


def test_mono_placement_preserves_channel_power_and_stereo_inputs_are_not_reinterpreted():
    a, _, _ = inputs()
    left, right = place_mono(a, -.35), place_mono(a, .35)
    assert np.mean(left*left) == pytest.approx(np.mean(a*a), abs=1e-15)
    np.testing.assert_allclose(left, right[:, ::-1], atol=1e-15)
    np.testing.assert_array_equal(place_mono(a, 0), a)
    actual_stereo = np.stack([a[:, 0], -a[:, 0]], axis=1)
    np.testing.assert_array_equal(place_mono(actual_stereo, 0), actual_stereo)
    with pytest.raises(ValueError):
        place_mono(actual_stereo, .35)


def test_complete_balance_retains_the_fixed_foundation_and_exact_master_gain():
    foundation, additions, _ = inputs();full, contributions = full_band_mix(foundation, additions)
    np.testing.assert_allclose(full-sum(contributions.values()), foundation, atol=1e-15)
    for p in PLAN:
        expected = place_mono(additions[p['track']], p['pan'])*10**((p['fader_db']-6)/20)
        np.testing.assert_array_equal(contributions[p['track']], expected)
    # The quiet mic is capped at unity raw-to-mix gain after Master.
    assert next(p for p in PLAN if p['track']=='GREEN')['fader_db'] == 6
    with pytest.raises(ValueError):
        full_band_mix(foundation, additions | {'MDMIC Talkback': foundation})


def scanner(path, _ffmpeg):
    # Controlled RMS stub; real assets use BS.1770/true-peak scanning.
    a, _ = sf.read(path, always_2d=True)
    return {'integrated_lufs': float(10*np.log10(max(np.mean(a*a), 1e-20))),
            'true_peak_dbtp': float(20*np.log10(max(np.max(np.abs(a)), 1e-10)))}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr('wcfi_tools.audio.updated_reference.scan_loudness', scanner)
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No host controls'))
    identifier = 'c'*32;folder = review.root/'diagnostic-staging'/identifier
    sources = {v: {'sha256': 'a'*64, 'duration_seconds': 12} for v in IDS}
    for v in ('bethel', 'maverick'):
        sources[v]['metadata'] = {'url': 'https://www.youtube.com/watch?v=example'}
    context = {'start_seconds': 40, 'duration_seconds': 12,
        'validation': {'source_hashes_verified': True, 'foundation_unchanged': True, 'added_channels_have_no_active_FX': True}}
    stats = {a['track']: {'full_scale_samples': 0, 'full_scale_percent': 0, 'peak_dbfs': -20,
                         'loudness': {'integrated_lufs': -30}} for a in PLAN}
    foundation, additions, references = inputs()
    p = build_package(foundation, additions, references, folder, sources=sources, context=context, source_stats=stats, ffmpeg='unused')
    yield review, identifier, folder, p
    review.close()


def test_complete_mix_publishes_with_own_opt_in_and_no_apply_action(staged):
    review, identifier, folder, _ = staged;validate_package(folder)
    result = review.import_diagnostic({'study_id': identifier})
    assert review.import_diagnostic({'study_id': identifier}) == result
    assert not review.snapshot(include_full_band=False)['jobs']
    assert review.snapshot(include_updated_reference=False, include_red_lowmid=False)['jobs']
    for key in AUDIO:
        assert review.audio(identifier, 'MIX', key).startswith(b'RIFF')
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'MIX', 'kind': 'software_gain'})


def test_no_unobserved_clipping_warning_and_actual_clipping_is_reported(staged):
    _, _, _, p = staged;r = p['reports']['MIX']
    assert r['findings'][0]['title'] == 'Acoustic input sample health'
    stats = copy.deepcopy(r['source_stats']);stats['AC GUITAR'].update(full_scale_samples=100, full_scale_percent=1)
    new = findings(r['metrics'], stats)
    assert 'recorded full-scale' in new[0]['title']
    assert 'physical analogue mixer trim' in new[0]['next_test']


@pytest.mark.parametrize('problem', ['plan', 'foundation', 'choice', 'processing', 'scope', 'finding',
    'forged_metric', 'loudness', 'path', 'checksum', 'nan', 'talkback', 'missing_source_stats'])
def test_full_band_provenance_or_assets_cannot_be_misrepresented(staged, problem):
    review, identifier, folder, p = staged;r = p['reports']['MIX']
    if problem == 'plan':
        r['plan'][0]['fader_db'] = 6
    elif problem == 'foundation':
        r['context']['validation']['foundation_unchanged'] = False
    elif problem == 'choice':
        r['fixed_choices']['red']['gain_db'] = 0
    elif problem == 'processing':
        r['context']['validation']['added_channels_have_no_active_FX'] = False
    elif problem == 'scope':
        r['scope'] = 'native_full_service_output'
    elif problem == 'finding':
        r['findings'][1]['evidence'] = 'Professional sound verified'
    elif problem == 'forged_metric':
        r['metrics']['updated']['side_energy_percent'] = 50;r['findings'] = findings(r['metrics'], r['source_stats'])
    elif problem == 'loudness':
        r['matching']['measured']['updated']['integrated_lufs'] += 4
    elif problem == 'path':
        r['audio']['updatedref_updated_matched'] = '../outside.wav'
    elif problem == 'checksum':
        p['files'][next(iter(p['files']))] = '0'*64
    elif problem == 'nan':
        r['extra'] = float('nan')
    elif problem == 'missing_source_stats':
        r['source_stats'].pop('KEYS')
    else:
        r['plan'].append({'track': 'MDMIC Talkback', 'fader_db': 0, 'pan': 0})
    (folder/'study.json').write_text(json.dumps(p), encoding='utf-8')
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']
