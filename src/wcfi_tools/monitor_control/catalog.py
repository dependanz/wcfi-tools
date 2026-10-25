"""Stable selections and asynchronous discovery for the target browser."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from .capture import Monitor
from .windows import AppWindow, enumerate_windows

APPLICATIONS = "Applications"
DISPLAYS = "Displays"
PRACTICE = "Practice"
Target = AppWindow | Monitor | None


def identity(target: Target):
    if isinstance(target, AppWindow):
        return target.key
    if isinstance(target, Monitor):
        return target.key
    return "practice"


@dataclass(frozen=True)
class Entry:
    target: Target
    label: str
    available: bool = True


class TargetCatalog:
    def __init__(self, monitors, windows):
        self.monitors, self.windows = monitors, windows
        self.mode = APPLICATIONS
        self.searches = {APPLICATIONS: "", DISPLAYS: "", PRACTICE: ""}
        self.selections: dict[str, Target] = {}

    def select(self, target: Target):
        self.selections[self.mode] = target

    def _all(self):
        if self.mode == APPLICATIONS:
            counts: dict[str, int] = {}
            entries = []
            for item in self.windows:
                counts[item.title] = counts.get(item.title, 0) + 1
                suffix = f" · Window {counts[item.title]}" if counts[item.title] > 1 else ""
                state = " · Minimized" if item.minimized else ""
                entries.append(Entry(item, item.title + suffix + state))
            return entries
        if self.mode == DISPLAYS:
            return [Entry(m, f"Display {i} · {m.label} · {m.device}")
                    for i, m in enumerate(self.monitors, 1)]
        return [Entry(None, "Practice · local test window")]

    def current(self) -> Entry | None:
        if self.mode not in self.selections:
            return None
        saved = self.selections[self.mode]
        for entry in self._all():
            if identity(entry.target) == identity(saved):
                self.select(entry.target)  # Keep title/state fresh, without changing identity.
                return entry
        title = saved.title if isinstance(saved, AppWindow) else saved.device
        state = "Disconnected · Waiting to reconnect" if isinstance(saved, Monitor) else "Unavailable"
        return Entry(saved, f"{title} · {state}", False)

    def entries(self) -> list[Entry]:
        entries = self._all()
        selected = self.current()
        if selected is not None and not selected.available:
            entries.insert(0, selected)
        terms = self.searches[self.mode].casefold().split()
        return [entry for entry in entries if all(t in entry.label.casefold() for t in terms)]


class WindowDiscovery:
    """Enumeration never blocks Tk or builds a queue of obsolete window lists."""

    def __init__(self, enumerate_fn=enumerate_windows, interval=1.5):
        self.results: queue.Queue = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.enumerate = enumerate_fn
        self.interval = interval
        self.thread = threading.Thread(target=self._run, name="wcfi-windows", daemon=True)
        self.thread.start()

    def request(self):
        self.wake.set()

    def take(self):
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None

    def _run(self):
        while not self.stop.is_set():
            try:
                result = self.enumerate()
            except (OSError, RuntimeError) as exc:
                result = exc
            try:
                self.results.get_nowait()
            except queue.Empty:
                pass
            self.results.put_nowait(result)
            self.wake.wait(self.interval)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
