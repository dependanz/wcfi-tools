"""Background Ableton inspection/control, with no transport operations."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ..ableton import client as bridge

app = typer.Typer(help="Control Ableton through WCFI_Bridge without using your mouse.",
                  no_args_is_help=True)


def report(value):
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


def failure(exc):
    if isinstance(exc, typer.Exit):
        raise exc
    report({"error": str(exc), "code": getattr(exc, "code", "local_error"),
            "details": getattr(exc, "details", {})})
    raise typer.Exit(1) from exc


def mutation_report(result):
    report(result)
    if not result.get("dry_run") and not result.get("verified"):
        raise typer.Exit(2)


@app.command("install")
def install_command(
    user_library: Annotated[Path | None, typer.Option(file_okay=False, exists=True)] = None,
    state_dir: Annotated[Path | None, typer.Option(file_okay=False)] = None,
):
    """Install the script. One-time selection in Live Preferences activates it."""
    from ..ableton.install import install

    try:
        report(install(user_library=user_library, state_dir=state_dir))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("status")
def status_command(instance: str | None = None):
    """Read connected Live version and runtime capabilities."""
    try:
        report(bridge.connect(instance=instance).call("status"))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("reload")
def reload_command(
    install_bundle: Annotated[bool, typer.Option("--install", help="Stage the current managed bridge bundle before reloading.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Check the installed reload plan without changing the runtime.")] = False,
    discard_receipts: Annotated[bool, typer.Option(help="Explicitly discard bridge insertion rollback receipts; does not remove host devices.")] = False,
    timeout: Annotated[float, typer.Option(min=.1, max=120, help="Total seconds to verify the new local runtime.")] = 20,
    user_library: Annotated[Path | None, typer.Option(file_okay=False, exists=True, help="Library to stage into; requires --install.")] = None,
    state_dir: Annotated[Path | None, typer.Option(file_okay=False, help="Bridge discovery directory; also used when installing.")] = None,
    instance: str | None = None,
):
    """Reload a managed code bundle in stopped Live without UI or transport control."""
    try:
        if install_bundle and dry_run:
            raise bridge.AbletonError("--install changes files and cannot be combined with --dry-run. "
                                      "Install separately, then dry-run the staged bundle.", code="invalid_argument")
        if user_library is not None and not install_bundle:
            raise bridge.AbletonError("--user-library requires --install", code="invalid_argument")
        installed = None
        if install_bundle:
            from ..ableton.install import install

            installed = install(user_library=user_library, state_dir=state_dir)
            if installed.get("restart_required"):
                raise bridge.AbletonError("Bundle installed, but the bootstrap or discovery configuration "
                    "changed. Save your set and restart Live once to activate it; do not press Play.",
                    code="bootstrap_update_required", details={"installation": installed})
        connected = bridge.connect(instance=instance, folder=state_dir)
        result = bridge.reload_bridge(connected,
            expected_bundle_id=installed["bundle_id"] if installed is not None else None,
            dry_run=dry_run, discard_receipts=discard_receipts, timeout=timeout)
        if installed is not None:
            result = {**result, "installation": installed}
        mutation_report(result)
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("inspect")
def inspect_command(
    output: Annotated[Path | None, typer.Option(help="New JSON snapshot file; refuses overwrite.")] = None,
    instance: str | None = None,
):
    """Read tracks, groups, returns, Master, routing, racks, devices and parameters."""
    try:
        snapshot = bridge.connect(instance=instance).call("inspect_session")
        if output:
            with output.open("x", encoding="utf-8") as stream:
                json.dump(snapshot, stream, indent=2, allow_nan=False)
            typer.echo(f"Saved session inspection to {output.resolve()}")
        else:
            report(snapshot)
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("fader")
def fader_command(
    track: str,
    db: Annotated[float, typer.Option(help="Absolute dB, or a dB change with --relative.")],
    relative: bool = False,
    dry_run: bool = False,
    instance: str | None = None,
):
    """Set a track/return/Master fader using Live's dB conversion and read back."""
    try:
        mutation_report(bridge.set_fader(bridge.connect(instance=instance), track, db,
                                        relative=relative, dry_run=dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("parameters")
def parameters_command(track: str, device: str, instance: str | None = None):
    """List a device's host-exposed parameters, units, ranges and current values."""
    try:
        snapshot = bridge.connect(instance=instance).call("inspect_session")
        report(bridge.choose(list(bridge.devices(bridge.choose_track(snapshot, track))),
                             device, "Device"))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("set-parameter")
def parameter_command(
    track: str,
    device: str,
    parameter: str,
    value: Annotated[float, typer.Option(help="Host parameter value; see parameters first.")],
    normalized: Annotated[bool, typer.Option(help="Interpret value as 0..1 across the reported range.")] = False,
    dry_run: bool = False,
    instance: str | None = None,
):
    """Set one exposed parameter, rejecting disabled/automated/stale controls."""
    try:
        mutation_report(bridge.set_parameter(bridge.connect(instance=instance), track, device,
                                            parameter, value, normalized=normalized, dry_run=dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("move-device")
def move_command(
    track: str,
    device: str,
    index: Annotated[int, typer.Option(min=0, help="Zero-based final position in destination.")],
    to_track: str | None = None,
    to_chain: Annotated[str | None, typer.Option(help="Rack chain ID from inspect.")] = None,
    dry_run: bool = False,
    instance: str | None = None,
):
    """Move an existing device; default destination is its current chain."""
    try:
        result = bridge.move_device(bridge.connect(instance=instance), track, device, index,
                                    to_track=to_track, to_chain=to_chain, dry_run=dry_run)
        mutation_report(result)
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("eq8-state")
def eq8_state_command(track: str, device: str, instance: str | None = None):
    """Read EQ Eight's native M/S mode, edit channel and oversampling state."""
    try:
        report(bridge.inspect_eq8_state(bridge.connect(instance=instance), track, device))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("set-eq8-mode")
def eq8_mode_command(
    track: str,
    device: str,
    mode: Annotated[int, typer.Option(min=0, max=2, help="0 Stereo, 1 Left/Right, 2 Mid/Side.")],
    dry_run: bool = False,
    instance: str | None = None,
):
    """Set only EQ Eight's native mode while Play and Record are stopped."""
    try:
        mutation_report(bridge.set_eq8_mode(bridge.connect(instance=instance), track, device,
                                            mode, dry_run=dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("browse-devices")
def browse_devices_command(
    parent: Annotated[str | None, typer.Option(help="Exact browser item/category ID from a previous page.")] = None,
    offset: Annotated[int, typer.Option(min=0, help="Page offset within the selected folder.")] = 0,
    limit: Annotated[int, typer.Option(min=1, max=100, help="Maximum items to return.")] = 100,
    instance: str | None = None,
):
    """List Audio Effects browser items without loading or previewing audio."""
    try:
        report(bridge.browse_devices(bridge.connect(instance=instance), parent=parent,
                                    offset=offset, limit=limit))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("insert-device")
def insert_device_command(
    track: str,
    browser_item_id: str,
    index: Annotated[int, typer.Option(min=0, help="Zero-based final position; chain length appends.")],
    dry_run: bool = False,
    instance: str | None = None,
):
    """Insert an exact browsed Audio Effect while Play and Record are stopped."""
    try:
        mutation_report(bridge.insert_device(bridge.connect(instance=instance), track,
                                             browser_item_id, index, dry_run=dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("remove-inserted-device")
def remove_inserted_device_command(
    insertion_id: str,
    dry_run: bool = False,
    instance: str | None = None,
):
    """Revert a bridge insertion by receipt, refusing changed device chains."""
    try:
        mutation_report(bridge.remove_inserted_device(bridge.connect(instance=instance),
                                                      insertion_id, dry_run=dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("insertion-context")
def insertion_context_command(track: str, instance: str | None = None):
    """Read insertion selection/mode context, including after unresolved loads."""
    try:
        report(bridge.inspect_insertion_context(bridge.connect(instance=instance), track))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)


@app.command("check-insertion-context")
def check_insertion_context_command(
    track: str,
    dry_run: bool = False,
    instance: str | None = None,
):
    """Test stopped Live selection/mode restoration without loading an effect."""
    try:
        mutation_report(bridge.probe_insertion_context(bridge.connect(instance=instance),
                                                       track, dry_run=dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        failure(exc)
