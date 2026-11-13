"""First-principles WCFI audit tests; no church audio or Ableton required."""
from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from wcfi_tools.audio.mix_objective_audit import (
    BANDS_HZ,
    INDEX,
    TRACKS,
    _band_powers,
    _capture_features,
    _select_onsets,
    _source_windows,
    _variants,
    evaluate_objectives,
)


def thresholds():
    return {x: -55.0 for x in TRACKS}


def make_features(n=1200):
    f = np.zeros((n, len(TRACKS), len(BANDS_HZ)), dtype=float)
    rms = np.ones((n, len(TRACKS)), dtype=float)*1e-5
    speech = list(BANDS_HZ).index("speech_300_4500")
    drum = list(BANDS_HZ).index("drum_80_7000")
    f[:, INDEX["RED"], speech] = 2e-3
    f[:, INDEX["KIT"], speech] = 4e-4
    f[:, INDEX["KEYS"], speech] = 3e-4
    f[:, INDEX["KIT"], drum] = 1e-3
    f[:, INDEX["RED"], drum] = 2e-4
    f[:, INDEX["KEYS"], drum] = 2e-4
    f[:, INDEX["KIT"], 0] = 2e-4
    f[:, INDEX["KIT"], 1] = 2e-4
    f[:, INDEX["KIT"], 2] = 2e-4
    f[:, INDEX["KIT"], 3] = 2e-4
    f[::10, INDEX["KIT"], 0] += 1e-3  # repeat isolated source-only attacks
    rms[:, INDEX["RED"]] = 1e-2
    rms[:, INDEX["KIT"]] = 1e-2
    return f, rms


def test_gain_ratios_change_in_expected_physical_direction():
    f, rms = make_features()
    variants = _variants({t:0.0 for t in TRACKS})
    score, select = evaluate_objectives(f,rms,variants,thresholds())
    a = score["frozen_static"]["global"]
    lead = score["lead_minus3db_probe"]["global"]
    kit = score["kit_plus3db_probe"]["global"]
    assert lead["lead_vs_all_stems_speech_band_db"]["median_db"] == pytest.approx(
        a["lead_vs_all_stems_speech_band_db"]["median_db"] - 3)
    assert kit["kit_vs_all_other_stems_drum_band_at_candidate_attacks_db"]["median_db"] == pytest.approx(
        a["kit_vs_all_other_stems_drum_band_at_candidate_attacks_db"]["median_db"] + 3)
    assert len(select["selected_attack_bin_indices"]) > 0
    assert select["kit_attack_candidates"] == len(select["selected_attack_bin_indices"])
    assert select["kit_attack_candidates"] < len(f)


def test_silence_returns_missing_events_not_fake_intelligibility():
    f=np.zeros((20,len(TRACKS),len(BANDS_HZ)))
    rms=np.zeros((20,len(TRACKS)))
    res,sel=evaluate_objectives(f,rms,_variants({t:0 for t in TRACKS}),thresholds())
    assert sel["kit_attack_candidates"] == 0
    assert res["frozen_static"]["global"]["lead_vs_all_stems_speech_band_db"]["status"] == "unavailable"
    assert res["frozen_static"]["global"]["kit_vs_all_other_stems_drum_band_at_candidate_attacks_db"]["status"] == "unavailable"


def test_source_only_attack_detection_is_gain_independent():
    f,rms=make_features(100)
    onset,flux,active=_select_onsets(f[:, INDEX['KIT']], rms[:, INDEX['KIT']],0,-55)
    assert len(onset)>0
    assert set(onset).issubset(set(range(100)))
    assert np.all(flux[onset]>0)
    assert active==100


def test_frequency_band_accounting_for_sine():
    rate,n=48000,4800
    time=np.arange(n)/rate
    x=np.sin(2*np.pi*100*time)[:,None]
    hann=np.hanning(n)
    freq=np.fft.rfftfreq(n,1/rate)
    masks=[(freq>=lo)&(freq<hi) for lo,hi in BANDS_HZ.values()]
    result=_band_powers(x,hann,masks,n*np.sum(hann**2))
    assert result[0] == pytest.approx(.5,rel=.01)
    assert result[1] < 1e-8
    assert result[-1] == pytest.approx(.5,rel=.01)


def test_source_selection_never_traverses_disallowed_tracks():
    rep={"protocol":{"tracks":list(TRACKS),"source_date":"2026-10-04","sample_rate":48000,
          "calibration_source_frames":[48000,96000],"evaluation_source_frames":[96000,96000+5760000]},
         "source_provenance":{t:{} for t in TRACKS}}
    assert _source_windows(rep)[0] == 48000
    rep["source_provenance"]["BASS"]={}
    with pytest.raises(ValueError,match="exactly seven"):
        _source_windows(rep)
    del rep["source_provenance"]["BASS"]
    rep["protocol"]["source_date"]="2026-01-04"
    with pytest.raises(ValueError,match="frozen October"):
        _source_windows(rep)


class FakeReader:
    def __init__(self, waveform):
        self.waveform=waveform.astype('float32')
        self.index=0
    def read(self,n,dtype,always_2d):
        ret=self.waveform[self.index:self.index+n]
        self.index+=len(ret)
        return ret


def test_coherent_sum_peak_catches_interference_not_visible_from_band_powers():
    n=4800
    phase=np.sin(2*np.pi*800*np.arange(n)/48000).reshape(-1,1)
    streams={t:FakeReader(np.zeros((n,1))) for t in TRACKS}
    streams['RED']=FakeReader(phase)
    streams['KIT']=FakeReader(-phase)
    variants=_variants({t:0 for t in TRACKS})
    _,_,_,peaks,_=_capture_features(streams,48000,n,variants)
    assert peaks['frozen_static'] == pytest.approx(0,abs=1e-7)
    assert peaks['kit_plus3db_probe'] > .4


def test_diagnostic_probes_do_not_change_original_static_reference():
    baseline={t:0 for t in TRACKS}
    baseline['RED']=6
    baseline['KIT']=-15.9
    variants=_variants(baseline)
    assert variants['frozen_static'][INDEX['RED']]==6
    assert variants['frozen_static'][INDEX['KIT']]==-15.9
    assert variants['both_probe'][INDEX['RED']]==3
    assert variants['both_probe'][INDEX['KIT']]==pytest.approx(-12.9)


def test_failure_before_output_on_missing_report(tmp_path):
    output=tmp_path/'should-not-exist'
    r=subprocess.run([sys.executable,'-m','wcfi_tools.audio.mix_objective_audit',
                      '--report',str(tmp_path/'missing-report.json'), '--output',str(output)],
                     capture_output=True,text=True, check=False)
    assert r.returncode != 0
    assert not output.exists()
