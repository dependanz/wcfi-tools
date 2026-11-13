"""V2 source-only observation tests, no church recordings or host access."""
from __future__ import annotations

import json
import subprocess
import sys

import numpy as np
import pytest

from wcfi_tools.audio.mix_objective_audit import (
    BANDS_HZ,
    INDEX,
    TRACKS,
    _variants,
    evaluate_objectives,
)
from wcfi_tools.audio.mix_objective_audit_v2 import (
    BANDS,
    HOP,
    _scan_ffmpeg_pipe,
    capture_section_attack_margins,
    kit_flux_10ms,
    pareto_descriptive,
    select_local_events,
    source_saturation,
    vocal_channel_evidence,
)


class Reader:
    def __init__(self, x):
        self.wave=np.asarray(x,dtype='float32')
        if self.wave.ndim==1:self.wave=self.wave[:,None]
        self.channels=self.wave.shape[1]
        self.pos=0
        self.seeks=[]
    def read(self,n,dtype='float32',always_2d=True):
        ret=self.wave[self.pos:self.pos+n]
        self.pos+=len(ret)
        return ret
    def seek(self,frame):
        if frame<0:raise ValueError('Negative seek')
        self.pos=frame
        self.seeks.append(frame)


def test_silent_source_does_not_fabricate_attacks():
    r=Reader(np.zeros((48000,2)))
    flux,energy=kit_flux_10ms(r,48000)
    found=select_local_events(flux,energy)
    assert all(x['union_count']==0 for x in found.values())
    assert all(len(flux[name])==100 for name in BANDS)


def test_local_bridge_detection_does_not_depend_on_global_loudness():
    n=12000
    flux={k:np.zeros(n) for k in BANDS}
    energy=np.ones(n)*1e-6
    # Three large early attacks, then low-amplitude bridge attacks.
    for i in (2000,2600,3400):flux['low_40_180'][i]=100
    for i in (5100,5600,6200,6600,7000,7700):flux['low_40_180'][i]=0.3
    for i in (5200,5900,6700,7800):flux['high_800_7000'][i]=0.5
    detected=select_local_events(flux,energy)
    bridge=detected['bridge_approx_50_82s']
    assert bridge['by_band']['low_40_180']['candidate_count']>=5
    assert bridge['by_band']['high_800_7000']['candidate_count']==4
    assert bridge['union_count']>=6


def test_minimum_event_spacing_and_no_activity_claim():
    flux={k:np.zeros(12000) for k in BANDS}
    for k in BANDS:flux[k][5100]=5;flux[k][5104]=4;flux[k][5300]=3
    energy=np.ones(12000)*1e-8
    out=select_local_events(flux,energy)
    a=out['bridge_approx_50_82s']['by_band']['low_40_180']['selected_hop_indices']
    assert len(a)==2
    assert a[1]-a[0]>=8
    assert not any(i in (5101,5102,5103) for i in a)


def test_10ms_onset_hop_with_causal_window():
    rate=48000
    s=np.zeros((HOP*40,1),dtype=np.float32)
    s[HOP*15:HOP*16]=0.4*np.sin(2*np.pi*120*np.arange(HOP)/rate)[:,None]
    flux,energy=kit_flux_10ms(Reader(s),len(s))
    assert np.max(flux['low_40_180'][:15])==0
    assert np.any(flux['low_40_180'][15:]>0)
    assert len(energy)==40


def test_saturation_counts_and_run_across_read_chunks():
    frames=48000*2
    x=np.zeros((frames,2),dtype='float32')
    x[47998:48003,0]=1.0
    x[49000,1]=-1
    r=Reader(x)
    res=source_saturation(r,frames)
    assert res['full_scale_sample_count']==6
    assert res['max_consecutive_full_scale_frames']==5
    assert res['full_scale_sample_count_by_channel']==[5,1]
    assert res['potential_input_saturation']
    assert not res['clipping_or_source_origin_proven']


def test_vocal_source_evidence_never_identifies_singing():
    t=np.arange(4800*12)/48000
    voice=(0.04*np.sin(2*np.pi*440*t)).astype(np.float32)
    streams={'RED':Reader(voice),'YELLOW':Reader(voice*.15),'GREEN':Reader(np.zeros_like(voice))}
    res=vocal_channel_evidence(streams,12,threshold_red=-50)
    assert res['YELLOW']['speechband_spectral_similarity_median']>0.99
    assert res['YELLOW']['independent_singer_verified'] is False
    assert res['YELLOW']['safe_to_auto_boost'] is False
    assert res['GREEN']['independent_singer_verified'] is False


def test_segment_attack_margin_reads_correct_evaluation_offset():
    rate=48000
    offset=rate*2
    n=rate*4
    y=np.zeros((offset+n,1),dtype='float32')
    y[offset+rate:offset+rate+3840]=0.5*np.sin(2*np.pi*100*np.arange(3840)/rate)[:,None]
    readers={t:Reader(y.copy()) for t in TRACKS}
    events={name:{'by_band':{band:{'selected_hop_indices':[]} for band in BANDS}}
             for name,_,_ in [('before_0_20s',0,20),('main_20_50s',20,50),('bridge_approx_50_82s',50,82),('later_82_120s',82,120)]}
    # Point at 1.08 sec: the selected trailing 80ms window is positive only at the right offset.
    events['before_0_20s']['by_band']['low_40_180']['selected_hop_indices']=[107]
    v=_variants({t:0 for t in TRACKS})
    res=capture_section_attack_margins(readers,events,v,evaluation_start=offset)
    assert res['before_0_20s']['low_40_180']['frozen_static']['count']==1
    assert readers['KIT'].seeks[0]==offset+108*480-3840


def test_missing_true_peak_cannot_be_pareto_ranked():
    f=np.ones((20,7,len(BANDS_HZ)))*1e-5
    p=np.ones((20,7))*1e-5
    p[:,INDEX['RED']]=1e-2
    p[:,INDEX['KIT']]=1e-2
    variants=_variants({t:0 for t in TRACKS})
    scores,_=evaluate_objectives(f,p,variants,{t:-55 for t in TRACKS})
    attack={section:{'high_800_7000':{k:{'p10_db':0} for k in variants}}
            for section in ['before_0_20s','main_20_50s','bridge_approx_50_82s','later_82_120s']}
    ranked=pareto_descriptive(scores,attack,{k:None for k in variants})
    assert all(x['pareto_status']=='insufficient_evidence' for x in ranked.values())
    assert all(not x['automatic_gain_decision_allowed'] for x in ranked.values())


def test_invalid_inputs_fail_before_creating_output(tmp_path):
    cmd=[sys.executable,'-m','wcfi_tools.audio.mix_objective_audit_v2',
         '--report',str(tmp_path/'missing.json'),'--output',str(tmp_path/'never')]
    r=subprocess.run(cmd,capture_output=True,text=True, check=False)
    assert r.returncode!=0
    assert not (tmp_path/'never').exists()


def test_ffmpeg_r128_from_pipe_no_audio_file(tmp_path):
    import shutil
    ffmpeg=shutil.which('ffmpeg')
    if ffmpeg is None:pytest.skip('ffmpeg not installed')
    n=48000
    t=np.arange(n)/48000
    sine=0.1*np.sin(2*np.pi*1000*t).astype('float32')
    streams={name:Reader(sine if name=='RED' else np.zeros_like(sine)) for name in TRACKS}
    variants={'signal':np.zeros(7)}
    res=_scan_ffmpeg_pipe(streams,variants,ffmpeg=ffmpeg,frames=n,evaluation_start=0)
    assert res['signal']['status']=='measured'
    assert -40<res['signal']['integrated_lufs']<-10
    assert -30<res['signal']['true_peak_dbtp']<-10
    assert list(tmp_path.iterdir())==[]


def test_full_synthetic_offline_run_verifies_sources_and_writes_only_json(tmp_path,monkeypatch):
    """Short synthetic fixture with the same seven sources; no church material."""
    import hashlib
    import shutil

    import soundfile as sf

    from wcfi_tools.audio import mix_objective_audit as v1
    from wcfi_tools.audio import mix_objective_audit_v2 as v2
    rate=48000
    cal_end=rate
    length=rate*3
    tracks={}
    t=np.arange(cal_end+length)/rate
    for name in TRACKS:
        x=(0.006*np.sin(2*np.pi*440*t)).astype('float32')
        if name=='KIT':
            x=(0.006*np.sin(2*np.pi*120*t)).astype('float32')
            for tick in range(cal_end+1200,cal_end+length,12000):
                x[tick:tick+300]+=np.hanning(300).astype('float32')*.08
        if name in ('GREEN','YELLOW'):
            x*=0.1
        path=tmp_path/f'{name}.wav'
        sf.write(path,x,rate,subtype='FLOAT')
        st=path.stat()
        with sf.SoundFile(path) as r:
            decoded=r.read(cal_end+length,dtype='float32',always_2d=True)
        tracks[name]={"path":str(path),"size":st.st_size,"mtime_ns":st.st_mtime_ns,
            "channels":1,"frames":len(x),
            "decoded_window_sha256":hashlib.sha256(decoded.astype('<f4').tobytes()).hexdigest(),
            "source_full_scale_samples":0}
    report={"protocol":{"tracks":list(TRACKS),"source_date":"2026-10-04",
                "sample_rate":rate,"calibration_source_frames":[0,cal_end],
                "evaluation_source_frames":[cal_end,cal_end+length],
                "coordinator_settings":{"baseline_gains_db":{t:0 for t in TRACKS},
                    "activity_thresholds_dbfs":{t:-55 for t in TRACKS}},
                "source_audit_sha256":"unused"},
            "source_provenance":tracks}
    rep=tmp_path/'trial.json'
    rep.write_text(json.dumps(report),encoding='utf-8')
    # Retain the production v1 freeze; override only the 120s fixture limit.
    real_windows=v1._source_windows
    def windows(r):
        if r is report or r.get('protocol',{}).get('evaluation_source_frames')==[cal_end,cal_end+length]:
            return rate,0,cal_end,cal_end+length,r['source_provenance']
        return real_windows(r)
    monkeypatch.setattr(v1,'_source_windows',windows)
    out=tmp_path/'result'
    ffmpeg=shutil.which('ffmpeg')
    summary=v2.run(rep,out,ffmpeg=ffmpeg)
    assert len(summary['verified_sources'])==7
    assert summary['protocol']['host_writes']==0
    assert summary['protocol']['audio_output_files']==0
    assert [x.name for x in out.iterdir()]==['summary.json']
    assert summary['vocal_source_evidence']['YELLOW']['independent_singer_verified'] is False
    assert summary['descriptive_pareto']['frozen_static']['automatic_gain_decision_allowed'] is False
    if ffmpeg:
        assert summary['r128']['frozen_static']['status']=='measured'
