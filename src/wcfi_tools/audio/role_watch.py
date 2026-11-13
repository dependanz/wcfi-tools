"""Passive node analysis, using a separate capture thread and read-only bridge RPCs."""
from __future__ import annotations

import threading
import time

from ..soundcheck.profiles import write_json
from .analysis_nodes import NodeBridge
from .role_stream import RoleAnalysisStream


def watch_nodes(output, *, seconds=30, window_seconds=10, interval=5,
                bridge_factory=NodeBridge, client=None):
    """No service start, source playback, audio output or Ableton writes.

    Host-clock windows are contemporaneous. Conservative waveform alignment is
    attempted; ambiguous/weak matches disable paired change attribution.
    Chain snapshots are polled and not sample-atomic; proposals are notes only.
    """
    if output.exists():
        raise ValueError("Choose a new output directory")
    if not .5 <= window_seconds <= 30 or not .1 <= interval <= 30 or not 1 <= seconds <= 3600:
        raise ValueError("Invalid watch duration, window or interval")
    bridge = bridge_factory()
    handles, selected, streams = {}, {}, {}
    stop = threading.Event()
    errors = []
    lock = threading.Lock()
    try:
        for identity in bridge.discover():
            handle = bridge.open(identity)
            if not handle:
                continue
            _, status = bridge.read(handle, 0)
            if status is None:
                bridge.close(handle)
                continue
            name = status.label.decode("utf-8", errors="replace").strip().upper()
            if name not in ("RED", "KIT") or status.position not in (0, 1):
                bridge.close(handle)
                continue
            key = (name, status.position)
            if key in selected:
                bridge.close(handle)
                raise ValueError("Duplicate RED/KIT node labels; select a unique before/after pair")
            handles[identity] = handle
            selected[key] = {"identity": identity, "cursor": int(status.written_frame),
                             "pid": int(status.pid), "rate": round(status.sample_rate),
                             "channels": int(status.channels)}
        for name in ("RED", "KIT"):
            if (name, 0) not in selected or (name, 1) not in selected:
                continue
            a, b = selected[(name, 0)], selected[(name, 1)]
            if (a["pid"], a["rate"], a["channels"]) != (b["pid"], b["rate"], b["channels"]):
                raise ValueError("Paired nodes must share host, sample rate and channel count")
            streams[name] = RoleAnalysisStream(name, a["rate"], a["channels"],
                seconds=window_seconds, interval=interval)
        if not streams:
            raise RuntimeError("No unique RED or KIT before/after node pair is available. No DAW action was taken.")
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "watch-started.json", {"tracks": list(streams), "read_only": True,
            "seconds": seconds, "window_seconds": window_seconds, "interval_seconds": interval,
            "audio_saved": False, "alignment": "host sample clock plus conservative waveform estimate",
            "chain_state": "polled host-exposed state; proprietary hidden state unknown"})

        def collect():
            try:
                while not stop.is_set():
                    for (name, position), item in selected.items():
                        if name not in streams:
                            continue
                        block, status = bridge.read(handles[item["identity"]], item["cursor"])
                        if status is None:
                            continue
                        previous = item["cursor"]
                        item["cursor"] = int(status.first_frame) + len(block)
                        if not len(block):
                            continue
                        age_ms = (bridge.clock_ns() - status.callback_ns) / 1e6 if status.callback_ns else -1
                        valid = (bool(status.flags & 1) and bool(status.active)
                            and 0 <= age_ms < 500 and round(status.sample_rate) == item["rate"]
                            and status.channels == item["channels"]
                            and int(status.first_frame) == previous)
                        first_host = (int(status.host_sample) + int(status.block_size)
                                      - int(status.written_frame) + int(status.first_frame))
                        with lock:
                            streams[name].push("pre_fx" if position == 0 else "post_fx",
                                block[:, :item["channels"]], max(0, first_host),
                                epoch=int(status.epoch), valid=valid)
                    stop.wait(.01)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(type(exc).__name__ + ": " + str(exc))
                stop.set()

        thread = threading.Thread(target=collect, name="wcfi-role-capture", daemon=True)
        thread.start()
        started, next_state, completed = time.monotonic(), 0.0, 0
        try:
            while time.monotonic() - started < seconds and not stop.is_set():
                if client is not None and time.monotonic() >= next_state:
                    try:
                        snapshot = client.call("inspect_session", timeout=2)
                        by_name = {}
                        for track in snapshot["tracks"]:
                            by_name.setdefault(track["name"].upper(), []).append(track)
                        for name, stream in streams.items():
                            matches = by_name.get(name, [])
                            expected_pid = getattr(client, "descriptor", {}).get("pid")
                            if expected_pid is not None and selected[(name, 0)]["pid"] != expected_pid:
                                matches = []
                            state = {"session_id": snapshot["session_id"], "track": matches[0]} if len(matches) == 1 else None
                            stream.set_chain_state(state)
                    except (OSError, ValueError, RuntimeError):
                        for stream in streams.values():
                            stream.set_chain_state(None)
                    next_state = time.monotonic() + 2
                for name, stream in streams.items():
                    with lock:
                        window = stream.window()
                    if window is None:
                        continue
                    report = stream.analyze(window, estimate_alignment=True)
                    report["state_observation"] = "polled_not_sample_atomic"
                    report["alignment_note"] = "Waveform alignment may be unavailable. An estimate is observed correspondence, not declared plug-in or hardware latency."
                    report["analysis_window_copy_to_completion_seconds"] = time.monotonic() - window["copied_at_monotonic"]
                    completed += 1
                    write_json(output / (f"{completed:04d}-" + name + ".json"), report)
                stop.wait(.05)
        finally:
            stop.set()
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError("Capture worker did not stop; no DAW actions were attempted")
        summary = {"windows": completed, "tracks": list(streams), "errors": errors,
                   "elapsed_seconds": time.monotonic() - started, "daw_writes": 0,
                   "audio_uploads": 0, "audio_saved": False,
                   "histories": {name: {"resets": s.resets, "last_reset_reason": s.last_reset_reason} for name, s in streams.items()}}
        write_json(output / "watch-result.json", summary)
        return summary
    finally:
        stop.set()
        for handle in handles.values():
            bridge.close(handle)
