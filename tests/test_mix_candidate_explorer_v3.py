"""No DAW/no audio output: deterministic static candidate screening."""
from __future__ import annotations

import json

import numpy as np
import pytest

from wcfi_tools.audio import mix_candidate_explorer_v3 as v3
from wcfi_tools.audio import mix_objective_audit as v1
from wcfi_tools.audio import mix_objective_audit_v2 as v2


def baseline():
    return {t:(6.0 if t == "RED" else -15.0 if t == "KIT" else 0.0)
            for t in v1.TRACKS}


def test_fixed_nine_candidate_grid_and_no_backing_changes():
    base = baseline()
    variants = v3.build_variants(base)
    assert len(variants) == 9
    assert len(set(variants)) == 9
    for gains in variants.values():
        assert float(gains[v1.INDEX["RED"]]) in (6.0,4.5,3.0)
        assert float(gains[v1.INDEX["KIT"]]) in (-15.0,-13.5,-12.0)
        for t in ("YELLOW","GREEN","EGUITAR","AC GUITAR","KEYS"):
            assert gains[v1.INDEX[t]] == base[t]
    assert np.allclose(variants[next(iter(variants))],np.array([base[t] for t in v1.TRACKS]))


def test_invalid_gain_manifest_fails_closed():
    b=baseline()
    del b["GREEN"]
    with pytest.raises(ValueError,match="seven-stem"):
        v3.build_variants(b)
    b=baseline(); b["RED"]=float("nan")
    with pytest.raises(ValueError,match="Nonfinite"):
        v3.build_variants(b)


def mock_events(energy):
    e={}
    for label, start, end in v2.SECTIONS:
        i=start*100+40
        # All section values 1.0, so the support floor is 2.0.
        # Add a real strong event at i, leaving a weak one at i+20.
        energy[i]=3.0
        energy[i+20]=1.0
        e[label]={"by_band":{
            name:{"candidate_count":2,"energy_supported_count":1,"weak_energy_count":1,
                  "selected_hop_indices":[i,i+20],"local_positive_peak_p75":1.0}
            for name in v2.BANDS},"union_hop_indices":[i,i+20],"union_count":2}
    return e


def test_energy_support_consistent_across_sections():
    energy=np.ones(12000)
    events=mock_events(energy)
    selected=v3.only_energy_supported(events,energy)
    assert all(x["by_band"]["low_40_180"]["selected_hop_indices"]==[start*100+40]
               for (label,start,_),x in zip(v2.SECTIONS,selected.values()))
    assert all(x["by_band"]["high_800_7000"]["excluded_weak_energy_candidates"]==1
               for x in selected.values())
    assert events["main_20_50s"]["by_band"]["low_40_180"]["candidate_count"]==2


def test_event_support_tamper_is_refused():
    energy=np.ones(12000);events=mock_events(energy)
    events["main_20_50s"]["by_band"]["low_40_180"]["energy_supported_count"]=2
    with pytest.raises(ValueError,match="support counts"):
        v3.only_energy_supported(events,energy)


def test_event_index_outside_section_is_refused():
    energy=np.ones(12000);events=mock_events(energy)
    events["before_0_20s"]["by_band"]["low_40_180"]["selected_hop_indices"]=[100000]
    with pytest.raises(ValueError,match="Invalid event"):
        v3.only_energy_supported(events,energy)


def test_output_normalization_is_computed_not_rendered():
    x=v3.safety_and_normalization(-16.6,-3.0)
    assert x["raw_meets_peak_ceiling"]
    assert not x["raw_meets_exploratory_guardband"]
    assert x["scalar_gain_to_target_lufs_db"]==pytest.approx(-7.4)
    assert x["predicted_after_scalar_normalization_true_peak_dbtp"]==pytest.approx(-10.4)
    assert x["predicted_normalized_meets_guardband"]
    assert x["normalization_is_theoretical_only"]
    with pytest.raises(ValueError,match="Unexpected loudness"):
        v3.safety_and_normalization(-27.0,-4.0)


def candidate(lead,low,high,tp=-5.0,change=0.0):
    head=v3.safety_and_normalization(-18.0,tp)
    return {"headroom":head,"lead_speechband_p10_db":lead,
        "worst_section_energy_supported_low_attack_p10_db":low,
        "worst_section_energy_supported_high_attack_p10_db":high,
        "gain_change_l1_db":change}


def test_hard_peak_gate_applies_before_pareto_not_as_objective():
    d={"ref":candidate(-2,-10,-10,tp=-3.6),
       "dominates":candidate(-1,-9,-9,tp=-4.0),
       "unsafe":candidate(0,0,0,tp=-2.5)}
    result=v3.pareto_among_feasible(d)
    assert result==["dominates"]
    assert d["ref"]["raw_safe_pareto_status"]=="dominated"
    assert d["unsafe"]["screening_status"]=="requires_scalar_output_attenuation"
    assert d["unsafe"]["raw_safe_pareto_status"]=="not_compared"


def test_missing_proxies_abstain_and_no_automatic_selection():
    d={"unknown":candidate(-1,None,-10)}
    assert v3.pareto_among_feasible(d)==[]
    assert d["unknown"]["screening_status"]=="insufficient_proxy_data"


def test_v2_fingerprint_and_event_mismatch_fail_closed():
    events={name:{"by_band":{band:{"candidate_count":1,"selected_hop_indices":[a*100+10]}
             for band in v2.BANDS}} for name,a,_ in v2.SECTIONS}
    checks={t:{"verified_sha256":t} for t in v1.TRACKS}
    old_checks={t:dict(v) for t,v in checks.items()}
    summary={"protocol":{"kind":"seven_stem_offline_physical_proxy_audit_v2","host_writes":0,
                         "automatic_gain_decision_made":False},
            "verified_sources":old_checks,
            "transient_candidates":events}
    assert v3._verify_v2_report(summary,{},checks,events)
    summary["verified_sources"]["RED"]["verified_sha256"]="wrong"
    with pytest.raises(ValueError,match="fingerprint"):
        v3._verify_v2_report(summary,{},checks,events)


def test_peak_gate_is_exploratory_not_live_eligibility():
    d={"safe":candidate(-1,-9,-9,tp=-10.0)}
    assert v3.pareto_among_feasible(d)==["safe"]
    assert d["safe"]["screening_status"]=="physically_screened_provisional"
    assert d["safe"].get("automatically_eligible") is not True


def test_end_to_end_report_only_on_synthetic_mocks(monkeypatch,tmp_path):
    """Exercise orchestration / filesystem isolation without 120s of fake WAVs."""
    import hashlib

    import soundfile as sf
    waveforms={}
    for t in v1.TRACKS:
        path=tmp_path/(t.replace(' ','_')+'.wav')
        sf.write(path, np.zeros(4800,dtype=np.float32),48000,subtype='FLOAT')
        st=path.stat()
        waveforms[t]={"path":str(path),"size":st.st_size,"mtime_ns":st.st_mtime_ns}
    audit={"sources":[{"track":t,"source_path":waveforms[t]["path"]} for t in v1.TRACKS]}
    audit_file=tmp_path/'audit.json';audit_file.write_text(json.dumps(audit))
    trial={"protocol":{"source_audit_sha256":hashlib.sha256(audit_file.read_bytes()).hexdigest(),
                       "coordinator_settings":{"baseline_gains_db":baseline(),
                          "activity_thresholds_dbfs":{t:-65.0 for t in v1.TRACKS}},
                       "evaluation_source_frames":[0,5760000]},
           "source_provenance":waveforms}
    report_file=tmp_path/'report.json';report_file.write_text(json.dumps(trial))
    verified={t:{"verified_sha256":f"hash-{t}"} for t in v1.TRACKS}
    energy=np.ones(12000,dtype=float)
    flux={name:np.zeros(12000,dtype=float) for name in v2.BANDS}
    for _,start,_ in v2.SECTIONS:
        flux['low_40_180'][start*100+40]=3.0
        flux['high_800_7000'][start*100+40]=3.0
        energy[start*100+40]=3.0
    ev=v2.select_local_events(flux,energy)
    summary={"protocol":{"kind":"seven_stem_offline_physical_proxy_audit_v2",
                         "source_report_sha256":hashlib.sha256(report_file.read_bytes()).hexdigest(),
                         "host_writes":0,"automatic_gain_decision_made":False},
             "verified_sources":verified,"transient_candidates":ev,
             "saturation":{"KIT":{"full_scale_sample_count":0}}}
    v2_file=tmp_path/'v2.json';v2_file.write_text(json.dumps(summary))
    monkeypatch.setattr(v1,'_check_source_files',lambda report:verified)
    monkeypatch.setattr(v1,'_source_windows',lambda report:(48000,0,0,5760000,waveforms))
    monkeypatch.setattr(v2,'kit_flux_10ms',lambda reader,frames:(flux,energy))
    monkeypatch.setattr(v2,'source_saturation',lambda reader,frames:{"full_scale_sample_count":0})
    def fake_capture(readers,rate,length,variants):
        bands=len(v1.BANDS_HZ)
        f=np.ones((1200,7,bands))
        p=np.ones((1200,7))
        return f,p,np.zeros(7),{x:.2 for x in variants},{x:1.0 for x in variants}
    monkeypatch.setattr(v1,'_capture_features',fake_capture)
    def fake_attacks(readers,events,variants,*,evaluation_start):
        return {s:{band:{n:{"status":"measured","count":1,"p10_db":-10.+float(g[v1.INDEX['KIT']]),
                                 "median_db":-5.,"p90_db":0.} for n,g in variants.items()}
                   for band in v2.BANDS} for s in events}
    monkeypatch.setattr(v2,'capture_section_attack_margins',fake_attacks)
    monkeypatch.setattr(v2,'_scan_ffmpeg_pipe',lambda readers,variants,**kw:{n:{"status":"measured",
        "integrated_lufs":-18.,"true_peak_dbtp":-5.} for n in variants})
    result=v3.run(report_file,v2_file,tmp_path/'out',audit_path=audit_file,ffmpeg=tmp_path/'audit.json')
    assert len(result['candidates'])==9
    assert result['protocol']['host_writes']==0
    assert result['automatic_selection'] is None
    assert list((tmp_path/'out').iterdir())==[tmp_path/'out'/'summary.json']
    assert not list(tmp_path.rglob('audition*.wav'))
