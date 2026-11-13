"""Observe-only home shadow of the open set: bind nodes, log policies, write nothing to Live."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Shadow the open set's Analysis Nodes at home; log only, apply nothing.",
                  no_args_is_help=True)

DEFAULT_ROLES = "RED,AC GUITAR,KIT,BASS,KEYS,EGUITAR"
DEFAULT_POLICIES = "v2,coordinator,red"
POLICIES = ("v2", "coordinator", "red")
STAGES = ("pre", "post")
MAX_SECONDS = 2400.
# The run's overall bound is the collection length plus an allowance for the wait until Play
# is pressed; a run whose wait outlasts it ends with max_seconds_reached. The default
# allowance is 30 minutes; --wait-minutes raises it to at most 12 hours for an unattended arm.
DEFAULT_WAIT_MINUTES = 30.
MAX_WAIT_MINUTES = 720.
MAX_WAIT_SECONDS = DEFAULT_WAIT_MINUTES * 60.
# run_home_shadow returns every end as a summary: a finished window or a transport stop is
# "completed", an early stop after collection began (such as host_state_changed) is
# "stopped", and a setup failure or a stop before anything was collected is "failed". Exit 1
# stays the setup-failure code the options checks use.
EXIT_CODES = {"completed": 0, "stopped": 2}
NOT_REPORTED = "not reported"


def _report(value):
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


def _fail(exc):
    typer.echo(str(exc), err=True)
    raise typer.Exit(1) from exc


def _stage(value, option):
    if value not in STAGES:
        raise ValueError(f"{option} must be pre or post, not {value!r}")
    return value


def _names(value, option, *, allowed=None):
    names = [name.strip() for name in value.split(",")]
    if not all(names) or len(set(names)) != len(names):
        raise ValueError(f"{option} needs distinct comma-separated names")
    unknown = [name for name in names if allowed is not None and name not in allowed]
    if unknown:
        raise ValueError(f"{option} accepts {', '.join(allowed)}; unknown: {', '.join(unknown)}")
    return names


def _duration(value, option, *, maximum):
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{option} must be above 0 and at most {maximum:g} seconds")
    return float(value)


def _inspector(instance):
    """Connect once; the shadow then asks this callable for inspect_session, as 'wcfi audio
    ableton inspect' does. The operation name is passed through so the shadow counts every
    call by name, and the snapshot carries the bridge descriptor's pid as host_pid: the Live
    process whose state is inspected, which the shadow ties the bound nodes to.
    """
    from ..ableton import client as bridge

    connected = bridge.connect(instance=instance)
    host_pid = connected.descriptor["pid"]

    def inspect(operation="inspect_session"):
        result = connected.call(operation)
        if operation == "inspect_session" and isinstance(result, dict):
            return {**result, "host_pid": host_pid}
        return result
    return inspect


def _count(summary, key):
    value = summary.get(key)
    return NOT_REPORTED if value is None else value


def _red_lock(summary):
    """One line for summary["red_lock"]: {"ready", "ready_bin_index", "gain_db"} or None."""
    lock = summary.get("red_lock")
    if not isinstance(lock, dict):
        return "not run" if "red_lock" in summary else NOT_REPORTED
    if not lock.get("ready"):
        return "not locked"
    return f"locked at bin {lock.get('ready_bin_index')}, gain {lock.get('gain_db')} dB"


def _summary_lines(summary, output):
    reason = summary.get("reason") or NOT_REPORTED
    detail = summary.get("detail")
    groups, proposals = _count(summary, "complete_groups"), _count(summary, "proposed_moves")
    bins, gaps = _count(summary, "coordinator_bins"), _count(summary, "gaps")
    return [f"outcome: {summary.get('outcome') or NOT_REPORTED}",
            f"reason: {reason}" if detail is None else f"reason: {reason}: {detail}",
            f"v2 groups: {groups}  proposals: {proposals}",
            f"coordinator bins: {bins}  gaps: {gaps}",
            f"red lock: {_red_lock(summary)}",
            f"output: {output.resolve()}"]


@app.command("bind")
def bind_command(
    stage: Annotated[str, typer.Option(help="Analysis Node position: pre (0) or post (1).")] = "post",
    roles: Annotated[str, typer.Option(help="Comma-separated role names, in policy order.")] = DEFAULT_ROLES,
):
    """Resolve roles to the running Live process's nodes and print the binding. Reads only."""
    try:
        selected_stage, selected_roles = _stage(stage, "--stage"), _names(roles, "--roles")
        from ..audio import session_nodes
        from ..audio.analysis_nodes import NodeBridge

        snapshots = session_nodes.inventory(NodeBridge())
        binding = session_nodes.bind_roles(snapshots, selected_roles, stage=selected_stage)
        _report(session_nodes.binding_record(binding, stage=selected_stage, roles=selected_roles))
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        _fail(exc)


@app.command("run")
def run_command(
    output: Annotated[Path, typer.Option(help="New output directory; an existing path is refused.")],
    seconds: Annotated[float, typer.Option(help="Collection length once playing, at most 2400.")] = 600,
    wait_for_play: Annotated[bool, typer.Option("--wait-for-play/--no-wait-for-play",
        help="Wait for the host transport to be playing before collecting.")] = True,
    coordinator_stage: Annotated[str, typer.Option(help="MixCoordinator taps: pre (dry) or post.")] = "pre",
    calibration_seconds: Annotated[float, typer.Option(help="Coordinator calibration window.")] = 30,
    policies: Annotated[str, typer.Option(help="Comma list from v2, coordinator, red.")] = DEFAULT_POLICIES,
    wait_minutes: Annotated[float, typer.Option(
        help="How long to wait for Play before giving up, in minutes; at most 720.")] = DEFAULT_WAIT_MINUTES,
    instance: str | None = None,
):
    """Collect from the open set once it plays and run the policies in shadow. Applies nothing.

    Exit 0: the window finished or the transport stopped. Exit 2: collection began but ended
    early (the reason line says why, for example host_state_changed). Exit 1: setup failed,
    or the run stopped before anything was collected. The wait for Play may take up to
    --wait-minutes (default 30) on top of --seconds before the run ends with
    max_seconds_reached.
    """
    try:
        if output.exists():
            raise ValueError(f"Output already exists; choose a new directory: {output}")
        selected_policies = tuple(_names(policies, "--policies", allowed=POLICIES))
        total = _duration(seconds, "--seconds", maximum=MAX_SECONDS)
        if not math.isfinite(wait_minutes) or not 0 < wait_minutes <= MAX_WAIT_MINUTES:
            raise ValueError(f"--wait-minutes must be above 0 and at most {MAX_WAIT_MINUTES:g}")
        wait_seconds = float(wait_minutes) * 60.
        calibration = _duration(calibration_seconds, "--calibration-seconds", maximum=MAX_SECONDS)
        if "coordinator" in selected_policies and calibration >= total:
            raise ValueError("--calibration-seconds must be shorter than --seconds for the coordinator")
        selected_stage = _stage(coordinator_stage, "--coordinator-stage")
        from ..audio import home_shadow
        from ..audio.analysis_nodes import NodeBridge

        config = home_shadow.ShadowConfig(output=output, seconds=total, wait_for_play=wait_for_play,
                                          coordinator_stage=selected_stage,
                                          calibration_seconds=calibration, policies=selected_policies,
                                          max_seconds=total + wait_seconds)
        summary = home_shadow.run_home_shadow(config, bridge_factory=NodeBridge,
                                              inspect=_inspector(instance))
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        _fail(exc)
    for line in _summary_lines(summary, output):
        typer.echo(line)
    code = EXIT_CODES.get(summary.get("outcome"), 1)
    if code:
        raise typer.Exit(code)
