"""Finite read-only node capture for app analysis; never starts an audio source."""
from __future__ import annotations

import time

from ..ableton.client import choose_track
from .analysis_nodes import NodeBridge
from .role_analysis import revision
from .role_stream import RoleAnalysisStream


def track_state(snapshot, name):
    return {"session_id": snapshot["session_id"], "track": choose_track(snapshot, name)}


def capture_pairs(client, seconds, *, bridge_factory=NodeBridge, timeout=45,
                  labels=("RED", "KIT"), guard_session=False, ready_event=None):
    if (not isinstance(labels, tuple) or not 1 <= len(labels) <= 12 or len(set(labels)) != len(labels)
            or any(not isinstance(name, str) or not 1 <= len(name) <= 100 or name != name.upper() for name in labels)):
        raise ValueError("Choose 1–12 distinct uppercase track labels with before/after nodes")
    snapshot = client.call("inspect_session")
    states = {name: track_state(snapshot, name) for name in labels}
    bridge = bridge_factory()
    selected, handles, streams = {}, [], {}
    began = time.monotonic()
    retryable_reads = 0
    try:
        for identity in bridge.discover():
            handle = bridge.open(identity)
            if not handle:
                continue
            handles.append(handle)
            _, status = bridge.read(handle, 0)
            header_deadline = time.monotonic() + .25
            while status is None and time.monotonic() < header_deadline:
                # The native seqlock returns -2 when its writer wins a race.
                # No frames were read; retry without advancing any cursor.
                retryable_reads += 1
                time.sleep(.01)
                _, status = bridge.read(handle, 0)
            if status is None:
                continue
            name = status.label.decode("utf-8", errors="replace").strip().upper()
            if name not in states or status.position not in (0, 1):
                continue
            key = (name, status.position)
            if key in selected:
                raise ValueError("Duplicate track node labels; use unique before/after pairs")
            if status.pid != client.descriptor["pid"]:
                raise ValueError("Analysis Nodes and Ableton bridge belong to different hosts")
            selected[key] = {"handle": handle, "cursor": int(status.written_frame),
                "rate": round(status.sample_rate), "channels": int(status.channels),
                "identity": identity, "pid": int(status.pid), "shared_clock": bool(status.flags & 1),
                "callback_ns": int(status.callback_ns)}
        for name in states:
            if (name, 0) not in selected or (name, 1) not in selected:
                raise ValueError(f"Add uniquely labelled {name} before/after nodes first")
            a, b = selected[name, 0], selected[name, 1]
            if (a["rate"], a["channels"]) != (b["rate"], b["channels"]):
                raise ValueError("Before/after nodes need matching sample rates and channels")
            # Used only as bounded capture storage; profiles do not classify these sources.
            streams[name] = RoleAnalysisStream("red" if name == "RED" else "kit",
                a["rate"], a["channels"], seconds=seconds)
        if len({item["rate"] for item in selected.values()}) != 1:
            raise ValueError("Captured tracks need a shared sample rate")
        frames = next(iter(streams.values())).frames
        shared_clock = all(item["shared_clock"] for item in selected.values())
        # Let an external rehearsal coordinator wait until all readers exist.
        # This reader still never starts a source or controls DAW transport.
        if ready_event is not None:
            ready_event.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fresh = True
            for (name, position), item in selected.items():
                block, status = bridge.read(item["handle"], item["cursor"])
                if status is None:
                    # A busy reader isn't a dropped frame or a missing node.
                    # A real loss/format/clock change is rejected on the next
                    # successful status; persistent failures reach the timeout.
                    retryable_reads += 1
                    fresh = False
                    continue
                age = (bridge.clock_ns() - status.callback_ns) / 1e6 if status.callback_ns else -1
                valid = ((not shared_clock or bool(status.flags & 1)) and bool(status.active) and 0 <= age < 500
                    and int(status.first_frame) == item["cursor"]
                    and round(status.sample_rate) == item["rate"]
                    and status.channels == item["channels"] and status.pid == item["pid"]
                    and status.label.decode("utf-8", errors="replace").strip().upper() == name
                    and status.position == position)
                fresh = fresh and valid
                item["cursor"] = int(status.first_frame) + len(block)
                item["callback_ns"] = int(status.callback_ns)
                first = (int(status.host_sample) + int(status.block_size) - int(status.written_frame)
                         + int(status.first_frame)) if shared_clock else int(status.first_frame)
                streams[name].push("pre_fx" if position == 0 else "post_fx",
                    block[:, :item["channels"]], max(0, first), epoch=int(status.epoch), valid=valid)
            rings = [ring for stream in streams.values() for ring in stream.history.values()]
            if fresh and all(r.end is not None for r in rings):
                end = min(r.end for r in rings)
                start = end - frames
                ready = start >= max(r.start for r in rings) if shared_clock else all(
                    r.end - r.start >= frames for r in rings)
                if ready:
                    final = client.call("inspect_session")
                    if any(revision(states[n]) != revision(track_state(final, n)) for n in states):
                        raise ValueError("A captured chain or mixer changed during capture; analyse again")
                    if guard_session and revision(snapshot["tracks"]) != revision(final["tracks"]):
                        raise ValueError("Session processing or routing changed during the mix capture")
                    def read_stage(stream, stage, common_end=end):
                        ring = stream.history[stage]
                        last = common_end if shared_clock else ring.end
                        return ring.read(last - stream.frames, last)

                    return {name: {"before": read_stage(s, "pre_fx"),
                        "after": read_stage(s, "post_fx"), "rate": s.rate,
                        "shared_time_available": shared_clock,
                        "session_snapshot": snapshot,
                        "state": states[name], "provenance": {"post_fx_kind": "current_node_capture",
                            "host_pid": client.descriptor["pid"], "start_host_frame": start if shared_clock else None,
                            "end_host_frame": end if shared_clock else None,
                            "clock_mode": "shared_host_sample_clock" if shared_clock else "independent_node_counters_unverified_pair_timing",
                            "independent_counter_windows": {stage: {"start": r.end - s.frames, "end": r.end,
                                "callback_ns": selected[name, pos]["callback_ns"]}
                                for pos, (stage, r) in enumerate(s.history.items())} if not shared_clock else None,
                            "node_ids": [selected[name, p]["identity"] for p in (0, 1)],
                            "retryable_reads": retryable_reads, "history_resets": s.resets,
                            "capture_elapsed_seconds": time.monotonic() - began,
                            "chain_observation": "before_and_after_capture_not_sample_atomic"}}
                        for name, s in streams.items()}
            time.sleep(.01)
        raise RuntimeError("No continuous node window arrived. Enable Live's audio engine, supply inputs and check Monitor In and node labels. This action never starts playback.")
    finally:
        for handle in handles:
            bridge.close(handle)
