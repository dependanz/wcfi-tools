import inspect

import numpy as np

from wcfi_tools.audio import full_mix_benchmark as base
from wcfi_tools.audio import full_mix_combined as combined


def test_combined_inference_does_not_read_key_or_renderer_settings():
    code = inspect.getsource(combined.run)+inspect.getsource(combined.evaluate_audio)
    assert 'private-key.json' not in code and 'gain_db' not in code and 'plans' not in code
    assert 'source_overrides' in code  # explicit audio context is permitted
    assert combined.POLICY['style'] == base.POLICY
    assert combined.POLICY['professional_quality_judgment_validated'] is False


def test_musical_negative_supplies_the_same_new_content_in_raw_source_and_mix():
    n = 12*48000;rng = np.random.default_rng(73)
    raw = {'BASS': rng.normal(0, .02, (n, 2))};anchor = rng.normal(0, .03, (n, 2))
    arrays, overrides = combined.new_hum_controls(anchor, raw, -6, .23)
    for name in ('bass_near50', 'bass_near60', 'bass_source60'):
        injected = (overrides[name]['BASS']-raw['BASS'])*10**(-6/20)*.23
        np.testing.assert_allclose(arrays[name]-anchor, injected, atol=1e-15)
    assert not (set(overrides) & {k for k, v in combined.HUM_CONTROLS.items() if v[1] == 'hum_fault'})
    np.testing.assert_array_equal(arrays['hum60_episode'][:6*48000], anchor[:6*48000])


def test_combined_package_never_accepts_professional_quality_claim(tmp_path):
    import pytest

    report = {'kind': 'wcfi_full_mix_combined_benchmark', 'policy': combined.POLICY,
              'professional_quality_judgment_validated': True}
    package = {'kind': 'wcfi_full_mix_combined_package', 'schema_version': 1, 'reports': {'MIX': report}}
    with pytest.raises(ValueError, match='scope'):
        combined.validate_package(tmp_path, package, b'{}')


def test_selection_requires_both_halves_reference_agreement_and_gap():
    winner = {'evaluation': {'eligible': True}}
    halves = [{'passed': True}, {'passed': True}]
    assert combined.selection_checks(winner, True, .2, .05, halves)
    assert not combined.selection_checks(winner, False, .2, .05, halves)
    assert not combined.selection_checks(winner, True, .2, .001, halves)
    assert not combined.selection_checks(winner, True, .01, .05, halves)
    assert not combined.selection_checks(winner, True, .2, .05, [{'passed': True}, {'passed': False}])
    assert not combined.selection_checks(winner, True, .2, .05, [{'passed': True}])
