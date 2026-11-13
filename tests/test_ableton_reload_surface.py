"""The Live timer remains isolated from runtime startup/reload/cleanup failures."""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def surface(tmp_path, monkeypatch):
    package_name = "_wcfi_isolated_surface_test"
    package = ModuleType(package_name)
    package.__path__ = [str(tmp_path)]
    monkeypatch.setitem(sys.modules, package_name, package)
    runtime_module = ModuleType(package_name + ".runtime")
    instances = []
    errors = {"init": None, "tick": None, "close": None}

    class RuntimeController:
        def __init__(self, song, application, package_root, state_dir):
            if errors["init"]:
                raise errors["init"]
            self.song = song
            self.application = application
            self.package_root = package_root
            self.state_dir = state_dir
            self.ticks = 0
            self.closes = 0
            instances.append(self)

        def tick(self):
            self.ticks += 1
            if errors["tick"]:
                raise errors["tick"]

        def close(self):
            self.closes += 1
            if errors["close"]:
                raise errors["close"]

    runtime_module.RuntimeController = RuntimeController
    monkeypatch.setitem(sys.modules, runtime_module.__name__, runtime_module)
    framework = ModuleType("_Framework")
    control_module = ModuleType("_Framework.ControlSurface")

    class ControlSurface:
        def __init__(self, host):
            self.host = host
            self.scheduled = []
            self.logs = []
            self.disconnections = 0

        def song(self):
            return self.host.song

        def application(self):
            return self.host.application

        def schedule_message(self, ticks, callback):
            if self.host.schedule_error:
                raise self.host.schedule_error
            self.scheduled.append((ticks, callback))

        def log_message(self, message):
            self.logs.append(message)

        def disconnect(self):
            self.disconnections += 1

    control_module.ControlSurface = ControlSurface
    monkeypatch.setitem(sys.modules, "_Framework", framework)
    monkeypatch.setitem(sys.modules, "_Framework.ControlSurface", control_module)
    source = Path(__file__).parents[1] / "src/wcfi_tools/ableton/remote/surface.py"
    copied = tmp_path / "surface.py"
    copied.write_bytes(source.read_bytes())
    config = tmp_path / "bridge-home.json"
    config.write_text(json.dumps({"state_dir": str(tmp_path / "discovery")}))
    spec = importlib.util.spec_from_file_location(package_name + ".surface", copied)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    host = SimpleNamespace(song=object(), application=object(), schedule_error=None)
    return SimpleNamespace(module=module, host=host, instances=instances, errors=errors, config=config)


def test_surface_owns_one_controller_and_schedules_only_its_timer(surface):
    bridge = surface.module.WCFIBridge(surface.host)
    assert len(surface.instances) == 1
    runtime = surface.instances[0]
    assert runtime.song is surface.host.song
    assert runtime.application is surface.host.application
    assert bridge._wcfi_runtime is runtime
    assert len(bridge.scheduled) == 1
    assert bridge.scheduled[0][0] == 1
    bridge.scheduled[0][1]()
    assert runtime.ticks == 1
    assert len(bridge.scheduled) == 2
    bridge.disconnect()
    assert runtime.closes == 1
    assert bridge.disconnections == 1
    for _ticks, callback in bridge.scheduled:
        callback()
    assert runtime.ticks == 1
    assert len(bridge.scheduled) == 2


@pytest.mark.parametrize("failure", ["configuration", "constructor", "schedule"])
def test_surface_startup_failures_are_contained_and_created_controller_is_closed(surface, failure):
    if failure == "configuration":
        surface.config.write_text("invalid JSON")
    elif failure == "constructor":
        surface.errors["init"] = ValueError("runtime construction failed")
    else:
        surface.host.schedule_error = RuntimeError("timer unavailable")
    bridge = surface.module.WCFIBridge(surface.host)
    assert bridge._wcfi_runtime is None
    assert not bridge.scheduled
    assert any("startup failed" in message.lower() for message in bridge.logs)
    if failure == "schedule":
        assert surface.instances[0].closes == 1
    else:
        assert not surface.instances
    bridge.disconnect()
    assert bridge.disconnections == 1


def test_surface_timer_exception_is_logged_and_next_tick_can_recover(surface):
    bridge = surface.module.WCFIBridge(surface.host)
    surface.errors["tick"] = ValueError("transient runtime check failed")
    bridge._wcfi_tick()
    assert len(bridge.scheduled) == 2
    assert any("transient runtime check failed" in message for message in bridge.logs)
    surface.errors["tick"] = None
    bridge._wcfi_tick()
    assert surface.instances[0].ticks == 2
    assert len(bridge.scheduled) == 3
    bridge.disconnect()


def test_runtime_cleanup_exception_does_not_skip_live_surface_disconnect(surface):
    bridge = surface.module.WCFIBridge(surface.host)
    surface.errors["close"] = OSError("socket cleanup failed")
    bridge.disconnect()
    assert bridge.disconnections == 1
    assert bridge._wcfi_closed
    bridge._wcfi_tick()
    assert surface.instances[0].ticks == 0
    assert any("socket cleanup failed" in message for message in bridge.logs)


def test_failed_startup_cleanup_still_does_not_abort_host_startup(surface):
    surface.host.schedule_error = RuntimeError("timer unavailable")
    surface.errors["close"] = OSError("socket cleanup failed")
    bridge = surface.module.WCFIBridge(surface.host)
    assert bridge._wcfi_runtime is None
    assert surface.instances[0].closes == 1
    assert any("startup failed" in message.lower() for message in bridge.logs)
    assert any("socket cleanup failed" in message for message in bridge.logs)
    bridge.disconnect()
    assert bridge.disconnections == 1
