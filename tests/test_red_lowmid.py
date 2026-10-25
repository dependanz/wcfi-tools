"""RED source-only EQ, level-matched control and correct multi-track publication."""
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_bass_study import write_mix_auditions
from wcfi_tools.audio.kit_diagnostic import validate_package
from wcfi_tools.audio.red_lowmid import (
    AUDIO_KEYS,
    build_package,
    inspect_red,
    lowmid_cut,
    red_versions,
    tone_metrics,
)
from wcfi_tools.audio.review_service import RehearsalReview


def sources():
    t = np.arange(96000)/48000
    red = .08*np.sin(2*np.pi*400*t)+.02*np.sin(2*np.pi*2000*t)
    red = np.stack([red, -red], axis=1)
    bed = np.repeat((.03*np.sin(2*np.pi*60*t)+.02*np.sin(2*np.pi*180*t))[:, None], 2, axis=1)
    return red, bed


def scanner(path, _ffmpeg):
    # Controlled RMS stub; real listening files use FFmpeg BS.1770.
    a, _ = sf.read(path, always_2d=True)
    return {'integrated_lufs': float(10*np.log10(max(np.mean(a*a), 1e-20))),
            'true_peak_dbtp': float(20*np.log10(max(np.max(np.abs(a)), 1e-10)))}


def test_cut_targets_400hz_without_collapsing_antiphase_stereo():
    t = np.arange(24000)/48000;a = np.stack([np.sin(2*np.pi*400*t), -np.sin(2*np.pi*400*t)], axis=1)
    b = lowmid_cut(a)
    assert 10*np.log10(np.mean(b*b)/np.mean(a*a)) == pytest.approx(-2, abs=.001)
    np.testing.assert_allclose(b[:, 0], -b[:, 1], atol=1e-14)


def test_red_only_changes_leave_bed_fixed_and_level_control_retains_tone():
    red, bed = sources();reds, mixes = red_versions(red, red+bed, level_gain_db=-1.2)
    for v in reds:
        np.testing.assert_allclose(mixes[v]-reds[v], bed, atol=1e-15)
    before = tone_metrics(red)['lowmid_over_presence_db']
    assert tone_metrics(reds['level'])['lowmid_over_presence_db'] == pytest.approx(before)
    assert tone_metrics(reds['eq'])['lowmid_over_presence_db'] < before-1.5
    inspection = inspect_red(red)
    assert 390 < inspection['whole']['dominant_peaks_hz'][0] < 410
    assert len(inspection['two_second_windows']) == 1
    with pytest.raises(ValueError):
        red_versions(red, red+bed, level_gain_db=-12)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr('wcfi_tools.audio.kit_bass_study.scan_loudness', scanner)
    monkeypatch.setattr('wcfi_tools.audio.red_lowmid.scan_loudness', scanner)
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No host writes'))
    identifier = 'f'*32;folder = review.root/'diagnostic-staging'/identifier
    red, bed = sources()
    c = {'start_seconds': 40, 'duration_seconds': 2,
         'validation': {'master_sum_relative_error': 0, 'fixed_bed_verified': True, 'saved_capture_verified': True}}
    p = build_package(red, red+bed, folder, context=c, ffmpeg='unused')
    yield review, identifier, folder, p
    review.close()


def test_matched_red_control_and_whole_mix_normalization_preserve_intended_comparison(staged):
    _, _, folder, p = staged;r = p['reports']['RED']
    assert r['red_loudness']['level']['integrated_lufs'] == pytest.approx(r['red_loudness']['eq']['integrated_lufs'], abs=.15)
    red, bed = sources();_, mixes = red_versions(red, red+bed, level_gain_db=r['level_control_gain_db'])
    for v, a in mixes.items():
        actual, _ = sf.read(folder/r['audio'][f'redlowmid_mix_{v}_matched'], always_2d=True)
        np.testing.assert_allclose(actual, a*10**(r['matching']['variant_gain_db'][v]/20), atol=1e-8)


def test_red_report_publishes_under_red_with_correct_assets_and_legacy_filters(staged):
    review, identifier, folder, _ = staged;validate_package(folder)
    result = review.import_diagnostic({'study_id': identifier})
    assert review.import_diagnostic({'study_id': identifier}) == result
    assert set(result['jobs'][0]['reports']) == {'RED'}
    assert not review.snapshot(include_red_lowmid=False)['jobs']
    assert not review.snapshot(include_diagnostics=False)['jobs']
    assert review.snapshot(include_overlap=False, include_presence=False, include_compression=False, include_combined=False)['jobs']
    for key in AUDIO_KEYS:
        assert review.audio(identifier, 'RED', key).startswith(b'RIFF')
        with pytest.raises(ValueError):
            review.audio(identifier, 'KIT', key)
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'RED', 'kind': 'software_gain'})


@pytest.mark.parametrize('problem', ['eq', 'bed', 'provenance', 'control_level', 'control_tone', 'residual',
    'source_guard', 'matching', 'path', 'checksum', 'nan', 'extra_asset'])
def test_unverified_red_studies_cannot_publish(staged, problem):
    review, identifier, folder, p = staged;r = p['reports']['RED']
    if problem == 'eq':
        r['filter']['gain_db'] = -8
    elif problem == 'bed':
        r['kit_choice']['bass_unchanged'] = False
    elif problem == 'provenance':
        r['origin'] = 'native_pro_q'
    elif problem == 'control_level':
        r['red_loudness']['level']['integrated_lufs'] += 3
    elif problem == 'control_tone':
        r['observed']['red']['level']['lowmid_over_presence_db'] -= 1
    elif problem == 'residual':
        r['context']['validation']['master_sum_relative_error'] = .1
    elif problem == 'source_guard':
        r['context']['validation']['fixed_bed_verified'] = False
    elif problem == 'matching':
        r['matching']['measured']['eq']['integrated_lufs'] += 4
    elif problem == 'path':
        r['audio']['redlowmid_mix_eq_matched'] = '../outside.wav'
    elif problem == 'checksum':
        p['files'][next(iter(p['files']))] = '0'*64
    elif problem == 'nan':
        r['extra'] = float('nan')
    else:
        p['files']['extra.wav'] = '0'*64
    (folder/'study.json').write_text(json.dumps(p), encoding='utf-8')
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']


def test_mix_writer_rejects_unexpected_asset_namespaces(tmp_path):
    red, _ = sources()
    with pytest.raises(ValueError):
        write_mix_auditions({'current': red}, tmp_path, track='../outside', prefix='redlowmid', ffmpeg='unused')
