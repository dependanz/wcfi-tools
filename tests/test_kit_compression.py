"""Paired gain estimates, fixed EQ and read-only comparison publication."""
import copy
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_compression import (
    AUDIO_KEYS,
    build_package,
    compression_versions,
    gain_reduction,
    only_threshold_changed,
)
from wcfi_tools.audio.kit_diagnostic import validate_package
from wcfi_tools.audio.kit_shelf import presence_bell
from wcfi_tools.audio.kit_study import low_shelf
from wcfi_tools.audio.review_service import RehearsalReview


def signal():
    t = np.arange(24000)/48000
    a = .08*np.sin(2*np.pi*60*t)+.03*np.sin(2*np.pi*3000*t)
    # Antiphase is intentional: folding to mono would lose all energy.
    return np.stack([a, -a], axis=1)


@pytest.mark.parametrize('reduction', [0, 1, 2.5])
def test_known_gain_and_output_gain_are_separated_in_antiphase_stereo(reduction):
    a = signal();m = gain_reduction(a, a*10**((-5.87-reduction)/20), output_gain_db=-5.87)
    assert m['strong_p50_p90_db'] == pytest.approx([reduction, reduction], abs=1e-10)
    assert m['active_1ms_max_db'] == pytest.approx(reduction, abs=1e-10)
    assert m['gain_model_relative_rms_error'] < 1e-12


def test_slowly_changing_attenuation_is_not_confused_with_constant_output_gain():
    a = signal();gr = 1+np.sin(np.arange(len(a))/48000*2*np.pi)
    m = gain_reduction(a, a*10**((-6-gr[:, None])/20), output_gain_db=-6)
    assert 1.9 < m['active_1ms_max_db'] <= 2.001
    assert m['gain_model_relative_rms_error'] < .002


@pytest.mark.parametrize('problem', ['silent', 'nonfinite', 'polarity', 'wrong_audio', 'shape'])
def test_gain_estimates_reject_pairs_that_do_not_support_the_model(problem):
    a = signal();b = a*.5
    if problem == 'silent':
        a[:] = 0
    elif problem == 'nonfinite':
        b[2, 0] = np.nan
    elif problem == 'polarity':
        b *= -1
    elif problem == 'wrong_audio':
        b = np.roll(b, 35, axis=0)
    else:
        b = b[:-1]
    with pytest.raises(ValueError):
        gain_reduction(a, b, output_gain_db=-6)


@pytest.mark.parametrize('problem', ['attack', 'other_track', 'transport', 'session'])
def test_native_trial_guard_permits_only_threshold_changes(problem):
    s = {'session_id': 'one', 'transport': {'is_playing': False}, 'tracks': [
        {'id': 'kit', 'name': 'KIT', 'devices': [{'class_name': 'Compressor2', 'parameters': [
            {'name': 'Threshold', 'value': .7, 'display': '-6 dB'}, {'name': 'Attack', 'value': 15}]}]},
        {'id': 'red', 'name': 'RED', 'volume': 0}]}
    changed = copy.deepcopy(s);changed['tracks'][0]['devices'][0]['parameters'][0].update(value=.4, display='-18 dB')
    assert only_threshold_changed(s, changed)['before_value'] == .7
    if problem == 'attack':
        changed['tracks'][0]['devices'][0]['parameters'][1]['value'] = 1
    elif problem == 'other_track':
        changed['tracks'][1]['volume'] = -6
    elif problem == 'transport':
        changed['transport']['is_playing'] = True
    else:
        changed['session_id'] = 'two'
    with pytest.raises(ValueError):
        only_threshold_changed(s, changed)


def scanner(path, _ffmpeg):
    # Controlled RMS matching stub, not an EBU R128 implementation.
    a, _ = sf.read(path, always_2d=True)
    return {'integrated_lufs': float(10*np.log10(max(np.mean(a*a), 1e-20))),
            'true_peak_dbtp': float(20*np.log10(max(np.max(np.abs(a)), 1e-10)))}


def inputs():
    raw = signal();processed = {v: raw*10**((-6-gr)/20) for v, gr in [('current', 0), ('gentle', 1), ('light', 2)]}
    other = np.repeat((.03*np.sin(np.arange(len(raw))*2*np.pi*440/48000))[:, None], 2, axis=1)
    return raw, processed, other+processed['current']*2, other


def test_every_native_capture_gets_exactly_the_same_eq_and_only_kit_changes_in_mix():
    raw, processed, mix, other = inputs()
    kits, mixes, reductions = compression_versions(raw, processed, mix, kit_to_mix_gain=2,
        output_gain_db=-6, sum_relative_error=0)
    for v, a in processed.items():
        np.testing.assert_array_equal(kits[v], presence_bell(low_shelf(a, 48000, hz=120, gain_db=-1), 48000, gain_db=2))
        np.testing.assert_allclose(mixes[v]-kits[v]*2, other, atol=2e-8, rtol=0)
    assert reductions['light']['strong_p50_p90_db'][1] == pytest.approx(2)
    with pytest.raises(ValueError):
        compression_versions(raw, processed, mix, kit_to_mix_gain=2, output_gain_db=-6, sum_relative_error=.01)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr('wcfi_tools.audio.kit_study.scan_loudness', scanner)
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No host control'))
    identifier = 'd'*32;folder = review.root/'diagnostic-staging'/identifier
    raw, processed, mix, _ = inputs()
    context = {'start_seconds': 40, 'duration_seconds': .5, 'path': {'kit_to_mix_gain': 2},
        'compressor': {'ratio': 2, 'attack_ms': 15, 'release_ms': 100, 'wet': 1, 'lookahead_ms': 0, 'output_gain_db': -6},
        'validation': {'master_sum_relative_errors': dict.fromkeys(processed, 0), 'raw_and_other_sources_unchanged': True},
        'native_trials': {v: {'restored': True, 'only_threshold_changed': True, 'new_underruns': 0, 'threshold_db': db}
            for v, db in [('gentle', -16), ('light', -18)]}}
    package = build_package(raw, processed, mix, folder, ffmpeg='unused', context=context)
    yield review, identifier, folder, package
    review.close()


def test_compression_study_publishes_twelve_assets_with_opt_in_and_no_apply(staged):
    review, identifier, folder, _ = staged
    validate_package(folder)
    result = review.import_diagnostic({'study_id': identifier})
    assert review.import_diagnostic({'study_id': identifier}) == result
    assert not review.snapshot(include_compression=False)['jobs']
    assert not review.snapshot(include_diagnostics=False)['jobs']
    for key in AUDIO_KEYS:
        assert review.audio(identifier, 'KIT', key).startswith(b'RIFF')
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'KIT', 'kind': 'software_gain'})


@pytest.mark.parametrize('problem', ['eq', 'restore', 'input', 'residual', 'threshold', 'target', 'bad_model',
    'loudness', 'other_sources', 'path', 'checksum', 'nan', 'native_claim'])
def test_unverified_compression_packages_cannot_publish(staged, problem):
    review, identifier, folder, package = staged;r = package['reports']['KIT']
    if problem == 'eq':
        r['fixed_eq']['bell']['gain_db'] = 4
    elif problem == 'restore':
        r['context']['native_trials']['light']['restored'] = False
    elif problem == 'input':
        r['context']['validation']['raw_and_other_sources_unchanged'] = False
    elif problem == 'residual':
        r['context']['validation']['master_sum_relative_errors']['light'] = .1
    elif problem == 'threshold':
        r['context']['native_trials']['light']['only_threshold_changed'] = False
    elif problem == 'target':
        r['gain_reduction']['light']['strong_p50_p90_db'] = [.5, .8]
    elif problem == 'bad_model':
        r['gain_reduction']['light']['gain_model_relative_rms_error'] = .2
    elif problem == 'loudness':
        r['matching']['kit']['measured_matched_loudness']['light']['integrated_lufs'] += 4
    elif problem == 'other_sources':
        r['matching']['mix']['kit_contribution_matched'] = False
    elif problem == 'path':
        r['audio']['study_kit_light_matched'] = '../outside.wav'
    elif problem == 'checksum':
        package['files'][next(iter(package['files']))] = '0'*64
    elif problem == 'nan':
        r['unknown'] = float('nan')
    else:
        r['origin'] = 'all_native_eq'
    (folder/'study.json').write_text(json.dumps(package), encoding='utf-8')
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']
