"""Experimental, independent digital-mixer commands."""

from __future__ import annotations

import json
import time
from typing import Annotated

import typer

from ..mixer.touchmix import (
    DEFAULT_PORT, OBSERVED_INPUT_FADERS, TouchMixClient, TouchMixError,
    plan_input_fader,
)

app = typer.Typer(help="Native mixer drivers and simulators (experimental).",
                  no_args_is_help=True)
touchmix_app = typer.Typer(help="TouchMix-30 Pro TCP driver; NOT service-qualified.",
                           no_args_is_help=True)
app.add_typer(touchmix_app, name="touchmix")


def _print(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, allow_nan=False))


def _fail(exc: Exception) -> None:
    typer.echo(f"TouchMix: {exc}", err=True)
    raise typer.Exit(1) from exc


@touchmix_app.command("identify")
def identify(
    host: Annotated[str, typer.Option(help="Literal private IPv4 address of the mixer.")],
    port: Annotated[int, typer.Option(min=1, max=65535)] = DEFAULT_PORT,
    timeout: Annotated[float, typer.Option(min=0.25, max=60)] = 8.0,
    simulator: Annotated[bool, typer.Option("--simulator", help="Explicitly allow a localhost simulator.")] = False,
) -> None:
    """Read the server greeting; sends no application data."""
    try:
        with TouchMixClient(host, port, timeout=timeout, allow_loopback=simulator) as client:
            _print(client.identify())
    except (TouchMixError, OSError, ValueError) as exc:
        _fail(exc)


@touchmix_app.command("inspect")
def inspect(
    host: Annotated[str, typer.Option(help="Literal private IPv4 address of the mixer.")],
    port: Annotated[int, typer.Option(min=1, max=65535)] = DEFAULT_PORT,
    timeout: Annotated[float, typer.Option(min=0.25, max=60)] = 12.0,
    trace: Annotated[bool, typer.Option("--trace", help="Print sanitized frame progress to stderr.")] = False,
    simulator: Annotated[bool, typer.Option("--simulator", help="Explicitly allow a localhost simulator.")] = False,
) -> None:
    """Read greeting, exchange client identification, and parse the configuration XML in memory."""
    try:
        with TouchMixClient(host, port, timeout=timeout, allow_loopback=simulator) as client:
            config, faders = client.retrieve_configuration(
                trace=(lambda event: typer.echo(json.dumps(event), err=True)) if trace else None
            )
            _print({"device": client.identify(), "configuration": config,
                    "faders": [
                        {"channel": f.channel, "channel_type": f.channel_type,
                         "parameter_index": f.parameter_index,
                         "xml_db_snapshot": f.xml_db,
                         "test_write_candidate": (f.channel in OBSERVED_INPUT_FADERS and
                                                  f.channel_type == 1 and
                                                  f.parameter_index == OBSERVED_INPUT_FADERS[f.channel][0])}
                        for f in faders.values()],
                    "note": "XML configuration values are a snapshot, not proven live readback"})
    except (TouchMixError, OSError, ValueError) as exc:
        _fail(exc)


@touchmix_app.command("observe")
def observe(
    channel: Annotated[str, typer.Option(help="Exact channel name, e.g., 'In 1'.")],
    host: Annotated[str, typer.Option(help="Literal private IPv4 mixer address.")],
    port: Annotated[int, typer.Option(min=1, max=65535)] = DEFAULT_PORT,
    samples: Annotated[int, typer.Option(min=2, max=5, help="Number of separate configuration snapshots.")] = 2,
    interval: Annotated[float, typer.Option(min=0, max=30, help="Seconds between samples; move the console fader during this interval.")] = 8.0,
    timeout: Annotated[float, typer.Option(min=0.25, max=60)] = 30.0,
    simulator: Annotated[bool, typer.Option("--simulator", help="Explicitly allow a localhost simulator.")] = False,
) -> None:
    """Observe a channel across separate configuration downloads; never sends parameter writes.

    XML values are configuration snapshots, not independently proven live fader readback.
    """
    try:
        readings: list[dict[str, object]] = []
        start = time.monotonic()
        for i in range(samples):
            with TouchMixClient(host, port, timeout=timeout,
                                allow_loopback=simulator) as client:
                config, faders = client.retrieve_configuration()
                if channel not in faders:
                    raise TouchMixError(f"Channel {channel!r} not found in mixer configuration")
                fader = faders[channel]
                if fader.xml_db is None:
                    raise TouchMixError(f"Channel {channel!r} has no numeric XML level")
                readings.append({
                    "sample": i + 1,
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                    "config_version": config["config_version"],
                    "parameter_index": fader.parameter_index,
                    "xml_db_snapshot": fader.xml_db,
                })
            if i < samples - 1:
                typer.echo(f"Snapshot {i + 1}/{samples} captured. Move the isolated physical fader during the next {interval:g} seconds if testing console-to-CLI observation.", err=True)
                time.sleep(interval)
        indices = {r["parameter_index"] for r in readings}
        versions = {r["config_version"] for r in readings}
        if len(indices) != 1 or len(versions) != 1:
            raise TouchMixError("Mixer parameter identity or version changed between samples")
        first = readings[0]["xml_db_snapshot"]
        last = readings[-1]["xml_db_snapshot"]
        _print({"channel": channel, "read_only": True, "samples": readings,
                "delta_db_first_to_last": round(last - first, 6),
                "changed_by_at_least_0_01_db": abs(last - first) >= 0.01,
                "note": "Configuration snapshots only; movement and causation require physical confirmation."})
    except (TouchMixError, OSError, ValueError) as exc:
        _fail(exc)


@touchmix_app.command("fader")
def fader(
    channel: Annotated[str, typer.Option(help="Exact input name, e.g., 'In 1'.")],
    value: Annotated[float, typer.Option(help="Experimental UI-normalized 0.35..0.75; NOT dB.")],
    host: Annotated[str, typer.Option(help="Literal private IPv4 mixer address.")],
    port: Annotated[int, typer.Option(min=1, max=65535)] = DEFAULT_PORT,
    timeout: Annotated[float, typer.Option(min=0.25, max=60)] = 12.0,
    trace: Annotated[bool, typer.Option("--trace", help="Print sanitized frame progress to stderr.")] = False,
    observe_replies_seconds: Annotated[float, typer.Option("--observe-replies-seconds", min=0, max=10, help="After --apply, listen for frames without sending additional commands; not a write acknowledgment.")] = 2.0,
    apply: Annotated[bool, typer.Option("--apply", help="Transmit one experimental fader frame; default is dry-run.")] = False,
    confirm_isolated: Annotated[bool, typer.Option("--confirm-isolated", help="Affirm this input is physically isolated and it is safe to move.")] = False,
    confirm_channel: Annotated[str | None, typer.Option(help="Repeat the exact input name to enable --apply.")] = None,
    simulator: Annotated[bool, typer.Option("--simulator", help="Explicitly allow a localhost simulator.")] = False,
) -> None:
    """Plan or explicitly attempt one isolated input-fader movement (not service-safe)."""
    try:
        if apply and (not confirm_isolated or confirm_channel != channel):
            raise TouchMixError("--apply requires --confirm-isolated and matching --confirm-channel")
        with TouchMixClient(host, port, timeout=timeout, allow_loopback=simulator) as client:
            config, faders = client.retrieve_configuration(
                trace=(lambda event: typer.echo(json.dumps(event), err=True)) if trace else None
            )
            plan = plan_input_fader(channel, value, config, faders)
            if not apply:
                _print({**plan, "sent": False, "verified": False,
                        "result": "dry_run_no_fader_write"})
            else:
                _print(client.send_test_fader(channel, value, config, faders,
                                              observe_replies_seconds=observe_replies_seconds))
    except (TouchMixError, OSError, ValueError) as exc:
        _fail(exc)


@touchmix_app.command("simulate")
def simulate(
    port: Annotated[int, typer.Option(min=0, max=65535)] = 58211,
) -> None:
    """Serve one synthetic TouchMix connection on localhost only; no actual mixer is contacted."""
    from ..mixer.simulator import TouchMixSimulator

    simulator = TouchMixSimulator(port=port)
    typer.echo("Synthetic TouchMix simulator, listening on 127.0.0.1 "
               f"port {port}; handles one client and then exits.")
    simulator.serve_once()
    _print({"received_writes": [
        {"identifier": f"0x{i:08X}", "value": v}
        for i, v in simulator.received_writes]})
