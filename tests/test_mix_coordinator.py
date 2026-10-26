"""Prospective contracts for the offline full-band gain coordinator."""
import numpy as np
import pytest

from wcfi_tools.audio.mix_coordinator import CoordinatorSettings, MixCoordinator, TRACKS, SUPPORT_TRACKS


RATE = 8000


def settings(**overrides):
    values = dict(baseline_gains_db={t: 0. for t in TRACKS},
                  reference_support_to_lead_db={t: -6. for t in SUPPORT_TRACKS})
    values.update(overrides)
    return CoordinatorSettings(**values)


def audio(seconds, red=-30., support=-36., channels=1):
    levels = np.array([red] + [support] * len(SUPPORT_TRACKS))
    return np.broadcast_to(10 ** (levels[None, :, None] / 20), (round(seconds * RATE), len(TRACKS), channels)).copy()


def run(block, config=None, chunks=None):
    c = MixCoordinator(config or settings(), RATE)
    if chunks is None:
        return c, c.process(block)
    pieces, position = [], 0
    for size in chunks:
        pieces.append(c.process(block[position:position + size]))
        position += size
    pieces.append(c.process(block[position:]))
    return c, np.concatenate(pieces)


def test_fixed_roles_exclude_lapel_talkback_bass_and_unknown_shapes():
    assert TRACKS == ('RED', 'YELLOW', 'GREEN', 'EGUITAR', 'AC GUITAR', 'KEYS', 'KIT')
    for unwanted in ("LAPEL", "MDMIC Talkback", "BASS", "UNKNOWN"):
        gains = {t: 0. for t in TRACKS}
        gains[unwanted] = 0.
        with pytest.raises(ValueError):
            settings(baseline_gains_db=gains)
        with pytest.raises(ValueError):
            MixCoordinator(settings(), RATE).set_manual(unwanted, 0)
    for shape in ((800, 8, 1), (800, 6, 1), (800, 7, 3), (800, 7)):
        with pytest.raises(ValueError):
            MixCoordinator(settings(), RATE).process(np.zeros(shape))


@pytest.mark.parametrize("field,value", [("max_cut_db", 3.1), ("slew_db_per_second", float('nan')),
    ("evidence_seconds", 3.05), ("drift_deadband_db", float('inf')),
    ("recovery_hysteresis_db", 2), ("master_peak_ceiling_dbfs", 1),
    ("evidence_window_seconds", 2.9), ("evidence_window_seconds", 5.05),
    ("unsupported_reset_seconds", 0)])
def test_invalid_settings(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


def test_settings_snapshot_cannot_mutate_during_stream():
    gains = {t: 0. for t in TRACKS}
    config = settings(baseline_gains_db=gains)
    gains['RED'] = 12
    assert config.baseline_gains_db['RED'] == 0
    with pytest.raises(TypeError):
        config.baseline_gains_db['RED'] = 12
    snapshot = config.as_dict()
    snapshot['baseline_gains_db']['RED'] = 12
    assert config.baseline_gains_db['RED'] == 0
    assert CoordinatorSettings(**config.as_dict()).as_dict() == config.as_dict()


def test_baseline_only_at_reference_and_no_autonomous_lead_change():
    gains = {t: -3. for t in TRACKS}
    source = audio(5)
    c, result = run(source, settings(baseline_gains_db=gains))
    np.testing.assert_allclose(result, source * 10 ** (-3 / 20), rtol=0, atol=1e-16)
    assert all(v == 0 for v in c.current_correction_db.values())


def test_sustained_relative_drift_qualifies_then_ramps_with_bounds():
    source = audio(12, support=-30)
    c, result = run(source)
    np.testing.assert_array_equal(result[:3 * RATE], source[:3 * RATE])
    gain = 20 * np.log10(result[:, 1, 0] / source[:, 1, 0])
    assert np.min(gain) == pytest.approx(-3)
    assert np.max(gain) == 0
    assert np.max(np.abs(np.diff(gain))) <= .5 / RATE + 1e-12
    assert c.current_correction_db['RED'] == 0
    assert c.current_correction_db['YELLOW'] == pytest.approx(-3)
    assert c.events[29]['target_correction_db']['YELLOW'] == -3


def test_brief_accent_and_inactive_support_never_qualify():
    source = np.concatenate((audio(4), audio(.5, support=-26), audio(4), audio(4, support=-100)))
    c, result = run(source)
    np.testing.assert_array_equal(source, result)
    assert any(e['reasons']['YELLOW'] == 'hold_accent' for e in c.events)
    assert any(e['reasons']['YELLOW'] == 'hold_support_inactive' for e in c.events)


def test_lead_gap_cancels_inflight_ramp_and_does_not_recover_in_silence():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(4, support=-30))
    assert c.current_correction_db['YELLOW'] == pytest.approx(-.5)
    c.process(audio(.1, red=-100, support=-36))
    held = c.current_correction_db['YELLOW']
    assert held == pytest.approx(-.55)
    c.process(audio(3, red=-100, support=-36))
    assert c.current_correction_db['YELLOW'] == held
    assert c.target_correction_db['YELLOW'] == held
    c.process(audio(2.9, support=-30))
    assert c.current_correction_db['YELLOW'] == held


def test_recovery_requires_new_active_evidence_and_respects_master_peak_veto():
    c = MixCoordinator(settings(master_peak_ceiling_dbfs=-30), RATE)
    c.process(audio(12, support=-30))
    c.process(audio(6))
    assert c.current_correction_db['YELLOW'] == pytest.approx(-3)
    assert any(e['reasons']['YELLOW'] == 'hold_master_headroom' for e in c.events)
    # Same ratio, lower valid absolute levels: now the completed sum admits recovery.
    c.process(audio(12, red=-45, support=-51))
    assert c.current_correction_db['YELLOW'] == pytest.approx(0)


def test_invalid_bin_and_gap_cancel_ramps_and_requalify():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(4, support=-30))
    corrupt = audio(.1, support=-30)
    corrupt[3, 1, 0] = np.nan
    output = c.process(corrupt)
    assert np.isfinite(output).all()
    assert c.invalid_samples == 1
    held = c.current_correction_db['YELLOW']
    assert c.target_correction_db['YELLOW'] == held
    c.process(audio(2.9, support=-30))
    assert c.current_correction_db['YELLOW'] == held
    c.process(audio(.2, support=-30))
    assert c.current_correction_db['YELLOW'] < held
    c.process(audio(.05, support=-30))
    c.notify_gap()
    held = c.current_correction_db['YELLOW']
    c.process(audio(3, support=-30))
    assert c.current_correction_db['YELLOW'] == held
    c.process(audio(.1, support=-30))
    assert c.current_correction_db['YELLOW'] < held


def test_manual_owns_gain_immediately_and_release_requalifies():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(4, support=-30))
    c.set_manual('YELLOW', 1)
    c.process(audio(5, support=-30))
    assert c.current_correction_db['YELLOW'] == pytest.approx(1)
    c.set_manual('YELLOW', None)
    c.process(audio(3, support=-30))
    assert c.current_correction_db['YELLOW'] == pytest.approx(1)
    c.process(audio(.1, support=-30))
    assert c.current_correction_db['YELLOW'] < 1
    assert c.events[-1]['reasons']['YELLOW'] == 'attenuate'
    for bad in (-3.1, 3.1, float('nan')):
        with pytest.raises(ValueError):
            c.set_manual('RED', bad)


def test_arbitrary_chunk_partition_is_exact_and_completed_bins_are_causal():
    source = np.concatenate((audio(4), audio(12, support=-30), audio(8)))
    a, full = run(source)
    rng = np.random.default_rng(333)
    chunks = rng.integers(1, 1600, size=80)
    b, split = run(source, chunks=chunks)
    np.testing.assert_array_equal(full, split)
    assert a.events == b.events
    prefix = 14327
    changed = source.copy()
    changed[prefix:] *= .01
    _, alternate = run(changed)
    np.testing.assert_array_equal(full[:prefix], alternate[:prefix])


def test_mono_and_identical_stereo_match_and_channel_change_rejected():
    mono = audio(12, support=-30)
    a, one = run(mono)
    b, two = run(np.repeat(mono, 2, axis=2))
    np.testing.assert_allclose(one[:, :, 0], two[:, :, 0], rtol=0, atol=1e-15)
    assert a.current_correction_db == b.current_correction_db
    with pytest.raises(ValueError):
        a.process(audio(.1, channels=2))


def test_reference_ratio_uses_baseline_but_not_own_correction():
    gains = {t: 0 if t == 'RED' else -6 for t in TRACKS}
    c, out = run(audio(12, support=-30), settings(baseline_gains_db=gains))
    assert all(v == 0 for v in c.current_correction_db.values())
    assert out[-1, 1, 0] == pytest.approx(10 ** (-36 / 20))


def test_disabled_tracks_never_accumulate_automatic_evidence():
    c, _ = run(audio(12, support=-30), settings(enabled_support_tracks=('KIT',)))
    assert c.current_correction_db['KIT'] == -3
    assert c.current_correction_db['YELLOW'] == 0
    assert all(e['reasons']['YELLOW'] == 'hold_disabled' for e in c.events)
    for invalid in (('LAPEL',), ('BASS',), ('RED',), ('KIT', 'KIT')):
        with pytest.raises(ValueError):
            settings(enabled_support_tracks=invalid)


def test_manual_release_mid_bin_does_not_count_partial_bin_as_fresh_evidence():
    c = MixCoordinator(settings(), RATE)
    c.set_manual('YELLOW', 1)
    c.process(audio(4.05, support=-30))
    c.set_manual('YELLOW', None)
    c.process(audio(3, support=-30))
    assert c.current_correction_db['YELLOW'] == 1
    c.process(audio(.1, support=-30))
    assert c.current_correction_db['YELLOW'] < 1


def test_shared_band_and_lead_dynamics_do_not_create_relative_corrections():
    source = np.concatenate([audio(4, red=level, support=level - 6) for level in (-40, -26, -35, -30)])
    c, result = run(source)
    np.testing.assert_array_equal(result, source)
    assert all(v == 0 for v in c.current_correction_db.values())


def test_short_phrase_pauses_retain_qualified_evidence_but_freeze_ramps():
    source = np.concatenate([part for _ in range(6)
                             for part in (audio(.9, support=-30), audio(.1, red=-100, support=-30))])
    c, result = run(source)
    assert c.current_correction_db['YELLOW'] < -.5
    assert all(e['target_correction_db']['YELLOW'] == e['current_correction_db']['YELLOW']
               for e in c.events if e['reasons']['YELLOW'].startswith('hold_') and
               e['reasons']['YELLOW'] != 'hold_cut')
    assert any(e['consecutive_unsupported_bins']['YELLOW'] > 0 and
               e['evidence_bins']['YELLOW'] > 0 for e in c.events)
    # Qualification is thirty good bins, not three wall-clock seconds.
    np.testing.assert_array_equal(result[:3 * RATE], source[:3 * RATE])


def test_one_second_relative_accent_cannot_bias_rolling_baseline_to_cut():
    source = np.concatenate((audio(4), audio(1, support=-32), audio(4)))
    c, result = run(source)
    np.testing.assert_array_equal(result, source)
    assert any(e['raw_relative_error_db']['YELLOW'] > 3 for e in c.events)
    assert all(e['target_correction_db']['YELLOW'] == 0 for e in c.events)


def test_one_second_unsupported_pair_clears_retained_history():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(4, support=-30))
    c.process(audio(.9, red=-100, support=-30))
    assert c.events[-1]['evidence_bins']['YELLOW'] > 0
    held = c.current_correction_db['YELLOW']
    c.process(audio(.1, red=-100, support=-30))
    assert c.events[-1]['evidence_bins']['YELLOW'] == 0
    c.process(audio(3, support=-30))
    assert c.current_correction_db['YELLOW'] == held
    c.process(audio(.2, support=-30))
    assert c.current_correction_db['YELLOW'] < held


def test_old_evidence_expires_without_needing_a_one_second_gap():
    source = np.concatenate([part for _ in range(15)
                             for part in (audio(.5, support=-30), audio(.5, red=-100, support=-30))])
    c, result = run(source)
    np.testing.assert_array_equal(result, source)
    assert 0 < c.events[-1]['evidence_bins']['YELLOW'] < 30
    assert max(e['evidence_bins']['YELLOW'] for e in c.events) < 30


def test_old_high_history_cannot_cut_a_current_softer_phrase():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(5, support=-32))
    c.process(audio(.1))
    assert c.events[-1]['rolling_median_error_db']['YELLOW'] > 2
    assert c.events[-1]['reasons']['YELLOW'] == 'hold_current_cut_veto'
    held = c.current_correction_db['YELLOW']
    assert c.target_correction_db['YELLOW'] == held
    c.process(audio(.1))
    assert c.current_correction_db['YELLOW'] == held


def test_old_low_history_cannot_recover_during_current_relative_drift():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(12, support=-30))
    c.process(audio(3))
    assert c.events[-1]['reasons']['YELLOW'] == 'recover'
    c.process(audio(.1, support=-32))
    assert c.events[-1]['rolling_median_error_db']['YELLOW'] <= 1.5
    assert c.events[-1]['reasons']['YELLOW'] == 'hold_current_recovery_veto'
    held = c.current_correction_db['YELLOW']
    c.process(audio(.1, support=-32))
    assert c.current_correction_db['YELLOW'] == held


def test_new_cut_is_limited_by_current_excess_as_well_as_rolling_median():
    c = MixCoordinator(settings(), RATE)
    c.process(audio(3, support=-30))
    c.process(audio(.1, support=-33.5))
    assert c.events[-1]['rolling_median_error_db']['YELLOW'] > 5
    assert c.target_correction_db['YELLOW'] == pytest.approx(-.5)
