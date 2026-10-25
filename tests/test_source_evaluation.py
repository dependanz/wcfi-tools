"""Tests of source identity, synchronization, silence and gain invariance."""
import json

import numpy as np
import pytest

from wcfi_tools.audio.source_evaluation import ROLES, SourceBank, assess_ranges, fit_ranges
from wcfi_tools.soundcheck.source_aware import annotation_features, annotation_role


def fixture():
    rng = np.random.default_rng(122)
    t = np.arange(64000)/16000
    raw = {'RED': (.03*np.sin(2*np.pi*900*t))[:, None].repeat(2, axis=1).astype('float32'),
        'KIT': rng.normal(0,.02,(64000,2)).astype('float32'),
        'BASS': (.02*np.sin(2*np.pi*90*t))[:, None].repeat(2,axis=1).astype('float32')}
    return raw, {'RED': 'vocals', 'KIT': 'drums', 'BASS': 'bass'}


def test_common_gain_invariant_and_source_gain_changes_role_measurement():
    raw, roles = fixture()
    bank = SourceBank(raw, roles, 16000)
    before = bank.measure(raw, master_pre=sum(raw.values()))
    quiet = bank.measure({k:v*.125 for k,v in raw.items()})
    assert np.allclose(before['features'],quiet['features'],atol=1e-5)
    changed = bank.measure({k:v*(.1 if k=='RED' else 1) for k,v in raw.items()})
    assert changed['roles']['vocals']['presence']['relative_db'] == pytest.approx(
        before['roles']['vocals']['presence']['relative_db']-20,abs=1e-4)
    assert before['reconstruction_relative_power'] < 1e-10


def test_muted_performer_does_not_disable_balance_test():
    raw, roles = fixture()
    bank = SourceBank(raw,roles,16000)
    muted = bank.measure({k:np.zeros_like(v) if k=='RED' else v for k,v in raw.items()})
    assert muted['roles']['vocals']['full']['status']=='measured'
    assert muted['roles']['vocals']['full']['relative_db']==-60
    assert muted['roles']['guitar']['full']['status']=='insufficient_activity'


def test_solo_or_missing_role_is_not_a_balance_failure():
    raw, _ = fixture()
    bank = SourceBank({'RED':raw['RED']},{'RED':'vocals'},16000)
    report = bank.measure({'RED':raw['RED']})
    assert not any(report['valid'])
    model = {'limits':[{'name':n,'lower':-5,'upper':5} for n in report['feature_names']]}
    assert assess_ranges(report,model)['anomaly_cost'] is None


def test_reject_missing_routing_wrong_roles_timing_nonfinite():
    raw, roles = fixture()
    with pytest.raises(ValueError, match='role'):
        SourceBank(raw,{k:'singer' for k in raw},16000)
    bank = SourceBank(raw,roles,16000)
    with pytest.raises(ValueError,match='reconstruct'):
        bank.measure(raw,master_pre=sum(raw.values())*.5)
    with pytest.raises(ValueError,match='synchronized'):
        bank.measure(raw | {'RED':raw['RED'][:-1]})
    with pytest.raises(ValueError,match='finite'):
        bank.measure(raw | {'RED':np.full_like(raw['RED'],np.nan)})
    with pytest.raises(ValueError,match='IDs'):
        bank.measure({'RED':raw['RED']})


def test_bleed_warns_cannot_make_physical_source_claim():
    raw, _ = fixture()
    bank = SourceBank({'RED':raw['RED'],'KIT':raw['RED'].copy()},{'RED':'vocals','KIT':'drums'},16000)
    report = bank.measure({'RED':raw['RED'],'KIT':raw['RED']})
    assert report['warnings'][0]['correlation'] > .99
    assert not report['auto_apply_allowed']
    assert report['professional_quality_judgment'] is None


def test_fit_range_folds_need_distinct_training_songs():
    raw, roles = fixture()
    bank = SourceBank(raw,roles,16000)
    report=bank.measure(raw)
    with pytest.raises(ValueError,match='distinct'):
        fit_ranges([report]*5,training_groups=['same']*5)
    model=fit_ranges([report]*5,training_groups=list('abcde'))
    assert assess_ranges(report,model)['anomaly_cost']==0
    bad=bank.measure({k:v*(100 if k=='RED' else 1) for k,v in raw.items()})
    assessed=assess_ranges(bad,model)
    assert assessed['anomaly_cost']>0
    assert any(a['feature']=='vocals_full_relative_db' and a['direction']=='dominant' for a in assessed['alerts'])
    assert set(model['training_groups'])==set('abcde')


def test_annotation_proxy_is_gain_invariant_roles_explicit():
    rows=[{'track_instrument_type':r,'track_instrument_subtype':s,
        'track_audio_lufs':v,'parameters':json.dumps({'gain':0})}
        for r,s,v in [('VOCALS','LEAD',-20),('GUITARS','ACOUSTIC',-24),('DRUMS','SNARE',-25)]]
    a=annotation_features(rows)
    b=annotation_features([r | {'parameters':json.dumps({'gain':-12})} for r in rows])
    assert np.allclose(a,b,atol=1e-10)
    assert annotation_role(rows[0])=='lead'
    assert annotation_role(rows[1])=='guitar'
    assert annotation_role(rows[0] | {'track_instrument_subtype':'ELECTRIC'})=='vocals_unknown'
    assert 'guitar' in ROLES
    with pytest.raises(ValueError,match='Nonfinite'):
        annotation_features([rows[0] | {'track_audio_lufs':float('nan')}])
