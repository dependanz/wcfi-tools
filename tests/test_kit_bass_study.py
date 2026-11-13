"""Source-specific fixed-EQ trials, coactivity and whole-mix audition matching."""
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_bass_study import (
    AUDIO_KEYS,
    body_bell,
    build_package,
    overlap_metrics,
    versions,
)
from wcfi_tools.audio.kit_diagnostic import validate_package
from wcfi_tools.audio.review_service import RehearsalReview


def sources():
    t = np.arange(24000)/48000
    stereo = lambda a: np.stack([a, -a], axis=1)
    kit = stereo(.08*np.sin(2*np.pi*60*t)+.04*np.sin(2*np.pi*180*t)+.02*np.sin(2*np.pi*3000*t))
    bass = stereo(.03*np.sin(2*np.pi*110*t)+.04*np.sin(2*np.pi*200*t))
    red = np.repeat((.02*np.sin(2*np.pi*440*t))[:, None], 2, axis=1)
    return kit, bass, red


@pytest.mark.parametrize('hz,q', [(180, 1), (200, .8)])
def test_exact_cut_depth_and_identity_preserve_stereo_polarity(hz, q):
    t = np.arange(24000)/48000;a = np.stack([np.sin(2*np.pi*hz*t), -np.sin(2*np.pi*hz*t)], axis=1)
    np.testing.assert_array_equal(body_bell(a, hz=hz, q=q, gain_db=0), a)
    b = body_bell(a, hz=hz, q=q, gain_db=-2)
    assert 10*np.log10(np.mean(b*b)/np.mean(a*a)) == pytest.approx(-2, abs=.001)
    np.testing.assert_allclose(b[:, 0], -b[:, 1], atol=1e-14)


def test_each_trial_changes_only_the_prescribed_sources():
    kit, bass, red = sources();tracks, mixes = versions(kit, bass, kit+bass+red)
    np.testing.assert_array_equal(tracks['bass_space']['kit'], kit)
    np.testing.assert_array_equal(tracks['kit_body']['bass'], bass)
    np.testing.assert_array_equal(tracks['both']['kit'], tracks['kit_body']['kit'])
    np.testing.assert_array_equal(tracks['both']['bass'], tracks['bass_space']['bass'])
    for v in tracks:
        np.testing.assert_allclose(mixes[v]-tracks[v]['kit']-tracks[v]['bass'], red, atol=1e-15)
    before = overlap_metrics(kit, bass, red, selection_kit=kit)
    after = overlap_metrics(tracks['both']['kit'], tracks['both']['bass'], red, selection_kit=kit)
    assert after['kit_body_over_attack_db'] < before['kit_body_over_attack_db']-1.5
    assert sum(after['body_source_power_percent'].values()) == pytest.approx(100)
    for band in before['bands']:
        assert before['bands'][band]['selected_windows'] == after['bands'][band]['selected_windows']
    # Relative metrics do not mistake whole-mix gain for reduced overlap.
    louder = overlap_metrics(kit*2, bass*2, red*2, selection_kit=kit)
    assert louder['bands']['140-250']['bass_over_kit_db'] == pytest.approx(before['bands']['140-250']['bass_over_kit_db'])


def scanner(path, _ffmpeg):
    # Controlled RMS test stub, not EBU R128.
    a, _ = sf.read(path, always_2d=True)
    return {'integrated_lufs': float(10*np.log10(max(np.mean(a*a), 1e-20))),
            'true_peak_dbtp': float(20*np.log10(max(np.max(np.abs(a)), 1e-10)))}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr('wcfi_tools.audio.kit_bass_study.scan_loudness', scanner)
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No host writes'))
    identifier = 'e'*32;folder = review.root/'diagnostic-staging'/identifier
    kit, bass, red = sources()
    context = {'start_seconds': 40, 'duration_seconds': .5,
        'compressor': {'threshold_db': -16, 'ratio': 2, 'attack_ms': 15, 'release_ms': 100},
        'validation': {'master_sum_relative_error': 0, 'gentle_capture_verified': True, 'other_sources_preserved': True}}
    p = build_package(kit, bass, red, kit+bass+red, folder, context=context, ffmpeg='unused')
    yield review, identifier, folder, p
    review.close()


def test_whole_mix_matching_scales_each_complete_candidate_without_undoing_source_cuts(staged):
    _, _, folder, p = staged;r = p['reports']['KIT']
    kit, bass, red = sources();_, mixes = versions(kit, bass, kit+bass+red)
    for v, a in mixes.items():
        actual, _ = sf.read(folder/r['audio'][f'overlap_mix_{v}_matched'], always_2d=True)
        np.testing.assert_allclose(actual, a*10**(r['matching']['variant_gain_db'][v]/20), atol=1e-8)
    # Ensure candidates retain their intended spectral change after one scalar.
    assert r['overlap']['both']['kit_body_over_attack_db'] < r['overlap']['current']['kit_body_over_attack_db']-1.5


def test_overlapping_study_has_eight_read_only_assets_and_legacy_opt_in(staged):
    review, identifier, folder, _ = staged
    validate_package(folder);r = review.import_diagnostic({'study_id': identifier})
    assert review.import_diagnostic({'study_id': identifier}) == r
    assert not review.snapshot(include_overlap=False)['jobs']
    assert not review.snapshot(include_diagnostics=False)['jobs']
    assert len(AUDIO_KEYS) == 8
    for key in AUDIO_KEYS:
        assert review.audio(identifier, 'KIT', key).startswith(b'RIFF')
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'KIT', 'kind': 'software_gain'})


@pytest.mark.parametrize('problem', ['eq', 'fixed_eq', 'compression', 'residual', 'unverified', 'native_claim',
    'loudness', 'method', 'checksum', 'path', 'nan', 'extra_asset'])
def test_misrepresented_or_invalid_packages_cannot_publish(staged, problem):
    review, identifier, folder, p = staged;r = p['reports']['KIT']
    if problem == 'eq':
        r['filters']['kit']['hz'] = 400
    elif problem == 'fixed_eq':
        r['fixed_eq']['bell']['gain_db'] = 4
    elif problem == 'compression':
        r['context']['compressor']['threshold_db'] = -18
    elif problem == 'residual':
        r['context']['validation']['master_sum_relative_error'] = .1
    elif problem == 'unverified':
        r['context']['validation']['gentle_capture_verified'] = False
    elif problem == 'native_claim':
        r['origin'] = 'native_eq'
    elif problem == 'loudness':
        r['matching']['measured']['both']['integrated_lufs'] += 4
    elif problem == 'method':
        r['matching']['method'] = 'Normalize each source separately'
    elif problem == 'checksum':
        p['files'][next(iter(p['files']))] = '0'*64
    elif problem == 'path':
        r['audio']['overlap_mix_both_matched'] = '../outside.wav'
    elif problem == 'nan':
        r['extra'] = float('nan')
    else:
        p['files']['extra.wav'] = '0'*64
    (folder/'study.json').write_text(json.dumps(p), encoding='utf-8')
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']


def test_invalid_input_cannot_construct_overlap_previews(tmp_path):
    kit, bass, red = sources()
    with pytest.raises(ValueError):
        body_bell(kit, hz=180, q=1, gain_db=-12)
    with pytest.raises(ValueError):
        versions(kit, bass[:-1], kit+bass+red)
    with pytest.raises(ValueError):
        overlap_metrics(kit*0, bass, red, selection_kit=kit*0)
    with pytest.raises(ValueError):
        build_package(kit, bass, red, kit+bass, tmp_path/'bad', context={'validation': {'master_sum_relative_error': 0}}, ffmpeg='unused')
