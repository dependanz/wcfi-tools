"""Physical line/flat-top evidence must not become an arbitrary quality meter."""
import numpy as np
import pytest

from wcfi_tools.soundcheck.artifact_health import analyze


def signal(rate=24000):
    rng = np.random.default_rng(3)
    t = np.arange(rate*10)/rate
    x = .08*rng.normal(size=(len(t), 2)) + .12*np.sin(2*np.pi*220*t)[:, None]
    return x.astype('float32'), t, rate


@pytest.mark.parametrize('mains', [50, 60])
def test_persistent_mains_family_detected_without_reference(mains):
    x, t, rate = signal()
    hum = sum(np.sin(2*np.pi*(f+.03)*t) for f in (mains, 2*mains, 3*mains))[:, None]*.025
    result = analyze(x+hum, rate)
    assert any(a['type'] == 'suspected_mains' and a['frequency_hz'] == mains for a in result['alerts'])
    assert result['professional_quality_score'] is None
    assert result['auto_fix_authorized'] is False


def test_short_mains_tone_and_nearby_bass_note_are_not_persistent_hum():
    x, t, rate = signal()
    nearby = x + .1*np.sin(2*np.pi*55*t)[:, None]
    assert not analyze(nearby, rate)['alerts']
    short = x.copy()
    short[:rate] += .2*np.sin(2*np.pi*60*t[:rate])[:, None]
    assert not any(a['type'] == 'suspected_mains' for a in analyze(short, rate)['alerts'])


def test_nearby_bass_does_not_conceal_persistent_hum_harmonics():
    x, t, rate = signal()
    bass = .12*np.sin(2*np.pi*62*t)[:, None]
    hum = .035*sum(np.sin(2*np.pi*(60+.03)*h*t) for h in (1, 2, 3))[:, None]
    assert any(a['type'] == 'suspected_mains' for a in analyze(x+bass+hum, rate)['alerts'])


def test_flat_top_clipping_and_gain_invariance():
    x, _, rate = signal()
    clipped = np.clip(x, -.12, .12)
    assert any(a['type'] == 'flat_peaks' for a in analyze(clipped, rate)['alerts'])
    a, b = analyze(clipped, rate), analyze(clipped*.1, rate)
    assert [r['type'] for r in a['alerts']] == [r['type'] for r in b['alerts']]
    assert a['metrics']['flat_top_fraction'] == pytest.approx(b['metrics']['flat_top_fraction'])


def test_silence_unknown_not_a_high_quality_rating():
    result = analyze(np.zeros((24000*8, 2)), 24000)
    assert result['status'] == 'insufficient_signal'
    assert result['professional_quality_score'] is None
    with pytest.raises(ValueError):
        analyze(np.zeros((24000, 2)), 24000)
