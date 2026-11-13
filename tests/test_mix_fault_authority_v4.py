"""WCFI v4: offline KIT level fault stress and input provenance regression tests."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.audio import mix_candidate_explorer_v3 as v3
from wcfi_tools.audio import mix_fault_authority_v4 as v4
from wcfi_tools.audio import mix_objective_audit as v1


def base():
    return {t: 6. if t == 'RED' else -15. if t == 'KIT' else 0. for t in v1.TRACKS}


def test_correction_knows_onset_only_in_hypothetical_clairvoyant_case():
    scenario='clairvoyant_slew_limited_maxcut'
    assert v4.controller_correction(19.9,scenario)==0
    assert v4.controller_correction(20,scenario)==0
    assert v4.controller_correction(21,scenario)==-.5
    assert v4.controller_correction(26,scenario)==-3
    assert v4.controller_correction(40,scenario)==-3
    assert v4.controller_correction(50,scenario)==-3
    assert v4.controller_correction(53,scenario)==-1.5
    assert v4.controller_correction(56,scenario)==0
    assert v4.validate_controller_contract()


def test_injection_is_transient_or_sustained_as_labeled():
    assert v4.injection_gain(19.99,'uncontrolled_10db_step')==0
    assert v4.injection_gain(20,'uncontrolled_10db_step')==10
    assert v4.injection_gain(49.99,'uncontrolled_10db_step')==10
    assert v4.injection_gain(50,'uncontrolled_10db_step')==0
    assert v4.injection_gain(20.05,'isolated_100ms_hit_uncontrolled')==10
    assert v4.injection_gain(20.2,'isolated_100ms_hit_uncontrolled')==0
    with pytest.raises(ValueError):v4.injection_gain(20,'nonexistent')


def test_mix_carries_exact_zero_before_fault_and_kit_offset_at_fault():
    gains=np.zeros(7);blocks=[np.zeros((v4.BIN,1)) for _ in v1.TRACKS]
    blocks[v1.INDEX['KIT']][:]=.01
    original=v4.mix_block(blocks,gains,'uncontrolled_10db_step',0)
    assert np.allclose(original,0.01)
    fault=v4.mix_block(blocks,gains,'uncontrolled_10db_step',200)
    assert np.allclose(fault,.01*10**(10/20))
    oracle=v4.mix_block(blocks,gains,'instant_maxcut_noncausal_upper_authority',200)
    assert np.allclose(oracle,.01*10**(7/20))
    assert not np.shares_memory(blocks[6],fault)


def test_causal_ramp_has_no_jump_at_20_and_stops_at_minus3():
    gains=np.zeros(7);blocks=[np.zeros((v4.BIN,2)) for _ in v1.TRACKS]
    blocks[v1.INDEX['KIT']][:]=.01
    a=v4.mix_block(blocks,gains,'clairvoyant_slew_limited_maxcut',200)
    assert a[0,0]==pytest.approx(.01*10**(10/20),abs=1e-7)
    assert a[-1,0]<a[0,0]
    at26=v4.mix_block(blocks,gains,'clairvoyant_slew_limited_maxcut',260)
    assert at26[0,0]==pytest.approx(.01*10**(7/20),abs=1e-7)
    assert (np.diff(a[:,0])<=1e-10).all()


def test_invalid_mix_pcm_and_scenario_rejected():
    b=[np.zeros((v4.BIN,1)) for _ in v1.TRACKS]
    with pytest.raises(ValueError):v4.mix_block(b[:-1],np.zeros(7),'uncontrolled_10db_step',0)
    b[0][0]=float('nan')
    with pytest.raises(ValueError):v4.mix_block(b,np.zeros(7),'uncontrolled_10db_step',0)
    b[0][0]=0.
    with pytest.raises(ValueError):v4.mix_block(b,np.zeros(7),'bad',0)


def test_parse_ebur128_requires_final_summary_and_finite_values():
    log='noise\nSummary:\n Integrated loudness:\n I: -23.5 LUFS\n True peak:\n Peak: -4.1 dBFS\n'
    assert v4.parse_r128_summary(log)=={'integrated_lufs': -23.5, 'true_peak_dbtp': -4.1}
    with pytest.raises(ValueError,match='summary'):
        v4.parse_r128_summary('no summary')
    with pytest.raises(ValueError,match='missing'):
        v4.parse_r128_summary('Summary: I: -23.5 LUFS')


def test_prior_source_and_grid_mismatch_refused():
    trial={'protocol':{'coordinator_settings':{'baseline_gains_db':base()}}}
    verified={t:{'verified_sha256':'sha-'+t} for t in v1.TRACKS}
    candidates={}
    for n,g in v3.build_variants(base()).items():
        candidates[n]={'gains_db':{t:float(g[v1.INDEX[t]]) for t in v1.TRACKS},
                       'raw_safe':True,'raw_safe_pareto_status':'nondominated',
                       'automatically_eligible':False}
    summary={'protocol':{'kind':'seven_stem_offline_gain_candidate_explorer_v3',
                         'host_writes':0,'no_audio_output':True,'automatic_gain_decision_made':False,
                         'source_audit_sha256':hashlib.sha256(b'audit').hexdigest()},
             'verified_sources':{t:dict(v) for t,v in verified.items()},
             'candidates':candidates, 'raw_safe_pareto_frontier':['red_m1_5_kit_p0_0']}
    names,_=v4.verify_prior(summary,trial,b'audit',verified)
    assert names==['red_m1_5_kit_p0_0']
    summary['verified_sources']['RED']['verified_sha256']='wrong'
    with pytest.raises(ValueError,match='SHA mismatch'):
        v4.verify_prior(summary,trial,b'audit',verified)


def test_mock_orchestration_creates_only_json_and_preserves_sources(tmp_path,monkeypatch):
    paths={}
    for track in v1.TRACKS:
        path=tmp_path/(track.replace(' ','_')+'.wav')
        sf.write(path,np.zeros(4800),48000,subtype='FLOAT')
        st=path.stat()
        paths[track]={'path':str(path),'size':st.st_size,'mtime_ns':st.st_mtime_ns}
    audit={'sources':[{'track':t,'source_path':paths[t]['path']} for t in v1.TRACKS]}
    af=tmp_path/'audit.json';af.write_text(json.dumps(audit))
    trial={'protocol':{'source_audit_sha256':hashlib.sha256(af.read_bytes()).hexdigest(),
                        'coordinator_settings':{'baseline_gains_db':base()},
                        'evaluation_source_frames':[0,5760000]},
           'source_provenance':paths}
    tf=tmp_path/'trial.json';tf.write_text(json.dumps(trial))
    verified={t:{'verified_sha256':'sha-'+t} for t in v1.TRACKS}
    candidates={}
    for n,g in v3.build_variants(base()).items():
        candidates[n]={'gains_db':{t:float(g[v1.INDEX[t]]) for t in v1.TRACKS},
                       'raw_safe':True,'raw_safe_pareto_status':'nondominated',
                       'automatically_eligible':False,
                       'r128':{'true_peak_dbtp':-5.0}}
    summary={'protocol':{'kind':'seven_stem_offline_gain_candidate_explorer_v3',
                         'host_writes':0,'no_audio_output':True,'automatic_gain_decision_made':False,
                         'report_sha256':hashlib.sha256(tf.read_bytes()).hexdigest(),
                         'source_audit_sha256':hashlib.sha256(af.read_bytes()).hexdigest()},
             'verified_sources':verified,'candidates':candidates,
             'raw_safe_pareto_frontier':['red_m1_5_kit_p0_0']}
    vf=tmp_path/'v3.json';vf.write_text(json.dumps(summary))
    monkeypatch.setattr(v1,'_check_source_files',lambda report:verified)
    monkeypatch.setattr(v1,'_source_windows',lambda report:(48000,0,0,5760000,paths))
    seen=[]
    def scan(readers,gains,scenario,**kwargs):
        seen.append(scenario)
        return {'status':'measured','integrated_lufs':-20.,'true_peak_dbtp':-6.,
                'raw_true_peak_meets_guardband':True,'no_audio_file_written':True}
    monkeypatch.setattr(v4,'scan_stress',scan)
    out=v4.run(tf,vf,af,af,tmp_path/'result')
    assert seen==list(v4.SCENARIOS)
    assert out['automatic_selection'] is None
    assert out['protocol']['audio_files_written']==0
    assert list((tmp_path/'result').iterdir()) == [tmp_path/'result'/'summary.json']
    assert len(list(tmp_path.glob('*.wav')))==7
    with pytest.raises(ValueError,match='fresh output'):
        v4.run(tf,vf,af,af,tmp_path/'result')
    modified=json.loads(vf.read_text());modified['protocol']['report_sha256']='0'*64
    vf.write_text(json.dumps(modified))
    with pytest.raises(ValueError,match='digest mismatch'):
        v4.run(tf,vf,af,af,tmp_path/'refuse')
    assert not (tmp_path/'refuse').exists()
