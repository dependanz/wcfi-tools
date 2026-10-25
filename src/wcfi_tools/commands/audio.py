"""Local virtual-interface sources and ASIO output capture."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ..audio.emulator import SCENARIOS
from .ableton import app as ableton_app
from .full_mix_benchmark import app as full_mix_benchmark_app
from .gain_shadow import app as gain_shadow_app
from .master_benchmark import app as master_benchmark_app
from .mix_preference import app as mix_preference_app
from .role_analysis import app as role_analysis_app
from .songbench_benchmark import app as songbench_benchmark_app

app = typer.Typer(help="Local audio interface emulator (Windows ASIO prototype).", no_args_is_help=True)
emulate_app = typer.Typer(help="Supply raw real/procedural inputs to the WCFI ASIO driver.",
                          no_args_is_help=True)
driver_app = typer.Typer(help="Inspect the optional native WCFI ASIO driver.", no_args_is_help=True)
app.add_typer(emulate_app, name="emulate")
app.add_typer(driver_app, name="driver")
emulator_app = typer.Typer(help="Start the shared emulator service or open its desktop UI.",
                           no_args_is_help=True)
app.add_typer(emulator_app, name="emulator")

app.add_typer(ableton_app, name="ableton")
app.add_typer(role_analysis_app, name="analyze")
app.add_typer(master_benchmark_app, name="master-benchmark")
app.add_typer(full_mix_benchmark_app, name="full-mix-benchmark")
app.add_typer(gain_shadow_app, name="gain-shadow")
app.add_typer(songbench_benchmark_app, name="songbench-benchmark")
app.add_typer(mix_preference_app, name="mix-preference")


def _error(exc):
    typer.echo(str(exc), err=True)
    raise typer.Exit(1) from exc


def _report(data):
    typer.echo(json.dumps(data, indent=2, allow_nan=False))


@emulate_app.command("init")
def init_command(
    output: Annotated[Path, typer.Argument(help="New editable JSON profile.")],
    from_profile: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Create a channel map. Default vocals are test tones; replace with real files."""
    from ..audio.emulator import make_profile
    from ..soundcheck.profiles import write_json

    try:
        write_json(output, make_profile(soundcheck_profile=from_profile))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _error(exc)
    typer.echo(f"Created {output.resolve()}. Edit sources before starting the virtual inputs.")


@emulate_app.command("scenarios")
def scenarios_command():
    """List repeatable faults; these affect raw inputs, never Ableton controls."""
    _report(SCENARIOS)


@emulate_app.command("render")
def render_command(
    profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="New diagnostic directory.")],
    scenario: str = "baseline",
):
    """Render diagnostic stems locally without opening any audio device."""
    from ..audio.emulator import render

    try:
        report = render(profile, output, scenario=scenario)
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        _error(exc)
    typer.echo(f"Rendered {len(report['tracks'])} raw sources to {output.resolve()}")


@emulate_app.command("start")
def start_command(
    profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    scenario: str = "baseline",
    seconds: Annotated[float | None, typer.Option(min=.01, max=86400)] = None,
    dll: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Feed the open WCFI ASIO device until Ctrl+C or --seconds. No transport commands."""
    from ..audio.asio import stream

    def ready(status):
        typer.echo(f"Feeding WCFI Emulator: {status['rate']} Hz, {status['blocksize']} frames. "
                   "Ctrl+C stops the input generator.")

    try:
        report = stream(profile, scenario=scenario, dll=dll, seconds=seconds, ready=ready)
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        _error(exc)
    _report(report)


@driver_app.command("status")
def driver_status_command(
    dll: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Read registration/bridge state. Does not install or open an audio driver."""
    from ..audio.asio import NativeBridge, default_dll, registration

    try:
        report = {"registration": registration(), "dll": str(dll or default_dll())}
        try:
            with NativeBridge(dll) as bridge:
                report["bridge"] = bridge.status()
        except (OSError, RuntimeError) as exc:
            report["bridge"] = {"connected": False, "reason": str(exc)}
    except (OSError, RuntimeError, ValueError) as exc:
        _error(exc)
    _report(report)


@app.command("capture")
def capture_command(
    output: Annotated[Path, typer.Argument(help="New local output WAV.")],
    seconds: Annotated[float, typer.Option(min=.1, max=60)] = 10,
    channels: Annotated[str, typer.Option(help="One-based DAW output channels, comma separated.")] = "23,24",
    dll: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Capture DAW outputs locally, without recording/exporting in Ableton or uploading."""
    from ..audio.asio import capture

    try:
        selected = tuple(int(ch.strip()) for ch in channels.split(","))
        report = capture(output, seconds=seconds, channels=selected, dll=dll)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        _error(exc)
    _report(report)


@emulator_app.command("serve", hidden=True)
def serve_command(
    dll: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Internal service process; the public start command manages it."""
    from ..audio.service import serve

    try:
        serve(dll=dll)
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)


@emulator_app.command("start")
def emulator_start_command(
    foreground: bool = False,
    dll: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Start the emulator without a UI. Inputs stay silent until explicitly sent."""
    from ..audio.client import ensure_service, request
    from ..audio.service import serve

    try:
        if foreground:
            serve(dll=dll)
            return
        info = ensure_service(dll=dll)
        state = request("POST", "/api/start", descriptor=info)
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)
    typer.echo(f"Emulator online | {state['sample_rate']} Hz | 32 inputs / 32 outputs")
    typer.echo("Select WCFI Emulator in your DAW. Use launch to open its control window.")


@emulator_app.command("launch")
def emulator_launch_command(
    web: Annotated[bool, typer.Option(help="Open the same UI in a browser instead of Tauri.")] = False,
    dll: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Start the emulator and open its desktop control window."""
    from ..audio.client import launch

    try:
        _report(launch(web=web, dll=dll))
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)


@emulator_app.command("stop")
def emulator_stop_command():
    """Disable the ASIO device and cut simulated inputs; leave the UI available."""
    from ..audio.client import request

    try:
        request("POST", "/api/stop")
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)
    typer.echo("Emulator stopped. Restart it from the UI or with emulator start.")


@emulator_app.command("status")
def emulator_status_command():
    """Read the same running state and channel settings used by the desktop app."""
    from ..audio.client import request

    try:
        _report(request("GET", "/api/state"))
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)


@emulator_app.command("preset")
def emulator_preset_command(
    name: Annotated[str, typer.Argument(help="silence, two-tones, or band-probes. Replaces channel settings.")],
):
    """Load a source preset and pause inputs, without changing DAW transport."""
    from ..audio.client import request

    try:
        request("POST", "/api/preset", {"name": name})
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)
    typer.echo(f"Loaded {name}. Inputs paused. Use emulator send when ready.")


def _emulator_sending(enabled):
    from ..audio.client import request

    try:
        request("POST", "/api/sending", {"enabled": enabled})
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)
    typer.echo("Sending configured inputs." if enabled else "Inputs paused.")


@emulator_app.command("send")
def emulator_send_command():
    """Send enabled sources through the online emulator. Never starts DAW playback."""
    _emulator_sending(True)


@emulator_app.command("pause")
def emulator_pause_command():
    """Pause simulated inputs while keeping the ASIO device online."""
    _emulator_sending(False)


@emulator_app.command("analyze")
def emulator_analyze_command(
    channels: Annotated[str, typer.Option(help="One or two one-based DAW outputs.")] = "23,24",
):
    """Measure the last second of returned audio; no recording or export."""
    from ..audio.client import request

    try:
        ports = [int(channel.strip()) for channel in channels.split(",")]
        if not 1 <= len(ports) <= 2 or len(set(ports)) != len(ports) or any(n < 1 or n > 32 for n in ports):
            raise ValueError("Select one output or a pair, numbered 1-32")
        _report(request("GET", "/api/analysis?channels=" + ",".join(map(str, ports))))
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)


@emulator_app.command("shutdown")
def emulator_shutdown_command():
    """Stop the emulator and exit its background service."""
    from ..audio.client import request

    try:
        request("POST", "/api/shutdown")
    except (OSError, ValueError, RuntimeError) as exc:
        _error(exc)
    typer.echo("Emulator service exited.")
