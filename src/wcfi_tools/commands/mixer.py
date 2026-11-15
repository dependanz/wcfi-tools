"""Experimental, independent digital-mixer commands."""

from __future__ import annotations

import json
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
    simulator: Annotated[bool, typer.Option("--simulator", help="Explicitly allow a localhost simulator.")] = False,
) -> None:
    """Read greeting, exchange client identification, and parse the configuration XML in memory."""
    try:
        with TouchMixClient(host, port, timeout=timeout, allow_loopback=simulator) as client:
            config, faders = client.retrieve_configuration()
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


@touchmix_app.command("fader")
def fader(
    channel: Annotated[str, typer.Option(help="Exact input name, e.g., 'In 1'.")],
    value: Annotated[float, typer.Option(help="Experimental UI-normalized 0.35..0.75; NOT dB.")],
    host: Annotated[str, typer.Option(help="Literal private IPv4 mixer address.")],
    port: Annotated[int, typer.Option(min=1, max=65535)] = DEFAULT_PORT,
    timeout: Annotated[float, typer.Option(min=0.25, max=60)] = 12.0,
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
            config, faders = client.retrieve_configuration()
            plan = plan_input_fader(channel, value, config, faders)
            if not apply:
                _print({**plan, "sent": False, "verified": False,
                        "result": "dry_run_no_fader_write"})
            else:
                _print(client.send_test_fader(channel, value, config, faders))
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
