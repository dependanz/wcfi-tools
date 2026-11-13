"""Audio-derived attribution, hard priority, pan invariance and bleed abstention."""
import inspect

import numpy as np
import pytest

from wcfi_tools.audio import balance_benchmark as bench
from wcfi_tools.audio.source_balance import BalanceAnalyzer, veto


def sources():
    n = 48000*4;t = np.arange(n)/48000;rng = np.random.default_rng(7)
    def stereo(a):
        return np.repeat(a[:, None], 2, axis=1)
    raw = {'RED': stereo(.05*np.sin(2*np.pi*380*t)+.02*np.sin(2*np.pi*1400*t)),
           'KIT': stereo(rng.normal(0, .04, n)),
           'AC GUITAR': stereo(.04*np.sin(2*np.pi*250*t)+.015*np.sin(2*np.pi*2300*t))}
    return raw


def test_dominant_acoustic_veto_cannot_be_overridden_by_perfect_reference_score():
    raw = sources();analyzer = BalanceAnalyzer(raw)
    good = analyzer.evaluate(raw['RED']+raw['KIT']+raw['AC GUITAR']*.1)
    bad = analyzer.evaluate(raw['RED']+raw['KIT']+raw['AC GUITAR']*5)
    assert good['eligible'] and not good['violations']
    assert any(v.startswith('acoustic_over_lead') for v in bad['violations'])
    best_style = {'cost': 0, 'eligible': True, 'guardrail_failures': []}
    assert not veto(best_style, bad)['eligible']
    assert veto(best_style, bad)['cost'] == 0
    # Improving balance must never erase a different, independently failed check.
    clipped = {'cost': 0, 'eligible': False, 'guardrail_failures': ['hard-clipping plateaus']}
    assert not veto(clipped, good)['eligible']
    assert veto(clipped, good)['guardrail_failures'] == ['hard-clipping plateaus']


def test_global_gain_and_pan_change_do_not_invent_balance_faults():
    raw = sources();analyzer = BalanceAnalyzer(raw)
    fixed = raw['RED']+raw['KIT'];ac = raw['AC GUITAR']*.1
    a = analyzer.evaluate(fixed+ac*np.array([1.3, .55]))
    b = analyzer.evaluate((fixed+ac*np.array([.55, 1.3]))*.31)
    assert a['violations'] == b['violations'] == []
    for left, right in zip(a['rules'], b['rules'], strict=True):
        if 'median_db' in left:
            assert left['median_db'] == pytest.approx(right['median_db'], abs=.03)


def test_short_peak_does_not_veto_but_sustained_surge_does():
    raw = sources();analyzer = BalanceAnalyzer(raw)
    baseline = raw['RED']+raw['KIT']+raw['AC GUITAR']*.1
    short = baseline.copy();short[48000:48240] += raw['AC GUITAR'][48000:48240]*5
    long = baseline.copy();long[48000:96000] += raw['AC GUITAR'][48000:96000]*5
    assert not any(v.startswith('acoustic_over_lead') for v in analyzer.evaluate(short)['violations'])
    assert any(v.startswith('acoustic_over_lead') for v in analyzer.evaluate(long)['violations'])


def test_correlated_bleed_is_indeterminate_rather_than_a_confident_verdict():
    raw = sources();raw['AC GUITAR'] = raw['RED'].copy()
    result = BalanceAnalyzer(raw).evaluate(sum(raw.values()))
    assert result['indeterminate_rules'] and not result['eligible']
    assert any(r['status'] == 'indeterminate' and r.get('unique_fraction', 1) < .04 for r in result['rules'])
    assert 'source attribution uncertain' in veto({'guardrail_failures': []}, result)['guardrail_failures']


def test_balance_inference_does_not_consume_renderer_parameters_or_answer_key():
    source = inspect.getsource(bench.run)
    assert 'private-key.json' not in source
    assert 'plans' not in source and 'gain_db' not in source
    assert bench.CONTROLS['ac_up14'][1] == 'fault'
    assert len(bench.CONTROLS) == 16
