"""Shelf response, source-preserving mix matching and safe local publication."""
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_diagnostic import validate_package
from wcfi_tools.audio.kit_shelf import (
    AUDIO_KEYS,
    audio_keys,
    build_package,
    presence_bell,
    shelf_versions,
    validate_path,
)
from wcfi_tools.audio.kit_study import low_shelf, write_auditions
from wcfi_tools.audio.review_service import RehearsalReview


def signal(seconds=.5):
    t = np.arange(round(seconds*48000))/48000
    kit = .08*np.sin(2*np.pi*60*t)+.02*np.sin(2*np.pi*3000*t)
    background = .03*np.sin(2*np.pi*440*t)
    return np.repeat(kit[:, None], 2, axis=1).astype(np.float32), np.repeat(background[:, None], 2, axis=1)


def scanner(path, _ffmpeg):
    """Use RMS for controlled unit matching tests; not an EBU R128 implementation."""
    a, _ = sf.read(path, always_2d=True)
    return {'integrated_lufs': float(10*np.log10(max(np.mean(a*a), 1e-20))),
            'true_peak_dbtp': float(20*np.log10(max(np.max(np.abs(a)), 1e-10)))}


@pytest.mark.parametrize('cut', [-1, -2])
@pytest.mark.parametrize('hz,fraction', [(10, 1), (120, .5), (5000, 0)])
def test_exact_requested_shelves_have_correct_asymptote_and_transition(cut, hz, fraction):
    t = np.arange(48000)/48000
    a = np.repeat(np.sin(2*np.pi*hz*t)[:, None], 2, axis=1).astype(np.float32)
    b = low_shelf(a, 48000, gain_db=cut, hz=120)
    change = 10*np.log10(np.mean(b.astype(float)**2)/np.mean(a.astype(float)**2))
    assert change == pytest.approx(cut*fraction, abs=.01)


def test_substitution_preserves_other_sources_and_rejects_unverified_mix():
    kit, other = signal();mix = other+kit*2
    kits, mixes = shelf_versions(kit, mix, kit_to_mix_gain=2, sum_relative_error=.00001)
    assert set(kits) == {'current', 'shelf1', 'shelf2'}
    for v in kits:
        np.testing.assert_allclose(mixes[v]-kits[v]*2, other, atol=1e-8, rtol=0)
    with pytest.raises(ValueError):
        shelf_versions(kit, mix, kit_to_mix_gain=2, sum_relative_error=.1)


def test_matching_uses_actual_path_gain_and_keeps_other_tracks_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr('wcfi_tools.audio.kit_study.scan_loudness', scanner)
    kit, other = signal()
    kits = {'current': kit, 'shelf1': kit*.8, 'shelf2': kit*.6}
    mixes = {v: other+a*2 for v, a in kits.items()}
    audio, _, match = write_auditions(kits, mixes, tmp_path, 48000, 'unused',
        match_kit_in_mix=True, kit_to_mix_gain=2)
    common = 10**(match['mix']['common_audition_gain_db']/20)
    for v in kits:
        a, _ = sf.read(tmp_path/audio[f'study_mix_{v}_matched'], always_2d=True)
        # Final saved PCM is float32; allow its rounding precision.
        np.testing.assert_allclose(a, (other+kit*2)*common, atol=2e-8, rtol=0)


@pytest.fixture
def path_snapshot():
    def track(name, db):
        return {'id': name, 'name': name, 'mute': False, 'solo': False,
            'pan': {'value': 0}, 'volume': {'display': f'{db:.1f} dB', 'automation_state': 0},
            'sends': [], 'devices': [], 'routing': {'output_routing_type': 'Master'}}
    kit, group, master = track('KIT', 6), track('INSTRUMENTS', 0), track('Master', -6)
    kit.update(group_id=group['id']);kit['routing']['output_routing_type'] = 'INSTRUMENTS'
    kit['devices'] = [{'name': 'Compressor'}, {'name': 'WCFI Analysis Node'}]
    master['devices'] = [{'name': 'WCFI Analysis Node'}]*2+[{'name': 'MiniMeters - Audio-Server'}]
    return {'tracks': [kit, group, master]}


@pytest.mark.parametrize('problem', ['nonlinear_master', 'group_fx', 'send', 'pan', 'mute', 'automation'])
def test_unknown_paths_cannot_authorize_local_mix_substitution(path_snapshot, problem):
    assert validate_path(path_snapshot)['kit_to_mix_gain'] == pytest.approx(1)
    kit, group, master = path_snapshot['tracks']
    if problem == 'nonlinear_master':
        master['devices'].append({'name': 'Limiter'})
    elif problem == 'group_fx':
        group['devices'].append({'name': 'Compressor'})
    elif problem == 'send':
        kit['sends'] = [{'value': .1}]
    elif problem == 'pan':
        kit['pan']['value'] = .2
    elif problem == 'mute':
        kit['mute'] = True
    else:
        master['volume']['automation_state'] = 1
    with pytest.raises(ValueError):
        validate_path(path_snapshot)


@pytest.fixture(params=['shelf', 'presence', 'combined'])
def staged(tmp_path, monkeypatch, request):
    monkeypatch.setattr('wcfi_tools.audio.kit_study.scan_loudness', scanner)
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No host control'))
    identifier = 'b'*32;folder = review.root/'diagnostic-staging'/identifier
    kit, other = signal()
    context = {'start_seconds': 40, 'duration_seconds': .5, 'path': {'kit_to_mix_gain': 1},
        'validation': {'master_sum_relative_error': 0}}
    package = build_package(kit, kit+other, folder, ffmpeg='unused', context=context, recipe=request.param)
    yield review, identifier, folder, package
    review.close()


def test_eq_study_publishes_all_declared_files_without_an_apply_action(staged):
    review, identifier, folder, package = staged
    assert len(AUDIO_KEYS) == 12
    assert len(package['files']) == 4*len(package['reports']['KIT']['variants'])
    validate_package(folder)
    snapshot = review.import_diagnostic({'study_id': identifier})
    assert review.import_diagnostic({'study_id': identifier}) == snapshot
    assert not review.snapshot(include_diagnostics=False)['jobs']
    if package['reports']['KIT']['kind'] == 'wcfi_kit_presence_study':
        assert not review.snapshot(include_presence=False)['jobs']
    else:
        assert review.snapshot(include_presence=False)['jobs']
    if package['reports']['KIT']['kind'] == 'wcfi_kit_combined_study':
        assert not review.snapshot(include_combined=False)['jobs']
    else:
        assert review.snapshot(include_combined=False)['jobs']
    for key in audio_keys(package['reports']['KIT']['kind']):
        assert review.audio(identifier, 'KIT', key).startswith(b'RIFF')
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'KIT', 'kind': 'software_gain'})


@pytest.mark.parametrize('problem', ['native_claim', 'prescription', 'other_tracks_change', 'checksum',
    'no_null', 'loudness', 'missing_variant', 'duplicate_variant', 'wrong_gain'])
def test_misrepresented_shelf_studies_cannot_publish(staged, problem):
    review, identifier, folder, package = staged
    report = package['reports']['KIT']
    if problem == 'native_claim':
        report['variants'][1] = report['variants'][1] | {'origin': 'native_capture'}
    elif problem == 'prescription':
        report['filter']['hz'] = 240
    elif problem == 'other_tracks_change':
        report['matching']['mix']['kit_contribution_matched'] = False
    elif problem == 'checksum':
        package['files'][next(iter(package['files']))] = '0'*64
    elif problem == 'no_null':
        report['context']['validation']['master_sum_relative_error'] = .05
    elif problem == 'loudness':
        candidate = report['variants'][1]['id']
        report['matching']['kit']['measured_matched_loudness'][candidate]['integrated_lufs'] += 4
    elif problem == 'missing_variant':
        report['variants'].pop()
    elif problem == 'duplicate_variant':
        report['variants'][-1] = report['variants'][1]
    else:
        field = 'presence_db' if report['kind'] == 'wcfi_kit_combined_study' else 'gain_db'
        report['variants'][1][field] = 5
    (folder/'study.json').write_text(json.dumps(package))
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']


@pytest.mark.parametrize('gain', [1, 2])
@pytest.mark.parametrize('hz,fraction', [(60, 0), (3000, 1), (10000, 0)])
def test_requested_presence_bell_has_correct_center_and_bounded_tails(gain, hz, fraction):
    t = np.arange(48000)/48000
    a = np.repeat(np.sin(2*np.pi*hz*t)[:, None], 2, axis=1).astype(np.float32)
    b = presence_bell(a, 48000, gain_db=gain)
    change = 10*np.log10(np.mean(b.astype(float)**2)/np.mean(a.astype(float)**2))
    assert change == pytest.approx(gain*fraction, abs=.3 if hz == 10000 else .01)


def test_presence_is_separate_from_shelf_and_preserves_periodic_phase():
    kit, other = signal(1)
    kits, mixes = shelf_versions(kit, kit+other, kit_to_mix_gain=1, sum_relative_error=0, recipe='presence')
    np.testing.assert_array_equal(kits['current'], kit)
    assert not any('shelf' in v for v in kits)
    np.testing.assert_allclose(presence_bell(np.roll(kit, 131, axis=0), 48000),
                               np.roll(presence_bell(kit, 48000), 131, axis=0), atol=1e-7, rtol=0)
    for v in kits:
        np.testing.assert_allclose(mixes[v]-kits[v], other, atol=1e-8, rtol=0)


def test_presence_cannot_accept_arbitrary_frequency_q_or_unbounded_gain():
    kit, _ = signal()
    for settings in ({'hz': 120}, {'q': 10}, {'gain_db': 3}, {'gain_db': -1}, {'gain_db': float('nan')}):
        with pytest.raises(ValueError):
            presence_bell(kit, 48000, **settings)


def test_combined_versions_cover_the_cartesian_product_from_the_unchanged_chain():
    kit, other = signal(1)
    kits, mixes = shelf_versions(kit, kit*2+other, kit_to_mix_gain=2, sum_relative_error=0, recipe='combined')
    assert set(kits) == {'current', 's1p1', 's1p2', 's2p1', 's2p2'}
    np.testing.assert_array_equal(kits['current'], kit)
    for shelf in (1, 2):
        for presence in (1, 2):
            key = f's{shelf}p{presence}'
            expected = presence_bell(low_shelf(kit, 48000, gain_db=-shelf), 48000, gain_db=presence)
            np.testing.assert_array_equal(kits[key], expected)
            np.testing.assert_allclose(mixes[key]-kits[key]*2, other, atol=1e-8, rtol=0)


@pytest.mark.parametrize('shelf,presence', [(1, 1), (1, 2), (2, 1), (2, 2)])
@pytest.mark.parametrize('hz', [10, 3000])
def test_combined_response_retains_both_requested_filter_gains(shelf, presence, hz):
    t = np.arange(48000)/48000
    a = np.repeat(np.sin(2*np.pi*hz*t)[:, None], 2, axis=1).astype(np.float32)
    b = presence_bell(low_shelf(a, 48000, gain_db=-shelf), 48000, gain_db=presence)
    change = 10*np.log10(np.mean(b.astype(float)**2)/np.mean(a.astype(float)**2))
    assert change == pytest.approx(-shelf if hz == 10 else presence, abs=.01)
