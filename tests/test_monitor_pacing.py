"""Frame deadlines and balanced timer requests; no native calls or windows."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wcfi_tools.monitor_control.pacing import FramePacer, PreviewTimer


def test_capture_cost_comes_out_of_frame_budget():
    pacer = FramePacer(60)
    assert pacer.delay_ms(0, .012) == 5
    assert pacer.delay_ms(.017, .029) == 5
    assert pacer.deadline == pytest.approx(2 / 60)


def test_delayed_frames_yield_without_building_a_catch_up_queue():
    pacer = FramePacer(60)
    assert pacer.delay_ms(0, .035) == 1
    assert pacer.delay_ms(.036, .047) == 5
    assert pacer.delay_ms(4, 4.012) == 5
    assert pacer.deadline == pytest.approx(4 + 1 / 60)


def test_lower_requested_rate_is_respected():
    assert FramePacer(10).delay_ms(0, .010) == 90


def test_timer_resolution_is_balanced_on_hide_resume_and_repeated_close():
    timer = PreviewTimer()
    timer._api = SimpleNamespace(timeBeginPeriod=Mock(return_value=0), timeEndPeriod=Mock())
    timer.set_active(True)
    timer.set_active(True)
    timer._api.timeBeginPeriod.assert_called_once_with(1)
    timer.set_active(False)
    timer.close()
    timer._api.timeEndPeriod.assert_called_once_with(1)
    timer.set_active(True)
    timer.close()
    assert timer._api.timeBeginPeriod.call_count == timer._api.timeEndPeriod.call_count == 2
    assert not timer.active


def test_unsupported_timer_request_leaves_default_timing_and_does_not_retry():
    timer = PreviewTimer()
    timer._api = SimpleNamespace(timeBeginPeriod=Mock(return_value=97), timeEndPeriod=Mock())
    timer.set_active(True)
    timer.set_active(True)
    timer.close()
    timer._api.timeBeginPeriod.assert_called_once_with(1)
    timer._api.timeEndPeriod.assert_not_called()
    assert not timer.active
