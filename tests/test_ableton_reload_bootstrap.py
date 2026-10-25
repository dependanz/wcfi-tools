"""Import the actual installed Remote Script, not a substituted runtime loader."""
import importlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from test_ableton_bridge import Song

installer = importlib.import_module("wcfi_tools.ableton.install")


class StoppedSong(Song):
    @property
    def record_mode(self):
        return False

    @record_mode.setter
    def record_mode(self, _value):
        raise AssertionError("Bootstrap must not change recording")


@pytest.fixture
def installed_bridge(tmp_path, monkeypatch):
    library = tmp_path / "User Library"
    library.mkdir()
    state_dir = tmp_path / "discovery"
    installed = installer.install(user_library=library, state_dir=state_dir)
    package_root = Path(installed["installed"])
    framework = ModuleType("_Framework")
    framework.__path__ = []
    control_module = ModuleType("_Framework.ControlSurface")

    class ControlSurface:
        def __init__(self, c_instance):
            self.c_instance = c_instance
            self.logs = []
            self.callbacks = []
            self.disconnections = 0

        def song(self):
            return self.c_instance.song

        def application(self):
            return self.c_instance.application

        def schedule_message(self, delay, callback):
            self.callbacks.append((delay, callback))

        def log_message(self, message):
            self.logs.append(message)

        def disconnect(self):
            self.disconnections += 1

    control_module.ControlSurface = ControlSurface
    monkeypatch.setitem(sys.modules, "_Framework", framework)
    monkeypatch.setitem(sys.modules, "_Framework.ControlSurface", control_module)
    package_name = installer.PACKAGE_NAME
    previous_modules = {name: module for name, module in sys.modules.items()
                        if name == package_name or name.startswith(package_name + ".")}
    for name in previous_modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        package_name, package_root / "__init__.py",
        submodule_search_locations=[str(package_root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    song = StoppedSong()
    application = SimpleNamespace(get_major_version=lambda: 11,
                                  get_minor_version=lambda: 3,
                                  get_bugfix_version=lambda: 35)
    c_instance = SimpleNamespace(song=song, application=application)
    surfaces = []
    servers = []

    def create():
        surface = module.create_instance(c_instance)
        surfaces.append(surface)
        if surface._wcfi_runtime is not None:
            servers.append(surface._wcfi_runtime.current["server"])
        return surface

    try:
        spec.loader.exec_module(module)
        yield SimpleNamespace(module=module, create=create, surfaces=surfaces, servers=servers,
                              song=song, root=package_root, state=state_dir, installed=installed)
    finally:
        for surface in surfaces:
            if surface._wcfi_runtime is not None:
                servers.append(surface._wcfi_runtime.current["server"])
            surface.disconnect()
        for server in servers:
            server.close()
            if server.stop_thread is not None:
                server.stop_thread.join(2)
            if server.thread is not None:
                server.thread.join(2)
                assert not server.thread.is_alive()
        for name in list(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                del sys.modules[name]
        sys.modules.update(previous_modules)


def test_installed_package_starts_and_reloads_its_actual_runtime_without_session_changes(installed_bridge):
    context = installed_bridge
    surface = context.create()
    assert not [message for message in surface.logs if "failed" in message.lower()]
    assert surface._wcfi_runtime is not None, surface.logs
    assert len(surface.callbacks) == 1
    controller = surface._wcfi_runtime
    original = controller.current
    runtime_module = sys.modules["WCFI_Bridge.runtime"]
    assert type(controller) is runtime_module.RuntimeController
    assert original["adapter"].__class__.__module__.startswith("WCFI_Bridge._wcfi_runtime_")
    descriptor = json.loads(original["server"].path.read_text())
    assert descriptor["instance_id"] == original["adapter"].session_id
    assert descriptor["port"] == original["server"].server.server_port
    before = runtime_module.session_fingerprint(original["adapter"].inspect_session())
    reload_id = uuid.uuid4().hex
    job = original["server"].dispatcher.submit(reload_id, "reload_bridge", {
        "reload_id": reload_id,
        "session_id": original["adapter"].session_id,
        "expected_bundle_id": original["bundle_id"],
    })
    controller.tick()
    assert job.response["ok"], job.response
    assert job.response["result"]["state"] == "accepted"
    assert controller.current is original
    controller.tick()
    replacement = controller.current
    context.servers.append(replacement["server"])
    assert controller.last_reload["state"] == "complete", controller.last_reload
    assert replacement is not original
    assert replacement["bundle_id"] == context.installed["bundle_id"]
    assert replacement["adapter"].session_id != original["adapter"].session_id
    assert runtime_module.session_fingerprint(replacement["adapter"].inspect_session()) == before
    assert original["server"].closed
    assert not original["server"].path.exists()
    assert original["package_name"] not in sys.modules
    assert len(list(context.state.glob("bridge-*.json"))) == 1
    assert not list((context.root / "runtimes").glob("**/__pycache__"))
    assert not context.song.moves
    assert context.song.undo_steps == 0
    for track in context.song.tracks + context.song.return_tracks + [context.song.master_track]:
        assert not track.mixer_device.volume.writes
        assert not track.mixer_device.panning.writes
        for device in track.devices:
            assert all(not parameter.writes for parameter in device.parameters)
    surface.disconnect()
    assert not list(context.state.glob("bridge-*.json"))
    for _delay, callback in surface.callbacks:
        callback()
    assert not list(context.state.glob("bridge-*.json"))


def test_reselected_surface_does_not_claim_changed_bootstrap_is_loaded(installed_bridge):
    context = installed_bridge
    original_surface = context.create()
    assert original_surface._wcfi_runtime is not None, original_surface.logs
    original_surface.disconnect()
    runtime_module = sys.modules["WCFI_Bridge.runtime"]
    loaded = dict(runtime_module.LOADED_BOOTSTRAP_HASHES)
    surface_file = context.root / "surface.py"
    surface_file.write_bytes(surface_file.read_bytes() + b"\n# bootstrap changed after initial import\n")
    pointer_path = context.root / "runtime-current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["bootstrap_hashes"] = runtime_module.bootstrap_hashes(context.root)
    pointer_path.write_text(json.dumps(pointer))
    new_surface = context.create()
    assert runtime_module.LOADED_BOOTSTRAP_HASHES == loaded
    assert new_surface._wcfi_runtime is None
    assert any("bootstrap_update_required" in message for message in new_surface.logs)
    assert not list(context.state.glob("bridge-*.json"))
