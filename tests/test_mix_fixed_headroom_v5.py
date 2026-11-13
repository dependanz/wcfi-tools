"""Regression tests for offline fixed master trim; no user audio or host actions."""
from __future__ import annotations

import json
import shutil

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio import mix_candidate_explorer_v3 as v3
from wcfi_tools.audio import mix_fault_authority_v4 as v4
from wcfi_tools.audio import mix_fixed_headroom_v5 as v5
from wcfi_tools.audio import mix_objective_audit as v1


def base():
    return {t: 6. if t == 'RED' else -15. if t == 'KIT' else 0. for t in v1.TRACKS}


def test_clean_reference_trim_is_fixed_and_attenuates():
    assert v5.trim_from_clean(-17.9) == pytest.approx(-6.1)
    assert v5.trim_from_clean(-24) == 0.
    assert v5.trim_from_clean(-19) == -5


def test_master_refuses_boost_and_extreme_trim():
    for clean, target in [(-30,-24),(1,-24),(-20,-10),(-20,-40)]:
        with pytest.raises(ValueError):v5.trim_from_clean(clean,target)
    for val in [float('nan'),float('inf'),True]:
        with pytest.raises(ValueError):v5.trim_from_clean(val)


def test_predictions_preserve_fault_identity_without_renormalization():
    original={'true_peak_dbtp': 0.5, 'integrated_lufs': -17.8}
    result=v5.scalar_prediction(original,-5)
    assert result['predicted_true_peak_dbtp']==pytest.approx(-4.5)
    assert result['predicted_integrated_lufs']==pytest.approx(-22.8)
    assert result['predicted_headroom_to_guardband_db']==pytest.approx(1.)
    assert original['true_peak_dbtp']==0.5


def test_stress_prediction_matches_known_concrete_v4_case():
    assert v5.scalar_prediction({'true_peak_dbtp':1.,'integrated_lufs':-16.9},-6.2)['predicted_true_peak_dbtp']==pytest.approx(-5.2)
    assert v5.scalar_prediction({'true_peak_dbtp':-1.2,'integrated_lufs':-18.4},-5.)['predicted_true_peak_dbtp']==pytest.approx(-6.2)


def fixture_reports(tmp_path):
    paths={}
    for t in v1.TRACKS:
        path=tmp_path/(t.replace(' ','_')+'.wav')
        sf.write(path,np.zeros(4800,dtype=np.float32),48000,subtype='FLOAT')
        st=path.stat()
        paths[t]={'path':str(path),'size':st.st_size,'mtime_ns':st.st_mtime_ns}
    audit={'sources':[{'track':t,'source_path':paths[t]['path']} for t in v1.TRACKS]}
    audit_path=tmp_path/'audit.json';audit_path.write_text(json.dumps(audit))
    trial={'protocol':{'source_audit_sha256':v5.digest(audit_path),
                       'coordinator_settings':{'baseline_gains_db':base()},
                       'evaluation_source_frames':[0,5760000]},
           'source_provenance':paths}
    trial_path=tmp_path/'trial.json';trial_path.write_text(json.dumps(trial))
    verified={t:{'verified_sha256':'sha-'+t} for t in v1.TRACKS}
    candidates={}
    frontier=['red_m1_5_kit_p0_0']
    for n,g in v3.build_variants(base()).items():
        candidates[n]={'gains_db':{t:float(g[v1.INDEX[t]]) for t in v1.TRACKS},
                       'raw_safe':True,'raw_safe_pareto_status':'nondominated',
                       'automatically_eligible':False,
                       'r128':{'status':'measured','integrated_lufs':-18.,'true_peak_dbtp':-5.0}}
    v3r={'protocol':{'kind':'seven_stem_offline_gain_candidate_explorer_v3',
                    'host_writes':0,'no_audio_output':True,'automatic_gain_decision_made':False,
                    'report_sha256':v5.digest(trial_path),
                    'source_audit_sha256':v5.digest(audit_path)},
         'verified_sources':verified,'candidates':candidates,
         'raw_safe_pareto_frontier':frontier}
    v3_path=tmp_path/'v3.json';v3_path.write_text(json.dumps(v3r))
    v4r={'protocol':{'kind':'seven_stem_fault_authority_audit_v4',
                    'host_writes':0,'audio_files_written':0,
                    'automatic_gain_decision_made':False,
                    'v3_sha256':v5.digest(v3_path),'trial_sha256':v5.digest(trial_path),
                    'audit_sha256':v5.digest(audit_path),
                    'hard_true_peak_ceiling_dbtp':-3.,'exploratory_guardband_db':.5,
                    'injected_fault_db':10.,'sustained_fault_seconds':[20,50]},
         'frontier':frontier,'scenarios':list(v4.SCENARIOS),
         'candidate_results':{frontier[0]:{
             'baseline_v3_true_peak_dbtp':-5.,
             'fault_scenarios':{s:{'status':'measured','true_peak_dbtp':-1.,
                                   'integrated_lufs':-17.,'no_audio_file_written':True}
                                for s in v4.SCENARIOS}}}}
    v4_path=tmp_path/'v4.json';v4_path.write_text(json.dumps(v4r))
    return trial_path,audit_path,v3_path,v4_path,paths,verified


def test_frozen_master_run_outputs_only_json_and_does_not_choose(tmp_path,monkeypatch):
    tf,af,vf,ff,paths,verified=fixture_reports(tmp_path)
    monkeypatch.setattr(v1,'_check_source_files',lambda _:verified)
    monkeypatch.setattr(v1,'_source_windows',lambda _:(48000,0,0,5760000,paths))
    seen=[]
    def scan(_readers,_gains,scenario,trim,**kwargs):
        seen.append((scenario,trim))
        return {'status':'measured','integrated_lufs':-23.,
                'true_peak_dbtp':-7.,'sample_peak_dbfs':-7.05,
                'sample_peak_guardband_exceeding_100ms_bins':0,
                'first_sample_peak_guardband_exceedance_seconds':None,
                'no_audio_file_written':True}
    monkeypatch.setattr(v5,'scan_trimmed',scan)
    out=v5.run(tf,af,vf,ff,af,tmp_path/'out')
    assert out['all_tested_faults_meet_fixed_trim_guardband']
    assert all(t==-6 for _,t in seen)
    assert [n for n,_ in seen]==list(v4.SCENARIOS)
    assert out['automatic_selection'] is None
    assert out['protocol']['host_writes']==0
    assert list((tmp_path/'out').iterdir())==[tmp_path/'out'/'summary.json']
    assert len(list(tmp_path.glob('*.wav')))==7
    with pytest.raises(ValueError,match='already exist'):
        v5.run(tf,af,vf,ff,af,tmp_path/'out')


def test_prior_hash_tamper_rejected_before_new_output(tmp_path,monkeypatch):
    tf,af,vf,ff,_paths,verified=fixture_reports(tmp_path)
    monkeypatch.setattr(v1,'_check_source_files',lambda _:verified)
    v4r=json.loads(ff.read_text())
    v4r['protocol']['v3_sha256']='0'*64
    ff.write_text(json.dumps(v4r))
    with pytest.raises(ValueError, match='precise v3'):
        v5.run(tf,af,vf,ff,af,tmp_path/'refused')
    assert not (tmp_path/'refused').exists()


def test_true_peak_mismatch_refused_before_write(tmp_path,monkeypatch):
    tf,af,vf,ff,paths,verified=fixture_reports(tmp_path)
    monkeypatch.setattr(v1,'_check_source_files',lambda _:verified)
    monkeypatch.setattr(v1,'_source_windows',lambda _:(48000,0,0,5760000,paths))
    monkeypatch.setattr(v5,'scan_trimmed',lambda *a,**kw:{
        'status':'measured','integrated_lufs':-23.,'true_peak_dbtp':-3.,
        'no_audio_file_written':True})
    with pytest.raises(ValueError,match='true-peak mismatch'):
        v5.run(tf,af,vf,ff,af,tmp_path/'refused')
    assert not (tmp_path/'refused').exists()


def test_real_ffmpeg_trim_scan_on_small_synthetic_stems(tmp_path):
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg: pytest.skip('FFmpeg unavailable')
    rate=48000
    t=np.arange(rate,dtype=np.float32)/rate
    sources={}
    for track in v1.TRACKS:
        x=.001*np.sin(2*np.pi*440*t) if track=='RED' else np.zeros(rate,dtype=np.float32)
        path=tmp_path/(track.replace(' ','_')+'.wav')
        sf.write(path,x,rate,subtype='FLOAT')
        sources[track]=path
    from contextlib import ExitStack
    with ExitStack() as st:
        readers={tr:st.enter_context(sf.SoundFile(pa)) for tr,pa in sources.items()}
        gains=np.zeros(len(v1.TRACKS),dtype=np.float64)
        normal=v5.scan_trimmed(readers,gains,'uncontrolled_10db_step',0,start=0,ffmpeg=ffmpeg,n_bins=10)
        reduced=v5.scan_trimmed(readers,gains,'uncontrolled_10db_step',-6,start=0,ffmpeg=ffmpeg,n_bins=10)
    assert normal['no_audio_file_written'] and reduced['no_audio_file_written']
    assert reduced['true_peak_dbtp']==pytest.approx(normal['true_peak_dbtp']-6,abs=.11)
    assert reduced['integrated_lufs']==pytest.approx(normal['integrated_lufs']-6,abs=.15)
    assert not list(tmp_path.glob('*.raw'))
