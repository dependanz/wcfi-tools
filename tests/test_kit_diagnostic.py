"""Diagnostic signal invariants, scope boundaries and immutable local publication."""
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio.kit_diagnostic import (
    AUDIO,
    IDS,
    analyze_signal,
    compare_pair,
    validate_package,
)
from wcfi_tools.audio.kit_study import low_shelf
from wcfi_tools.audio.review_service import RehearsalReview
from wcfi_tools.audio.service import EmulatorServer


def signal(seconds=1):
    t = np.arange(round(48000*seconds))/48000
    wave = .1*np.sin(2*np.pi*70*t)+.02*np.sin(2*np.pi*3000*t)
    return np.column_stack([wave, wave]).astype(np.float32)


def test_spectrum_preserves_antiphase_energy_and_reports_mono_cancellation():
    normal = signal();anti = normal.copy();anti[:, 1] *= -1
    a, b = analyze_signal(normal), analyze_signal(anti)
    assert b['rms_dbfs'] == pytest.approx(a['rms_dbfs'])
    assert b['spectrum']['band_energy_percent'] == pytest.approx(a['spectrum']['band_energy_percent'])
    assert b['side_energy_percent'] == pytest.approx(100)
    assert b['mono_energy_change_db'] < -80


def test_scalar_gain_does_not_imply_tone_or_transient_improvement():
    audio = signal(2);audio *= np.tile(np.r_[np.linspace(0, 1, 240), np.exp(-np.arange(9360)/1000)], 10)[:, None]
    pair = compare_pair(audio, audio*.5)
    assert pair['best_fit_scalar_gain_db'] == pytest.approx(-6.0206, abs=.0001)
    assert pair['scalar_residual_relative_rms'] < 1e-7
    assert pair['crest_change_db'] == pytest.approx(0, abs=1e-5)
    assert max(abs(v) for v in pair['band_shape_change_db'].values()) < 1e-5
    assert pair['raw_events'] == pair['processed_events']


def test_known_low_shelf_is_detected_after_level_normalization():
    raw = signal();post = low_shelf(raw, 48000, hz=120, gain_db=-2)
    a, b = analyze_signal(raw), analyze_signal(post)
    assert b['spectrum']['weight_over_attack_db'] < a['spectrum']['weight_over_attack_db']-1.5
    assert b['spectrum']['shape'] != a['spectrum']['shape']


@pytest.mark.parametrize('bad', [np.full((24000, 2), np.nan), np.ones((12000, 2)), np.ones((24000, 3))])
def test_invalid_audio_is_rejected(bad):
    with pytest.raises(ValueError):
        analyze_signal(bad)


@pytest.fixture
def staged(tmp_path):
    review = RehearsalReview(tmp_path/'state', tmp_path, client_factory=lambda: pytest.fail('No Live access'))
    identifier = 'a'*32;folder = review.root/'diagnostic-staging'/identifier;folder.mkdir(parents=True)
    a = signal(.5);files = {}
    for filename in AUDIO.values():
        sf.write(folder/filename, a, 48000, subtype='FLOAT')
        files[filename] = hashlib.sha256((folder/filename).read_bytes()).hexdigest()
    observation = analyze_signal(a)
    report = {'kind': 'wcfi_kit_diagnostic', 'title': 'KIT diagnostic', 'audio': AUDIO,
        'processing_applied': False, 'reference_scope': 'full_mix_context_only', 'audio_models_run': 0,
        'measurements': {name: observation for name in IDS},
        'matched': {name: {'integrated_lufs': -24, 'true_peak_dbtp': -20} for name in IDS},
        'target_lufs': -24, 'context': {'start_seconds': 40},
        'findings': [{'title': 'A measured observation'}], 'next_tests': [{'title': 'A proposed test'}]}
    package = {'kind': 'wcfi_kit_diagnostic_package', 'schema_version': 1, 'reports': {'KIT': report}, 'files': files}
    (folder/'study.json').write_text(json.dumps(package))
    yield review, identifier, folder, package
    review.close()


def test_read_only_diagnostic_is_persistent_idempotent_and_hidden_from_old_clients(staged):
    review, identifier, _, _ = staged
    result = review.import_diagnostic({'study_id': identifier})
    assert result == review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot(include_diagnostics=False)['jobs']
    assert review.audio(identifier, 'KIT', 'diagnostic_raw_matched').startswith(b'RIFF')
    with pytest.raises(ValueError):
        review.audio(identifier, 'KIT', '../../outside')
    with pytest.raises(ValueError):
        review.apply({'job_id': identifier, 'track': 'KIT', 'kind': 'software_gain'})
    restored = RehearsalReview(review.root.parent, review.library_root)
    try:
        assert restored.snapshot() == result
    finally:
        restored.close()


@pytest.mark.parametrize('case', ['checksum', 'path', 'format', 'nan', 'scope', 'applied', 'gpu_score', 'matching', 'missing_signal'])
def test_invalid_packages_cannot_publish_a_misrepresented_diagnostic(staged, case):
    review, identifier, folder, package = staged
    report = package['reports']['KIT']
    if case == 'checksum':
        package['files'][next(iter(AUDIO.values()))] = '0'*64
    elif case == 'path':
        report['audio'] = copy.deepcopy(AUDIO) | {'diagnostic_raw_matched': '../outside.wav'}
    elif case in ('format', 'nan'):
        path = folder/AUDIO['diagnostic_raw_matched']
        sf.write(path, np.full((24000, 2), np.nan if case == 'nan' else .01),
                 44100 if case == 'format' else 48000, subtype='FLOAT')
        package['files'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif case == 'scope':
        report['reference_scope'] = 'isolated_reference_drums'
    elif case == 'applied':
        report['processing_applied'] = True
    elif case == 'gpu_score':
        report['audio_models_run'] = 1
    elif case == 'matching':
        report['matched']['raw']['integrated_lufs'] = -10
    else:
        del report['measurements']['bethel']
    (folder/'study.json').write_text(json.dumps(package))
    with pytest.raises(ValueError):
        validate_package(folder)
    with pytest.raises(ValueError):
        review.import_diagnostic({'study_id': identifier})
    assert not review.snapshot()['jobs']


def test_diagnostics_cannot_evict_existing_balance_or_reference_jobs(staged):
    review, identifier, _, _ = staged
    for kind in ('study', 'balance', 'reference', 'suite', 'nodes'):
        key = hashlib.md5(kind.encode()).hexdigest()
        (review.root/key).mkdir()
        reports = {'MIX': {'kind': 'wcfi_rehearsal_suite', 'passages': []}} if kind == 'suite' else {}
        if kind == 'reference':
            reports = {'MIX': {'kind': 'wcfi_reference_study'}}
        review.jobs[key] = {'id': key, 'source': kind, 'status': 'error', 'reports': reports}
        review._save(review.jobs[key])
    # Pruning operates on feature-owned folders; avoid rendering synthetic empty reports.
    before = set(review.jobs)
    review.import_diagnostic({'study_id': identifier})
    assert set(review.jobs) == before | {identifier}


def test_twelve_diagnostics_fit_and_thirteenth_only_evicts_oldest_diagnostic(staged):
    review, identifier, _, _ = staged
    review.import_diagnostic({'study_id': identifier})
    other = 'c'*32
    (review.root/other).mkdir()
    review.jobs[other] = {'id': other, 'source': 'study', 'status': 'ready', 'reports': {}}
    review._save(review.jobs[other])
    for index in range(12):
        key = f'{index:032x}'
        (review.root/key).mkdir()
        review.jobs[key] = {'id': key, 'source': 'diagnostic', 'status': 'ready', 'reports': {}}
        review._save(review.jobs[key]);review._prune()
        assert sum(j['source'] == 'diagnostic' for j in review.jobs.values()) == min(index+2, 12)
        assert other in review.jobs
    assert identifier not in review.jobs


def test_diagnostic_import_and_audio_require_authentication_and_opt_in_visibility(staged):
    review, identifier, _, _ = staged
    server = EmulatorServer(SimpleNamespace(state_dir=review.root.parent, library_root=review.library_root))
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, *, data=None, authorized=True):
        headers = {'Authorization': 'Bearer '+server.token} if authorized else {}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        return opener.open(urllib.request.Request(server.origin+path, headers=headers,
            data=json.dumps(data).encode() if data is not None else None), timeout=5)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request('/api/rehearsal/import-diagnostic', data={'study_id': identifier}, authorized=False)
        assert exc.value.code == 403
        with request('/api/rehearsal/import-diagnostic', data={'study_id': identifier}) as response:
            assert json.load(response)['jobs'][0]['id'] == identifier
        with request('/api/rehearsal') as response:
            assert not json.load(response)['jobs']
        with request('/api/rehearsal?kit_diagnostics=1') as response:
            assert json.load(response)['jobs'][0]['source'] == 'diagnostic'
        path = f'/api/rehearsal/audio?job={identifier}&track=KIT&variant=diagnostic_raw_matched'
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(path, authorized=False)
        assert exc.value.code == 403
        with request(path) as response:
            assert response.read().startswith(b'RIFF')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
