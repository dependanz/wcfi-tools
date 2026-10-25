"""Saved-file boundary checks; mono raw signals preserve stereo contributions."""
import json

import numpy as np
import pytest
import soundfile as sf

from wcfi_tools.soundcheck.source_manifest import inspect


def make_manifest(folder):
    rate=16000
    t=np.arange(rate*2)/rate
    raw=(.01*np.sin(2*np.pi*800*t)).astype('float32')
    contribution=np.column_stack((raw*.2,raw*.5))
    sf.write(folder/'raw.wav',raw,rate,subtype='FLOAT')
    sf.write(folder/'contribution.wav',contribution,rate,subtype='FLOAT')
    sf.write(folder/'master.wav',contribution,rate,subtype='FLOAT')
    spec={'kind':'wcfi_source_contributions_v1','stage':'master_pre_fx','alignment_verified':True,
        'duration_seconds':2,'master_pre':'master.wav',
        'sources':[{'id':'RED','role':'vocals','raw':'raw.wav','contribution':'contribution.wav'}]}
    path=folder/'manifest.json'
    path.write_text(json.dumps(spec),encoding='utf-8')
    return path,spec


def test_file_manifest_mono_to_stereo_and_reconstruction(tmp_path):
    path,_=make_manifest(tmp_path)
    result=inspect(path)
    assert result['sample_rate']==16000
    assert result['routing_reconstruction_checked']
    assert result['reconstruction_relative_power']<1e-12
    assert len(result['evidence'])==3
    assert not result['auto_apply_allowed']


@pytest.mark.parametrize('update,match',[
    ({'alignment_verified':False},'latency'),
    ({'stage':'master_post_fx'},'Master pre-FX'),
    ({'duration_seconds':float('nan')},'finite'),
    ({'duration_seconds':3},'shorter'),
])
def test_reject_unverified_alignment_nonlin_master_or_window(tmp_path,update,match):
    path,spec=make_manifest(tmp_path)
    path.write_text(json.dumps(spec | update),encoding='utf-8')
    with pytest.raises(ValueError,match=match):
        inspect(path)
