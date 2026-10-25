"""Validate challenge integrity and honest scoring, not professional mix expertise."""
import builtins
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.soundcheck.master_benchmark import (
    AXES,
    CONDITIONS,
    PRIMARY_DEGRADATIONS,
    interventions,
    prepare,
    summarize,
    validate_blind,
)
from wcfi_tools.soundcheck.profiles import file_hash, write_json


@pytest.fixture(scope='module')
def audio():
    t = np.arange(480000)/48000
    envelope = .55+.45*np.sin(2*np.pi*.75*t)**2
    a = envelope*(.2*np.sin(2*np.pi*220*t)+.05*np.sin(2*np.pi*6000*t))
    return np.column_stack([a, .8*a+.025*np.sin(2*np.pi*330*t)]).astype(np.float32)


def test_challenges_preserve_sources_and_have_known_physical_interventions(audio):
    original = audio.copy()
    versions, facts = interventions(audio, audio*.7, audio*.95)
    np.testing.assert_array_equal(audio, original)
    assert tuple(versions) == CONDITIONS
    np.testing.assert_array_equal(versions['current'], versions['identical'])
    np.testing.assert_array_equal(versions['current'], versions['level_only'])
    assert facts['clip_mild']['sample_fraction'] == pytest.approx(.01, abs=.001)
    assert facts['clip_severe']['sample_fraction'] == pytest.approx(.1, abs=.001)
    difference = versions['hum'].astype(float)-audio
    relative = 10*np.log10(np.mean(difference**2)/np.mean(audio.astype(float)**2))
    assert relative == pytest.approx(-12, abs=.001)
    np.testing.assert_allclose(difference[:, 0], difference[:, 1], atol=3e-8)
    for start, end in facts['dropouts']['windows_seconds']:
        assert np.max(np.abs(versions['dropouts'][round(start*48000)+96:round(end*48000)-96])) == 0
    # A 6 kHz test component is strongly attenuated while the body remains.
    t = np.arange(len(audio))/48000
    before = abs(np.mean(audio[:, 0]*np.exp(-2j*np.pi*6000*t)))
    after = abs(np.mean(versions['lowpass'][:, 0]*np.exp(-2j*np.pi*6000*t)))
    assert after/before < .01
    assert facts['candidate']['human_preference_known'] is False


def test_nonfinite_or_unaligned_audio_is_refused(audio):
    invalid = audio.copy(); invalid[100, 0] = np.nan
    for pre in (invalid, audio[:-1], audio[:, :1]):
        with pytest.raises(ValueError, match='aligned'):
            interventions(audio, pre, audio)


def profile(tmp_path, audio):
    entries = {}
    for stage, samples in [('pre_fx', audio), ('post_fx', audio), ('candidate', audio*.98)]:
        path = tmp_path/(stage+'.wav'); sf.write(path, samples, 48000, subtype='FLOAT')
        entries[stage] = {'file':path.name, 'sha256':file_hash(path)}
    path = tmp_path/'sources.json'
    write_json(path, {'kind':'wcfi_master_benchmark_sources','schema_version':1,
        'sessions':[{'id':'test-session','output_gain_db':-6,**entries}]})
    return path


@pytest.fixture(scope='module')
def prepared(tmp_path_factory, audio):
    import imageio_ffmpeg
    tmp_path=tmp_path_factory.mktemp('master-fixtures')
    cfg = profile(tmp_path, audio); out = tmp_path/'benchmark'
    prepare(cfg, out, ffmpeg=Path(imageio_ffmpeg.get_ffmpeg_exe()), seed=4)
    return out


def test_preparation_blinds_inputs_and_verifies_level_matches(prepared):
    manifest = validate_blind(prepared)
    public = json.dumps(manifest)
    for value in (*CONDITIONS, 'test-session', 'lufs', 'threshold'):
        # 'current' etc must not be leaked through random IDs or filenames.
        assert value not in public
    assert len(manifest['clips']) == 10 and len(manifest['repeat_ids']) == 1
    truth = json.loads((prepared/'private-answer-key.json').read_text())['clips']
    baseline = next(c for c in truth if c['condition']=='current')
    for item in truth:
        assert abs(item['matched_metrics']['integrated_lufs']-item['target_lufs']) <= .15
        assert item['matched_metrics']['true_peak_dbtp'] <= -2.85
        if item['condition'] in ('identical','level_only'):
            assert item['conversion']['sha256'] == baseline['conversion']['sha256']
    for source in json.loads((prepared/'private-answer-key.json').read_text())['sessions']:
        for stage in ('pre_fx','post_fx','candidate'):
            p = prepared.parent/source[stage]['file']
            assert file_hash(p)==source[stage]['sha256']


def test_tampered_inputs_fail_before_loading_torch(prepared, tmp_path, monkeypatch):
    from wcfi_tools.soundcheck import master_benchmark_cpu as cpu
    copied=tmp_path/'benchmark';shutil.copytree(prepared,copied);prepared=copied
    manifest = validate_blind(prepared)
    (prepared/manifest['clips'][0]['model_file']).write_bytes(b'changed')
    monkeypatch.setattr(cpu, 'configure_cpu', lambda: None)
    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name=='torch':
            pytest.fail('Bad inputs must fail before importing torch')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guard)
    with pytest.raises(ValueError, match='changed'):
        cpu.run(prepared, tmp_path/'run', tmp_path/'checkpoint')
    assert not (tmp_path/'run').exists()


def fake_run(directory, output, *, wrong_degradation=False, wrong_null=False):
    manifest = validate_blind(directory)
    key = {c['id']:c for c in json.loads((directory/'private-answer-key.json').read_text())['clips']}
    output.mkdir(); records=[]
    schedule = [(c['id'], False) for c in manifest['clips']]+[(c, True) for c in manifest['repeat_ids']]
    for index, (identifier, repeat) in enumerate(schedule):
        case = key[identifier]; condition=case['condition']
        pq = 5 if condition in PRIMARY_DEGRADATIONS else 6
        if wrong_degradation and condition=='hum':pq=7
        if wrong_null and condition=='identical':pq=6.1
        item = next(c for c in manifest['clips'] if c['id']==identifier)
        filename=f'prediction-{index:04d}.json'
        write_json(output/filename, {'id':identifier,'repeat':repeat,'input_sha256':item['model_sha256'],
            'prediction':{'scores':{a:pq for a in AXES},'elapsed_seconds':.1}})
        records.append({'file':filename,'sha256':file_hash(output/filename)})
    write_json(output/'batch-result.json', {'status':'complete',
        'manifest_sha256':file_hash(directory/'blind-manifest.json'),
        'runtime':{'model':'test-only','device':'cpu'},'timing':{},'predictions':records,
        'gpu_initialized':False,'network_connection_attempts':0,'daw_operations':0})


@pytest.mark.parametrize('degraded,null,passes',[(False,False,True),(True,False,False),(False,True,False)])
def test_gate_cannot_certify_professional_quality(prepared, tmp_path, degraded, null, passes):
    out=tmp_path/'run';fake_run(prepared,out,wrong_degradation=degraded,wrong_null=null)
    result=summarize(prepared,out)
    assert result['coarse_control_gate_passed'] is passes
    assert result['professional_mixing_judgment_validated'] is False
    assert result['automatic_edits_authorized'] is False
    assert result['human_preference_data_present'] is False
    assert result['totals']['coarse_degradation']['total']==4
    assert all(not r['preference_established'] for r in result['candidates'])
    markup=(out/'report.html').read_text()
    assert 'unvalidated' in markup and 'autoplay' not in markup


def test_answer_key_cannot_be_changed_after_inference(prepared,tmp_path):
    copied=tmp_path/'benchmark';shutil.copytree(prepared,copied);prepared=copied
    out=tmp_path/'run';fake_run(prepared,out)
    key=prepared/'private-answer-key.json'
    key.write_text(key.read_text()+' ')
    with pytest.raises(ValueError,match='answer key changed'):
        summarize(prepared,out)


def test_gpu_or_incomplete_run_cannot_be_reported_as_cpu_success(prepared,tmp_path):
    out=tmp_path/'run';fake_run(prepared,out)
    path=out/'batch-result.json';batch=json.loads(path.read_text())
    batch['gpu_initialized']=True;path.write_text(json.dumps(batch))
    with pytest.raises(ValueError,match='CPU-only offline'):
        summarize(prepared,out)
    batch['gpu_initialized']=False;batch['predictions'].pop();path.write_text(json.dumps(batch))
    with pytest.raises(ValueError,match='Missing benchmark predictions'):
        summarize(prepared,out)
    assert not (out/'results.json').exists()


def test_run_refuses_overwrite_before_any_model_or_budget_changes(tmp_path,monkeypatch):
    from wcfi_tools.soundcheck import master_benchmark_cpu as cpu
    output=tmp_path/'existing';output.mkdir()
    monkeypatch.setattr(cpu,'configure_cpu',lambda:pytest.fail('Must refuse before doing work'))
    with pytest.raises(ValueError,match='No automatic retries'):
        cpu.run(tmp_path/'missing',output,tmp_path/'checkpoint')


def test_cpu_listener_thread_budget_validation_precedes_torch_import(tmp_path,monkeypatch):
    from wcfi_tools.soundcheck.quality_listening import AudioboxListener
    original_import=builtins.__import__

    def guard(name,*args,**kwargs):
        if name=='torch':pytest.fail('Invalid budget must not load torch')
        return original_import(name,*args,**kwargs)

    monkeypatch.setattr(builtins,'__import__',guard)
    for invalid in (0,5,True,1.5):
        with pytest.raises(ValueError,match='CPU inference threads'):
            AudioboxListener(tmp_path,cpu_threads=invalid)


def test_worker_blocks_network_only_within_its_context():
    import socket

    from wcfi_tools.soundcheck.master_benchmark_cpu import offline_connections
    original=socket.socket.connect
    with offline_connections() as attempts:
        with socket.socket() as client, pytest.raises(RuntimeError,match='Network access is disabled'):
            client.connect(('127.0.0.1',1))
        assert len(attempts)==1
    assert socket.socket.connect is original
