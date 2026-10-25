"""Monitor CLI checks without capture, native input, or a desktop session."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.commands import monitor_control

runner = CliRunner()


def _runtime(monkeypatch, name: str, **attributes):
    module = ModuleType(f"wcfi_tools.monitor_control.{name}")
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(monitor_control.sys, "platform", "win32")


def test_commands_are_discoverable():
    result = runner.invoke(app, ["monitor-control", "--help"])
    assert result.exit_code == 0
    assert "launch" in result.output
    assert "list" in result.output


@pytest.mark.parametrize("command", ["launch", "list"])
def test_non_windows_reports_requirement(monkeypatch, command):
    monkeypatch.setattr(monitor_control.sys, "platform", "linux")
    result = runner.invoke(app, ["monitor-control", command])
    assert result.exit_code == 1
    assert "requires Windows" in result.output


def test_launch_defaults_do_not_require_setup(monkeypatch, tmp_path, windows):
    from wcfi_tools import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "missing.toml")
    launch = Mock()
    _runtime(monkeypatch, "app", launch=launch)
    result = runner.invoke(app, ["monitor-control", "launch"])
    assert result.exit_code == 0, result.output
    launch.assert_called_once_with(practice=False, fps=60, monitor=None)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["--practice", "--fps", "10"], {"practice": True, "fps": 10, "monitor": None}),
            (["--monitor", "2"], {"practice": False, "fps": 60, "monitor": 2}),
    ],
)
def test_launch_passes_options(monkeypatch, windows, options, expected):
    launch = Mock()
    _runtime(monkeypatch, "app", launch=launch)
    result = runner.invoke(app, ["monitor-control", "launch", *options])
    assert result.exit_code == 0, result.output
    launch.assert_called_once_with(**expected)


@pytest.mark.parametrize(
    "options",
    [
        ["--fps", "0"],
        ["--fps", "61"],
        ["--monitor", "0"],
        ["--practice", "--monitor", "1"],
    ],
)
def test_invalid_options_never_launch(monkeypatch, windows, options):
    launch = Mock()
    _runtime(monkeypatch, "app", launch=launch)
    result = runner.invoke(app, ["monitor-control", "launch", *options])
    assert result.exit_code == 2
    launch.assert_not_called()


def test_launch_runtime_failure_is_readable(monkeypatch, windows):
    _runtime(monkeypatch, "app", launch=Mock(side_effect=RuntimeError("No desktop available")))
    result = runner.invoke(app, ["monitor-control", "launch"])
    assert result.exit_code == 1
    assert "Unable to launch monitor control: No desktop available" in result.output


def test_missing_tk_explains_requirement(monkeypatch, windows):
    failure = ModuleNotFoundError("No module named 'tkinter'", name="tkinter")
    _runtime(monkeypatch, "app", launch=Mock(side_effect=failure))
    result = runner.invoke(app, ["monitor-control", "launch"])
    assert result.exit_code == 1
    assert "Install Python with Tcl/Tk support" in result.output


def test_list_reports_indices_and_desktop_geometry(monkeypatch, windows):
    displays = [
        SimpleNamespace(
            device=r"\\.\DISPLAY1", label="Laptop", left=0, top=0,
            width=1920, height=1080, primary=True,
        ),
        SimpleNamespace(
            device=r"\\.\DISPLAY2", label="Projector", left=-1280, top=100,
            width=1280, height=720, primary=False,
        ),
    ]
    _runtime(monkeypatch, "capture", enumerate_monitors=lambda: displays)
    result = runner.invoke(app, ["monitor-control", "list"])
    assert result.exit_code == 0, result.output
    assert "1: Laptop [primary]" in result.output
    assert "2: Projector" in result.output
    assert "1280 x 720 at (-1280, 100)" in result.output
    assert r"\\.\DISPLAY2" in result.output


def test_list_empty(monkeypatch, windows):
    _runtime(monkeypatch, "capture", enumerate_monitors=list)
    result = runner.invoke(app, ["monitor-control", "list"])
    assert result.exit_code == 0
    assert "No connected displays found" in result.output


def test_list_runtime_failure_is_readable(monkeypatch, windows):
    _runtime(monkeypatch, "capture", enumerate_monitors=Mock(side_effect=OSError("Access denied")))
    result = runner.invoke(app, ["monitor-control", "list"])
    assert result.exit_code == 1
    assert "Unable to list displays: Access denied" in result.output
