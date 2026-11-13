"""Agent-facing Control Node configuration; dry-run by default."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated

import typer

from ..audio.control_node import PARAMETERS, bind, compare_telemetry, configure_one

app = typer.Typer(help="Inspect/configure WCFI Control Node through the existing Ableton bridge.",
                  no_args_is_help=True)


def _report(value):
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


@app.command("inspect")
def inspect_command(track: str, device: str = "WCFI Control Node", instance: str | None = None,
                    output: Annotated[Path | None, typer.Option(help="New ownership binding JSON; refuses overwrite.")] = None,
                    verify_meters: Annotated[bool, typer.Option(help="Read twice to check whether the meter sequence advances.")] = False):
    """Read a node between Analysis Nodes without changing any host state."""
    from ..ableton.client import connect
    try:
        connected = connect(instance=instance)
        result = bind(connected.call("inspect_session"), track, device)
        if verify_meters and result["telemetry"].get("available"):
            first = result
            time.sleep(.15)
            result = bind(connected.call("inspect_session"), first["track_id"], first["device_id"])
            result["telemetry"] = compare_telemetry(first, result)
        if output:
            with output.open("x", encoding="utf-8") as file:
                json.dump(result, file, indent=2, allow_nan=False)
        _report(result)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        _report({"error": str(exc)})
        raise typer.Exit(1) from exc


@app.command("set")
def set_command(
    binding: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    setting: Annotated[str, typer.Option(help="One of: " + ", ".join(PARAMETERS))],
    value: Annotated[float, typer.Option(help="Parameter value in its displayed units (dB, or 0/1 for auto).")],
    apply: Annotated[bool, typer.Option("--apply", help="Actually write; omission performs a dry-run.")] = False,
    receipt: Annotated[Path | None, typer.Option(help="New durable JSONL journal; required with --apply.")] = None,
    instance: str | None = None,
):
    """Configure one parameter; preserve manual changes and reject stale bindings."""
    from ..ableton.client import connect
    try:
        if apply and receipt is None:
            raise ValueError("--apply requires a new --receipt path")
        owned = json.loads(binding.read_text(encoding="utf-8"))
        connected = connect(instance=instance or owned["session_id"])
        if receipt:
            with receipt.open("x", encoding="utf-8") as file:
                def journal(event):
                    file.write(json.dumps(event, allow_nan=False) + "\n")
                    file.flush()
                    os.fsync(file.fileno())
                result = configure_one(connected, owned, setting, value, apply=apply, journal=journal)
        else:
            result = configure_one(connected, owned, setting, value)
        _report(result)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        _report({"error": str(exc), "action": "stop; inspect before further commands", "automatic_retry": False})
        raise typer.Exit(1) from exc
