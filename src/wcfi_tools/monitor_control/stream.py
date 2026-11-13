"""Capture off the UI thread and retain only the newest completed frame."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .capture import Capture
from .pacing import FramePacer
from .window_scene import WindowScene

Bounds = tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class Frame:
    """A completed frame; timestamps use the high-resolution perf_counter clock."""

    sequence: int
    bounds: Bounds
    ppm: bytes
    started_at: float
    captured_at: float
    scene: WindowScene | None = None


class CaptureStream:
    """One producer, one latest-frame slot, no queue of delayed pictures.

    GDI resources are constructed, used and disposed on the capture thread.
    Geometry changes invalidate in-flight results, including A → B → A switches.
    The UI never waits for a capture except during bounded shutdown.
    """

    def __init__(self, fps: int, *, factory=Capture):
        if not 1 <= fps <= 60:
            raise ValueError("fps must be between 1 and 60")
        self.fps = fps
        self._factory = factory
        self._condition = threading.Condition()
        self._bounds: Bounds | None = None
        self._window: tuple[int, int] | None = None
        self._scene: WindowScene | None = None
        self._generation = 0
        self._frame: Frame | None = None
        self._error: Exception | None = None
        self._closed = False
        self._reset_requested = False
        self._thread: threading.Thread | None = None

    def request(self, bounds: Bounds, *, window: tuple[int, int] | None = None,
                scene: WindowScene | None = None) -> bool:
        with self._condition:
            if self._closed:
                raise RuntimeError("Capture has been closed.")
            changed = bounds != self._bounds or window != self._window or scene != self._scene
            if changed:
                self._bounds = bounds
                self._window = window
                self._scene = scene
                self._generation += 1
                self._frame = None
                self._condition.notify_all()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="wcfi-capture", daemon=True)
                self._thread.start()
            return changed

    def take(self) -> Frame | None:
        with self._condition:
            if self._error is not None:
                error, self._error = self._error, None
                raise RuntimeError(f"Capture failed: {error}") from error
            frame, self._frame = self._frame, None
            return frame

    def pause(self):
        with self._condition:
            if self._bounds is not None or self._error is not None:
                self._generation += 1
                self._bounds = None
                self._frame = None
                self._error = None
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._bounds = None
            self._frame = None
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("The capture worker has not finished shutting down.")

    def reset(self):
        """Invalidate old output and rebuild graphics resources on their owning thread."""
        with self._condition:
            self._generation += 1
            self._bounds = None
            self._frame = None
            self._error = None
            self._reset_requested = True
            self._condition.notify_all()

    def _run(self):
        backend = None
        sequence = 0
        generation = -1
        pacer = FramePacer(self.fps)
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._closed or self._bounds is not None or self._reset_requested,
                    )
                    if self._closed:
                        return
                    reset, self._reset_requested = self._reset_requested, False
                    bounds = self._bounds
                    window = self._window
                    scene = self._scene
                    if generation != self._generation:
                        generation = self._generation
                        pacer = FramePacer(self.fps)
                if reset:
                    if backend is not None:
                        backend.close()
                        backend = None
                    continue
                started = time.perf_counter()
                try:
                    if backend is None:
                        backend = self._factory()
                    if scene is not None:
                        ppm = backend.grab_scene(scene, *bounds)
                    else:
                        ppm = (backend.grab_window(*window, *bounds) if window is not None
                               else backend.grab(*bounds))
                except Exception as exc:  # noqa: BLE001 - surface failures on the UI thread
                    with self._condition:
                        if generation == self._generation and not self._closed:
                            self._error = exc
                            self._bounds = None
                            self._frame = None
                    if backend is not None:
                        backend.close()
                        backend = None
                    continue
                captured = time.perf_counter()
                sequence += 1
                with self._condition:
                    if self._closed:
                        return
                    if generation == self._generation:
                        if ppm is not None:
                            self._frame = Frame(sequence, bounds, ppm, started, captured, scene)
                        delay = pacer.delay_ms(started, captured) / 1000
                        # wait_for's timeout bookkeeping uses time.monotonic on
                        # Python 3.12, which is a coarse GetTickCount64 clock on
                        # Windows. A plain timed wait avoids repeatedly sleeping
                        # until that clock ticks, which can add an entire frame.
                        self._condition.wait(timeout=delay)
        except Exception as exc:  # noqa: BLE001 - never leave a silently dead capture worker
            with self._condition:
                if not self._closed:
                    self._error = exc
                    self._bounds = None
                    self._frame = None
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:  # noqa: BLE001 - retain cleanup failures for the caller
                    with self._condition:
                        self._error = exc
