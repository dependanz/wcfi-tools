"""No source-level reference leakage; calibrated geometry and raw activity contracts."""
import copy

import numpy as np
import pytest

from wcfi_tools.audio.balance_calibration import (
    ROLES,
    assess,
    conformal_threshold,
    fit,
    frame_evidence,
    vector,
)


def fixture():
    rng=np.random.default_rng(38)
    raw={r:rng.normal(0,.02,(32000,2)).astype('float32') for r in ROLES}
    return raw,frame_evidence(raw,16000)


def training():
    _,e=fixture()
    rng=np.random.default_rng(7)
    rows=[]
    for i in range(50):
        row=copy.deepcopy(e)
        p=np.array(e['powers'])*10**(rng.normal(0,1.5,4)/10)
        row['powers']=p.tolist()
        rows.append({'id':str(i),'artist':'artist'+str(i),'evidence':row})
    return rows[:25],rows[25:]


def test_finite_sample_calibration_uses_order_statistic_not_interpolation():
    assert conformal_threshold(list(range(20)),.1)==18
    assert conformal_threshold([1,2],.1) is None
    with pytest.raises(ValueError):
        conformal_threshold([float('nan')],.1)


def test_muted_candidate_keeps_raw_role_context_and_gain_invariance():
    raw,e=fixture()
    base=vector(e)
    muted=np.array(e['powers']);muted[:,0]=0
    changed=vector(e,powers=muted)
    assert changed['context']==base['context']
    assert changed['relative_db'][0]==-60
    quiet=frame_evidence({r:a*.25 for r,a in raw.items()},16000)
    assert np.allclose(vector(e,powers=quiet['powers'])['relative_db'],base['relative_db'],atol=1e-5)


@pytest.mark.parametrize('geometry',['axis','covariance','neighbors'])
def test_geometry_detects_large_known_gain_and_preserves_ids(geometry):
    train,calib=training()
    model=fit(train,calib,geometry=geometry,alpha=.1)
    _,e=fixture()
    original=assess(vector(e),model)
    p=np.array(e['powers']);p[:,0]*=100
    bad=assess(vector(e,powers=p),model)
    assert bad['score']>original['score']
    assert bad['alert']
    assert set(model['training_ids']).isdisjoint(model['calibration_ids'])
    assert not bad['auto_apply_allowed']
    # Raw-relative source levels are never part of a prediction: only fixed
    # activity/context and ACTUAL candidate contribution powers are used.
    altered=copy.deepcopy(e);altered['powers']=(np.array(e['powers'])*[100,.1,5,2]).tolist()
    assert vector(altered,powers=p)==vector(e,powers=p)


def test_no_song_or_artist_leakage():
    train,calib=training()
    with pytest.raises(ValueError,match='Song leakage'):
        fit(train,calib+[train[0]])
    calib[0]['artist']=train[0]['artist']
    with pytest.raises(ValueError,match='Artist leakage'):
        fit(train,calib)


def test_sparse_arrangement_has_own_calibration_and_single_role_abstains():
    train,calib=training()
    model=fit(train,calib)
    _,e=fixture()
    sparse=vector(e,context=['vocals','other'])
    assert assess(sparse,model)['status']!='uncalibrated_context'
    with pytest.raises(ValueError,match='two supported'):
        vector(e,context=['vocals'])
    unsupported=assess({'context':['guitar','vocals'],'relative_db':[0,0]},model)
    assert unsupported['alert'] is None


def test_source_alignment_and_nonfinite_data_rejected():
    raw,_=fixture()
    with pytest.raises(ValueError,match='synchronized'):
        frame_evidence(raw | {'vocals':raw['vocals'][:-1]},16000)
    with pytest.raises(ValueError,match='finite'):
        frame_evidence(raw | {'vocals':np.full_like(raw['vocals'],np.nan)},16000)


def test_unknown_originals_are_not_counted_as_clean():
    from wcfi_tools.soundcheck.balance_validation import summarize

    rows=[{'new':{'alert':None},'variants':[]},{'new':{'alert':True},'variants':[]}]
    summary=summarize(rows,'new')
    assert summary['unknown_originals']==1
    assert summary['assessed_originals']==1
    assert summary['original_alert_fraction']==1.


def test_manifest_accepts_joint_model_and_reports_coarsened_roles(tmp_path):
    import json

    import soundfile as sf

    from wcfi_tools.soundcheck.source_manifest import inspect

    train,calib=training()
    model=fit(train,calib)
    (tmp_path/'model.json').write_text(json.dumps(model),encoding='utf-8')
    raw,_=fixture()
    for role in ('vocals','other'):
        sf.write(tmp_path/(role+'.wav'),raw[role],16000,subtype='FLOAT')
    sf.write(tmp_path/'master.wav',raw['vocals']+raw['other'],16000,subtype='FLOAT')
    spec={'kind':'wcfi_source_contributions_v1','stage':'master_pre_fx','alignment_verified':True,
        'duration_seconds':2,'master_pre':'master.wav','sources':[
            {'id':'RED','role':'vocals','raw':'vocals.wav','contribution':'vocals.wav'},
            {'id':'AC','role':'guitar','raw':'other.wav','contribution':'other.wav'}]}
    path=tmp_path/'sources.json'
    path.write_text(json.dumps(spec),encoding='utf-8')
    result=inspect(path,model=tmp_path/'model.json')
    assert result['joint_balance_assessment']['status']!='uncalibrated_context'
    assert result['joint_evaluation_role_mapping']['guitar']=='other'
    assert not result['joint_balance_assessment']['auto_apply_allowed']
