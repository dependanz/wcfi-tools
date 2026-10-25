"""Capture-thread ownership, latest-frame delivery and stale-target safety."""

import threading
import time

import pytest

from wcfi_tools.monitor_control.stream import CaptureStream

A = (0, 0, 1920, 1080, 960, 540)
B = (-1920, 0, 1920, 1080, 960, 540)


def wait_frame(stream):
    deadline = time.perf_counter() + 1
    while time.perf_counter() < deadline:
        frame = stream.take()
        if frame is not None:
            return frame
        time.sleep(.001)
    raise AssertionError("No completed frame")


def test_only_latest_frame_survives_a_slow_consumer_and_resources_stay_on_worker():
    fourth_started, finish = threading.Event(), threading.Event()
    owners = []

    class Backend:
        def __init__(self):
            self.count = 0
            owners.append(threading.get_ident())

        def grab(self, *bounds):
            owners.append(threading.get_ident())
            self.count += 1
            if self.count == 4:
                fourth_started.set()
                assert finish.wait(1)
            return str(self.count).encode()

        def close(self):
            owners.append(threading.get_ident())

    stream = CaptureStream(60, factory=Backend)
    try:
        assert stream.request(A)
        assert not stream.request(A)
        assert fourth_started.wait(1)
        frame = stream.take()
        assert frame.sequence == 3 and frame.ppm == b"3"
        assert frame.bounds == A
        assert frame.started_at <= frame.captured_at <= time.perf_counter()
        assert stream.take() is None  # No replay of frames 1 and 2.
    finally:
        finish.set()
        stream.close()
    assert len(set(owners)) == 1 and owners[0] != threading.get_ident()
    stream.close()
    with pytest.raises(RuntimeError, match="closed"):
        stream.request(A)


def test_switching_away_and_back_discards_an_in_flight_frame():
    first, second, release_first, release_second = (threading.Event() for _ in range(4))

    class Backend:
        count = 0

        def grab(self, *bounds):
            self.count += 1
            if self.count == 1:
                first.set()
                assert release_first.wait(1)
            if self.count == 2:
                second.set()
                assert release_second.wait(1)
            return str(self.count).encode()

        def close(self):
            pass

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A)
        assert first.wait(1)
        stream.request(B)
        stream.request(A)
        release_first.set()
        assert second.wait(1)
        assert stream.take() is None
        release_second.set()
        assert wait_frame(stream).ppm != b"1"
    finally:
        release_first.set()
        release_second.set()
        stream.close()


def test_pause_drops_in_flight_output_without_blocking_the_ui():
    started, release, finished = (threading.Event() for _ in range(3))

    class Backend:
        def grab(self, *bounds):
            started.set()
            assert release.wait(1)
            finished.set()
            return b"old target"

        def close(self):
            pass

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A)
        assert started.wait(1)
        stream.pause()
        assert not release.is_set()  # pause returned before capture could finish.
        release.set()
        assert finished.wait(1)
        stream.close()
        assert stream.take() is None
    finally:
        release.set()
        stream.close()


def test_capture_error_reaches_consumer_and_backend_can_be_recreated():
    failed, closed = threading.Event(), threading.Event()
    attempts = []

    class Backend:
        def __init__(self):
            attempts.append(1)
            self.fail = len(attempts) == 1

        def grab(self, *bounds):
            if self.fail:
                failed.set()
                raise OSError("display disconnected")
            return b"recovered"

        def close(self):
            closed.set()

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A)
        assert failed.wait(1) and closed.wait(1)
        with pytest.raises(RuntimeError, match="display disconnected"):
            stream.take()
        stream.request(B)
        assert wait_frame(stream).ppm == b"recovered"
    finally:
        stream.close()


def test_initial_gpu_frame_unavailable_is_retried_without_publishing_empty_image():
    retry, resume = threading.Event(), threading.Event()

    class Backend:
        count = 0

        def grab(self, *bounds):
            self.count += 1
            if self.count == 1:
                return None
            retry.set()
            assert resume.wait(1)
            return b"pixels"

        def close(self):
            pass

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A)
        assert retry.wait(1)
        assert stream.take() is None
        resume.set()
        assert wait_frame(stream).ppm == b"pixels"
    finally:
        resume.set()
        stream.close()


def test_reset_during_initialization_rebuilds_on_worker_without_waiting_on_ui():
    first_started, release_first = threading.Event(), threading.Event()
    owners, creations, closes = [], [], []

    class Backend:
        def __init__(self):
            creations.append(1)
            self.number = len(creations)
            owners.append(threading.get_ident())
            if self.number == 1:
                first_started.set()
                assert release_first.wait(1)

        def grab(self, *bounds):
            return str(self.number).encode()

        def close(self):
            closes.append(self.number)
            owners.append(threading.get_ident())

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A)
        assert first_started.wait(1)
        stream.reset()
        assert not release_first.is_set()
        stream.request(B)
        release_first.set()
        frame = wait_frame(stream)
        assert frame.ppm == b"2" and frame.bounds == B
        assert closes == [1]
    finally:
        release_first.set()
        stream.close()
    assert closes == [1, 2]
    assert len(set(owners)) == 1 and owners[0] != threading.get_ident()


def test_same_bounds_different_windows_do_not_reuse_in_flight_pixels():
    started, release = threading.Event(), threading.Event()

    class Backend:
        def grab_window(self, hwnd, pid, *bounds):
            if hwnd == 1:
                started.set()
                assert release.wait(1)
            return f"window {hwnd}".encode()

        def grab(self, *bounds):
            return b"monitor"

        def close(self):
            pass

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A, window=(1, 99))
        assert started.wait(1)
        assert stream.request(A, window=(2, 99))
        release.set()
        assert wait_frame(stream).ppm == b"window 2"
        assert stream.request(A)
        assert wait_frame(stream).ppm == b"monitor"
    finally:
        release.set()
        stream.close()


def test_popup_scene_change_invalidates_frame_even_when_bounds_match():
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow

    root = WindowLayer(AppWindow(1, 10, "Root"), A[:4])
    popup = WindowLayer(AppWindow(2, 10, "Dialog"), A[:4])
    old, new = WindowScene((root,)), WindowScene((root, popup))
    started, release = threading.Event(), threading.Event()

    class Backend:
        def grab_scene(self, scene, *bounds):
            if scene == old:
                started.set()
                assert release.wait(1)
                return b"old"
            return b"popup"

        def close(self):
            pass

    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A, scene=old)
        assert started.wait(1)
        assert stream.request(A, scene=new)
        release.set()
        frame = wait_frame(stream)
        assert frame.ppm == b"popup" and frame.scene == new
    finally:
        release.set()
        stream.close()


def test_hotplug_reset_rejects_old_pixels_even_when_display_bounds_are_identical():
    started, finish = threading.Event(), threading.Event()
    created = []
    class Backend:
        def __init__(self):
            self.number = len(created) + 1
            created.append(self)
        def grab(self, *bounds):
            if self.number == 1:
                started.set()
                assert finish.wait(1)
            return str(self.number).encode()
        def close(self):
            pass
    stream = CaptureStream(60, factory=Backend)
    try:
        stream.request(A)
        assert started.wait(1)
        stream.reset()
        stream.request(A)
        finish.set()
        assert wait_frame(stream).ppm == b'2'
        assert len(created) == 2
    finally:
        finish.set()
        stream.close()
