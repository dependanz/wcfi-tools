import numpy as np

from wcfi_tools.audio.mains_hum import HumAnalyzer, veto


def sources():
    n = 6*48000;t = np.arange(n)/48000;rng = np.random.default_rng(51)
    stereo = lambda a: np.repeat(a[:, None], 2, axis=1)
    return {'RED': stereo(.04*np.sin(2*np.pi*210*t)),
            'KIT': rng.normal(0, .02, (n, 2)),
            'BASS': stereo(.03*np.sin(2*np.pi*73.4*t))}, t


def comb(t, hz):
    return sum(np.sin(2*np.pi*hz*k*t)/k for k in (1, 2, 3))[:, None]*.03


def test_added_hum_detected_at_both_nominal_families_and_under_scalar_gain():
    raw, t = sources();analyzer = HumAnalyzer(raw);clean = sum(raw.values())
    assert not analyzer.evaluate(clean)['violation']
    for hz in (50, 60):
        a = clean+comb(t, hz)
        assert analyzer.evaluate(a)['violation']
        assert analyzer.evaluate(a*.23)['violation']


def test_nearby_musical_note_and_source_explained_comb_are_not_new_master_hum():
    raw, t = sources()
    for hz in (49., 61.735):
        changed = raw | {'BASS': raw['BASS']+comb(t, hz)}
        assert not HumAnalyzer(changed).evaluate(sum(changed.values()))['violation']
    changed = raw | {'BASS': raw['BASS']+comb(t, 60)}
    assert not HumAnalyzer(changed).evaluate(sum(changed.values()))['violation']


def test_a_brief_tone_does_not_supply_persistent_evidence():
    raw, t = sources();a = sum(raw.values());h = comb(t, 60);h[48000:] = 0
    assert not HumAnalyzer(raw).evaluate(a+h)['violation']


def test_hum_replaces_old_rule_without_erasing_other_failures():
    prior = {'cost': .01, 'guardrail_failures': ['tonal hum', 'source balance: buried_lead']}
    result = veto(prior, {'violation': False})
    assert result['guardrail_failures'] == ['source balance: buried_lead'] and not result['eligible']
    assert 'unexplained mains hum' in veto({'guardrail_failures': []}, {'violation': True})['guardrail_failures']
