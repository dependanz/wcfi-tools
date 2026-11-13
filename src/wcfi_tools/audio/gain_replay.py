"""Fixed October scalar replay, independent scoring and local audit artifacts.

No audio is opened. The policy sees one scalar frame at a time; control labels,
whole-file opportunity counts and expected answers belong only to this harness.
"""
from __future__ import annotations

import contextlib
import ctypes as ct
import gc
import hashlib
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, deque
from itertools import pairwise
from pathlib import Path

from .gain_controller import POLICY, GainController
from .gain_observations import CONTEXT_ROLES, ROLES, SOURCE_SUFFIX, Adapter, load_october

PLAN_SHA256 = "5fe8370dd3e561e91d8b65b3fc781ce8f53418b34d7ed45a981a86ac8f1f9984"
PLAN = Path("_work/automix-shadow-plan-2026-10-24/plan.md")
REVIEW = Path("_work/automix-supervision-2026-10-24/plan-review.md")
DURATION = 240
FAULTS = ("missing", "stale", "epoch", "format", "silence", "nonfinite",
          "attribution", "alignment")
TAKEOVERS = ("manual_fader", "routing", "chain", "session", "inflight")
OWN_FILES = (
    "src/wcfi_tools/audio/gain_controller.py",
    "src/wcfi_tools/audio/gain_observations.py",
    "src/wcfi_tools/audio/gain_replay.py",
    "src/wcfi_tools/commands/gain_shadow.py",
    "src/wcfi_tools/commands/audio.py",
    "tests/test_gain_controller.py", "tests/test_gain_observations.py",
    "tests/test_gain_replay.py", "docs/offline-gain-controller.md",
)
LIMITS = {"suite_seconds": 300, "peak_mib": 512, "growth_mib": 16,
          "transition_p95_ms": 50, "transition_max_ms_exclusive": 200}


class TransitionTimings:
    """Bounded histogram covering EVERY timed replay, including prefix proofs."""

    def __init__(self):
        self.count = 0
        self.maximum = 0.
        self.bins = [0] * 5001  # .01ms bins below50ms, one overflow bin.

    def append(self, elapsed_ms):
        self.count += 1
        self.maximum = max(self.maximum, elapsed_ms)
        self.bins[min(5000, int(elapsed_ms * 100))] += 1

    def describe(self):
        target = math.ceil(.95 * self.count)
        cumulative = 0
        upper = 0.
        for index, count in enumerate(self.bins):
            cumulative += count
            if cumulative >= target:
                upper = (index + 1) / 100 if index < 5000 else self.maximum
                break
        return {"transition_count": self.count, "p95_upper_bound_ms": upper,
                "max_ms": self.maximum, "histogram_resolution_ms": .01,
                "scope": "all primary, repeated, prefix and future-suffix adapter+controller transitions"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for part in iter(lambda: handle.read(65536), b""):
            result.update(part)
    return result.hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")


def verify_freeze(root, output, frozen, freeze_hash):
    """Run on success AND failure; never silently omit provenance after timeout."""
    changed = [name for name, expected in frozen["files"].items()
               if file_digest(root / name) != expected]
    fixture_changes = [name for name, expected in frozen["fixture_hashes"].items()
                       if file_digest(output / "fixtures" / name) != expected]
    changed.extend(name for name, field in (("private-scorer-key.json", "key_sha256"),
                    ("opportunities.json", "opportunities_sha256"))
                   if file_digest(output / name) != frozen[field])
    if file_digest(output / "freeze.json") != freeze_hash:
        changed.append("freeze.json")
    return gate(not changed and not fixture_changes, changed_files=changed,
                changed_fixtures=fixture_changes, expected_freeze_sha256=freeze_hash)


def cases():
    """Frozen fixture specification and scorer key; never passed to replay()."""
    result = []
    for gain in (2, 3, 6):
        for onset in (20, 30, 40):
            result.append({"kind": "positive", "gain": gain, "onset": onset,
                           "removal": onset + 90})
    result.extend({"kind": "null", "variant": name} for name in
                  ("unchanged", "identical", "jitter", "ac_negative"))
    for target in ("all", "RED", *CONTEXT_ROLES):
        for gain in (-3, 3):
            result.append({"kind": "null", "variant": "isolated", "target": target,
                           "gain": gain})
    for episode in (False, True):
        for at in (60, 120):
            result.extend({"kind": "fault", "fault": name, "at": at,
                           "episode": episode} for name in FAULTS)
            result.extend({"kind": "takeover", "event": name, "at": at,
                           "rearm": at + 30, "episode": episode} for name in TAKEOVERS)
    return {f"case-{index:03d}": spec for index, spec in enumerate(result, 1)}


def make_frames(anchor, spec):
    """Make public scalar observations. Perturbations are not policy inputs."""
    frames = []
    for end in range(1, DURATION + 1):
        start = end - 1
        levels = {role: anchor[role][start] for role in ROLES}
        if spec["kind"] == "positive" and spec["onset"] <= start < spec["removal"]:
            levels["AC GUITAR"] += spec["gain"]
        if spec.get("episode") and 20 <= start < 110:
            levels["AC GUITAR"] += 6
        if spec["kind"] == "null":
            variant = spec["variant"]
            if variant == "jitter":
                levels["AC GUITAR"] += .5 if end % 2 else -.5
            elif variant == "ac_negative":
                levels["AC GUITAR"] -= 3
            elif variant == "isolated":
                targets = ROLES if spec["target"] == "all" else (spec["target"],)
                for role in targets:
                    levels[role] += spec["gain"]
        frame = {"time_seconds": end, "levels": levels, "revision": "base",
                 "anchor_revision": "base", "epoch": 0, "format": "scalar-dbfs-v1"}
        if spec["kind"] == "fault":
            at, fault = spec["at"], spec["fault"]
            if fault == "epoch" and end >= at:
                frame["epoch"] = 1
            if fault == "format" and end >= at:
                frame["format"] = "scalar-dbfs-v2"
            if fault == "stale" and at <= end < at + 3:
                frame["observation_age_seconds"] = 3
            if end == at:
                if fault == "missing":
                    frame.update(valid=False, reason="declared_missing_observation")
                elif fault == "silence":
                    levels["AC GUITAR"] = -200
                elif fault == "nonfinite":
                    levels["AC GUITAR"] = None  # Strict JSON representation of unavailable value.
                elif fault == "attribution":
                    frame["attribution_ambiguous"] = True
                elif fault == "alignment":
                    frame["alignment_unknown"] = True
        if spec["kind"] == "takeover":
            at, rearm = spec["at"], spec["rearm"]
            if at <= end <= rearm:
                # In-flight case tests a late result BEFORE the new revision is observed.
                if spec["event"] == "inflight" and end == at:
                    frame["completed_revision"] = "retired-result"
                else:
                    frame["revision"] = "changed-" + spec["event"]
            if end == at:
                frame["declared_takeover"] = spec["event"]
                if spec["event"] == "manual_fader":
                    frame["manual_fader_increment_db"] = 1
            if end == rearm:
                frame["rearm"] = {"revision": "rearmed-" + spec["event"],
                                  "anchor_revision": "accepted-new-anchor"}
            if end > rearm:
                frame.update(revision="rearmed-" + spec["event"],
                             anchor_revision="accepted-new-anchor")
        frames.append(frame)
    return frames


def scheduled_frames(frames, *, until=None):
    """Logical one-second scheduler; absent frames cannot erase decision slots."""
    previous = 0
    last_revision, last_anchor = "base", "base"
    for frame in frames:
        now = frame["time_seconds"]
        if not isinstance(now, int) or now <= previous:
            raise ValueError("Scalar frame endpoints must strictly increase")
        if until is not None and now > until:
            break
        for missing in range(previous + 1, now):
            yield {"time_seconds": missing, "valid": False,
                   "reason": "scheduled_missing_observation", "revision": last_revision,
                   "anchor_revision": last_anchor}
        yield frame
        previous = now
        last_revision = frame.get("revision", "base")
        last_anchor = frame.get("anchor_revision", "base")
        if until is not None and now == until:
            return
    if until is not None:
        for missing in range(previous + 1, until + 1):
            yield {"time_seconds": missing, "valid": False,
                   "reason": "scheduled_missing_observation", "revision": last_revision,
                   "anchor_revision": last_anchor}


def replay(anchor, frames, *, durations=None, telemetry=None, deadline=None, until=None):
    """Streaming boundary: no spec, key, global support total or future statistic."""
    adapter, controller = Adapter(anchor), GainController()
    recent = deque(maxlen=8)
    logs = []
    for frame in scheduled_frames(frames, until=until):
        now = frame["time_seconds"]
        if deadline is not None and time.perf_counter() > deadline:
            raise TimeoutError("Frozen five-minute suite deadline exceeded; no automatic retry")
        recent.append(frame)
        began = time.perf_counter()
        observation = adapter.push(frame)
        if observation is not None:
            decision = controller.step(observation)
            elapsed = (time.perf_counter() - began) * 1000
            if durations is not None:
                durations.append(elapsed)
            if telemetry is not None:
                telemetry.append(elapsed)
            logs.append({"observation": observation, "decision": decision})
        if "rearm" in frame:
            # Explicit fixture operator accepts an anchor from observations available NOW.
            # Never read injected gain or expected answer to form this calibration.
            if len(recent) != 8 or any(row.get("valid", True) is not True or
                                       not isinstance(row.get("levels"), dict) for row in recent):
                raise ValueError("Rearm requires eight observed scalar frames")
            offsets = {role: statistics.median(
                row["levels"][role] - anchor[role][row["time_seconds"] - 1]
                for row in recent) for role in ROLES}
            event = frame["rearm"]
            decision = controller.rearm(now, event["revision"], event["anchor_revision"])
            adapter.reset(now, event["revision"], event["anchor_revision"], offsets)
            logs.append({"observation": None, "decision": decision,
                         "accepted_observed_anchor_offsets_db": offsets})
            recent.clear()
    return logs


def opportunities(anchor):
    adapter = Adapter(anchor)
    return {row["time_seconds"]: row["opportunity"] for end in range(1, DURATION + 1)
            if (row := adapter.push({"time_seconds": end,
                                     "levels": {role: anchor[role][end - 1] for role in ROLES}}))
            is not None and end >= 8}


def gate(passed, **evidence):
    return {"passed": bool(passed), **evidence}


def score(spec, logs, support):
    """Independent fixture scorer: known offsets are used ONLY here, after replay."""
    rows = [row for row in logs if row["observation"] is not None]
    decisions = [row["decision"] for row in rows]
    moves = [row for row in decisions if row["action"] == "propose"]
    bounds = all(-3 <= row["offset_db"] <= 0 and
                 math.isclose(row["effective_gain"], 10 ** (row["offset_db"] / 20),
                              abs_tol=1e-12) for row in decisions)
    bounds = bounds and all(abs(row["delta_db"]) == .5 for row in moves)
    bounds = bounds and all(row["offset_db"] == row["total_before_db"] + row["delta_db"]
                            and (row["action"] == "propose" or row["delta_db"] == 0)
                            for row in decisions)
    # Rearm may reset offsets, but does not send a fader move; cadence across a rearm
    # is conservatively checked too (30-second suspension guarantees separation).
    cadence = all(b["time_seconds"] - a["time_seconds"] >= 10
                  for a, b in pairwise(moves))
    intervals = {"dense_240_330": (8, 90), "sparser_330_390": (90, 150),
                 "dense_390_480": (150, 241)}
    counts = {}
    for label, (start, stop) in intervals.items():
        chosen = [row for row in rows if start <= row["decision"]["time_seconds"] < stop]
        counts[label] = {"scheduled": len(chosen), "anchor_supported": sum(
            support.get(row["decision"]["time_seconds"], False) for row in chosen),
            "runtime_veto": sum(support.get(row["decision"]["time_seconds"], False)
                                and not row["observation"]["eligible"] for row in chosen)}
    result = {"bounds_cadence": gate(bounds and cadence, proposals=len(moves)),
              "support_veto_counts": counts,
              "reason_counts": dict(Counter(row["reason"] for row in decisions))}
    result["fixed_opportunity_denominator"] = gate(
        [row["time_seconds"] for row in decisions] == list(range(2, 241, 2)) and all(
            row["observation"]["opportunity"] == support.get(row["decision"]["time_seconds"], False)
            for row in rows), scheduled_decisions=len(decisions))
    if spec["kind"] == "positive":
        onset, removal, gain = spec["onset"], spec["removal"], spec["gain"]
        plateau = [t for t, ok in support.items() if onset + 8 <= t <= removal and ok]
        deadline = 70 if gain == 6 else 60
        required = sorted(set(range(onset + 8, onset + deadline + 1, 2)) |
                          set(range(removal + 8, removal + 71, 2)))
        missing = [t for t in required if not support.get(t, False)]
        covered = sum(support.values()) >= 30 and sum(support.values()) / len(support) >= .5
        covered = covered and len(plateau) >= 30 and not missing
        active_moves = [row for row in moves if onset <= row["time_seconds"] <= removal]
        # Reaching a target briefly is insufficient: hold it from the deadline
        # through every remaining supported plateau decision.
        corrected = [row for row in decisions
                     if onset + deadline <= row["time_seconds"] <= removal
                     and support.get(row["time_seconds"], False)]
        if gain == 6:
            reached = bool(corrected) and all(row["offset_db"] == -3 for row in corrected)
            unresolved = any(row["reason"] == "cap_unresolved" for row in decisions
                             if onset <= row["time_seconds"] <= removal)
        else:
            reached = bool(corrected) and all(abs(gain + row["offset_db"]) <= .75
                                               for row in corrected)
            unresolved = True
        after_deadline = [row for row in decisions if row["time_seconds"] >= removal + 70
                          and support.get(row["time_seconds"], False)]
        restored = bool(after_deadline) and all(row["offset_db"] == 0 for row in after_deadline)
        result.update(
            coverage=gate(covered, plateau_supported=len(plateau),
                          missing_deadline_windows=missing, denominator_retained=True),
            direction=gate(bool(active_moves) and active_moves[0]["delta_db"] < 0
                           and all(row["delta_db"] < 0 for row in active_moves)),
            correction=gate(reached and unresolved, deadline_seconds=deadline,
                            classification="scored" if covered else "insufficient_deadline_support"),
            removal=gate(restored, deadline_seconds=70))
    elif spec["kind"] == "null":
        result["null"] = gate(all(row["offset_db"] == 0 for row in decisions))
    elif spec["kind"] == "fault":
        at = spec["at"]
        last_invalid = at + (2 if spec["fault"] == "stale" else 0)
        invalid = [row for row in rows if at <= row["decision"]["time_seconds"] <= last_invalid]
        subsequent = [row for row in rows if row["decision"]["time_seconds"] > last_invalid]
        eligible = [row for row in subsequent if row["observation"]["eligible"]]
        earliest = eligible[0]["decision"]["time_seconds"] if eligible else None
        proposed = [row for row in subsequent if row["decision"]["action"] == "propose"]
        fresh = all(row["decision"]["time_seconds"] >= last_invalid + 8 for row in eligible)
        fresh = fresh and all(row["decision"]["action"] != "propose"
                              for row in subsequent
                              if row["decision"]["time_seconds"] < last_invalid + 8)
        new_votes = True
        for proposal in proposed:
            preceding = [r for r in subsequent if r["decision"]["time_seconds"] <=
                         proposal["decision"]["time_seconds"]][-3:]
            new_votes = new_votes and len(preceding) == 3 and all(
                r["observation"]["eligible"] for r in preceding) and all(
                b["decision"]["time_seconds"] - a["decision"]["time_seconds"] == 2
                for a, b in pairwise(preceding))
        reset = bool(invalid) and all(row["decision"]["raw_history_count"] == 0
                                     and row["decision"]["action"] != "propose" for row in invalid)
        reset = reset and (not eligible or eligible[0]["decision"]["raw_history_count"] == 1)
        result["fault"] = gate(reset and fresh and new_votes,
                               first_eligible=earliest, last_invalid=last_invalid)
        # Only supported unchanged fixtures have the strict sixteen-second availability gate.
        available = spec["episode"] or earliest is not None and earliest <= last_invalid + 16
        result["recovery"] = gate(available, first_eligible=earliest,
                                  supported_unchanged_deadline=last_invalid + 16)
        before = [row for row in decisions if row["time_seconds"] < at]
        result["pre_fault_state"] = before[-1] if before else None
    else:
        at, rearm = spec["at"], spec["rearm"]
        suspended = [row for row in decisions if at <= row["time_seconds"] <= rearm]
        events = [row for row in logs if row["decision"]["action"] == "rearmed"]
        valid_rearm = len(events) == 1 and events[0]["decision"]["offset_db"] == 0
        valid_rearm = valid_rearm and events[0]["decision"]["time_seconds"] == rearm
        after = [row for row in rows if row["decision"]["time_seconds"] > rearm]
        safe_after = all(row["decision"]["action"] != "propose" or (
            row["decision"]["time_seconds"] >= rearm + 12 and
            row["decision"]["revision"].startswith("rearmed-") and
            row["decision"]["anchor_revision"] == "accepted-new-anchor") for row in after)
        result["manual_takeover"] = gate(bool(suspended) and all(
            row["action"] == "suspended" for row in suspended) and valid_rearm and safe_after,
            suspension_seconds=30, explicit_rearms=len(events))
    result["passed"] = all(value["passed"] for value in result.values()
                           if isinstance(value, dict) and "passed" in value)
    return result


def check_causality(anchor, frames, full, *, deadline=None, telemetry=None):
    checked = 0
    for end in range(2, len(frames) + 1, 2):
        expected = [row for row in full if row["decision"]["time_seconds"] <= end]
        prefix = frames[:end]
        shortened = {role: values[:end] for role, values in anchor.items()}
        if replay(shortened, prefix, deadline=deadline, telemetry=telemetry) != expected:
            return gate(False, checked=checked, failed_prefix=end, variant="prefix_alone")
        # Constructor receives the corrupted entire future; frame iteration remains causal.
        adversarial_anchor = {role: tuple(values[:end]) + tuple(
            None if index % 3 == 0 else 10000. if index % 2 else -10000.
            for index in range(end, len(values))) for role, values in anchor.items()}
        adversarial_frames = list(prefix) + [{"time_seconds": 100000 + index * 19,
            "levels": {role: -10000. for role in ROLES}, "valid": False,
            "private_label": "shuffled-unavailable-to-policy"}
            for index in range(end, len(frames))]
        # Stop at the same explicit event clock. No future coverage or labels are inputs.
        earlier = replay(adversarial_anchor, iter(adversarial_frames), deadline=deadline,
                         telemetry=telemetry, until=end)
        if earlier != expected:
            return gate(False, checked=checked, failed_prefix=end, variant="future_suffix")
        checked += 1
    return gate(True, checked=checked, variants=["prefix_alone", "extreme_future_gap_labels"])


def memory():
    if os.name != "nt":
        raise RuntimeError("This frozen worker resource audit currently requires Windows")

    class Counters(ct.Structure):
        _fields_ = [("cb", ct.c_ulong), ("faults", ct.c_ulong)] + [
            (name, ct.c_size_t) for name in ("peak", "working", "peak_paged", "paged",
                                           "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    kernel, psapi = ct.WinDLL("kernel32"), ct.WinDLL("psapi")
    kernel.GetCurrentProcess.restype = ct.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (ct.c_void_p, ct.POINTER(Counters), ct.c_ulong)
    handle = kernel.GetCurrentProcess()
    counters = Counters()
    counters.cb = ct.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(handle, ct.byref(counters), counters.cb):
        raise RuntimeError("Memory audit unavailable")
    mask, system = ct.c_size_t(), ct.c_size_t()
    kernel.GetProcessAffinityMask.argtypes = (ct.c_void_p, ct.POINTER(ct.c_size_t),
                                              ct.POINTER(ct.c_size_t))
    kernel.GetPriorityClass.argtypes = (ct.c_void_p,)
    if not kernel.GetProcessAffinityMask(handle, ct.byref(mask), ct.byref(system)):
        raise RuntimeError("CPU affinity audit unavailable")
    return {"working_mib": counters.working / 2**20, "peak_mib": counters.peak / 2**20,
            "cpu_count": mask.value.bit_count(), "idle_priority": kernel.GetPriorityClass(handle) == 0x40}


@contextlib.contextmanager
def isolation():
    """Deny external capabilities during the replay; count attempted violations."""
    state = {"active": True, "denied": [], "audio_open_attempts": 0,
             "socket_attempts": 0, "process_start_attempts": 0, "forbidden_import_attempts": 0,
             "evidence_scope": "Python audit events; no host/desktop imports or call sites in policy/replay. Counts are attempted operations, not OS-wide monitoring."}

    def audit(event, args):
        if not state["active"]:
            return
        violation = None
        if event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
            state["socket_attempts" if event.startswith("socket.") else "process_start_attempts"] += 1
            violation = event
        elif event == "open" and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0]).lower()
            if Path(name).suffix in (".wav", ".mp3", ".flac", ".aif", ".aiff", ".als"):
                state["audio_open_attempts"] += 1
                violation = "audio_or_set_open"
            if "2026-01-24" in name:
                violation = "reserved_recording_access"
        elif event == "import" and any(str(args[0]).startswith(prefix) for prefix in
                                      ("soundfile", "sounddevice", "torch", "numpy",
                                       "wcfi_tools.ableton", "wcfi_tools.audio.asio",
                                       "wcfi_tools.audio.analysis_nodes")):
            state["forbidden_import_attempts"] += 1
            violation = "forbidden_import:" + str(args[0])
        if violation:
            state["denied"].append(violation)
            raise RuntimeError("Offline scalar worker denied " + violation)
    sys.addaudithook(audit)
    try:
        yield state
    finally:
        state["active"] = False


def run(output, *, repo=None, progress=None):
    """Execute frozen suite once, preserving all failed artifacts; no automatic retry."""
    root = Path(repo) if repo is not None else Path(__file__).resolve().parents[3]
    output = Path(output).resolve()
    if file_digest(root / PLAN) != PLAN_SHA256:
        raise ValueError("Reviewed plan changed; refuse to silently change the protocol")
    output.mkdir(parents=True, exist_ok=False)
    began = time.perf_counter()
    deadline = began + LIMITS["suite_seconds"]
    results = {"kind": "wcfi_offline_gain_prerequisite_v1", "status": "running",
               "live_eligible": False, "families": {}}
    durations = []
    telemetry = TransitionTimings()
    try:
        with isolation() as isolated:
            recorded = load_october(root.joinpath(*SOURCE_SUFFIX))
            synthetic = {role: tuple([-24. - i] * DURATION) for i, role in enumerate(ROLES)}
            families = {"synthetic": synthetic, "october": recorded}
            private_key = cases()
            support = {name: opportunities(anchor) for name, anchor in families.items()}
            hashes = {str(path): file_digest(root / path) for path in (*OWN_FILES, PLAN, REVIEW,
                                                                      Path(*SOURCE_SUFFIX))}
            fixtures = output / "fixtures"
            fixtures.mkdir()
            fixture_hashes = {}
            for family, anchor in families.items():
                for identifier, spec in private_key.items():
                    frames = make_frames(anchor, spec)
                    target = fixtures / f"{family}-{identifier}.json"
                    save(target, frames)
                    fixture_hashes[target.name] = file_digest(target)
            save(output / "private-scorer-key.json", private_key)
            save(output / "opportunities.json", support)
            save(output / "freeze.json", {
                "plan_sha256": PLAN_SHA256, "files": hashes, "policy": POLICY, "limits": LIMITS,
                "source_indices": [240, 480], "clock": "one-second frame [t-1,t); end t; eval even t",
                "positive_episode": "gain begins at frame start onset, ends at removal",
                "fault_clock": "event at declared frame END; stale covers end at,at+1,at+2",
                "missing_representation": "explicit invalid one-second sentinel; actual gaps unit-tested",
                "context_support": "Each context source uses >=4 of its own immutable raw frames; AC/RED use identical shared frames.",
                "rearm": "Explicit operator fixture; new offsets median of last8 observed level-minus-anchor, never hidden gain truth.",
                "fixture_hashes": fixture_hashes, "key_sha256": file_digest(output / "private-scorer-key.json"),
                "opportunities_sha256": file_digest(output / "opportunities.json"),
                "canonical_logs": "All observation/evidence/decision values; wall timings separate.",
                "repetitions": 3, "causality_prefixes_per_case": 120,
                "timing_scope": "Each even-frame adapter.push plus controller.step; scalar I/O and all other work included in whole-suite wall budget.",
                "manual_fader_fixture": "Declared +1dB manual-control revision; pre-FX scalar signal stays unchanged."})
            freeze_hash = file_digest(output / "freeze.json")
            save(output / "freeze-hash.json", {"sha256": freeze_hash})
            frozen = json.loads((output / "freeze.json").read_text(encoding="utf-8"))
            # Warm allocations on the actual worst-shaped public data, before memory-growth baseline.
            replay(recorded, make_frames(recorded, private_key["case-009"]), telemetry=telemetry)
            gc.collect()
            initial = memory()
            maximum_working = initial["working_mib"]
            for family, anchor in families.items():
                family_results = {"cases": {}, "coverage": gate(
                    sum(support[family].values()) >= 30 and
                    sum(support[family].values()) / len(support[family]) >= .5,
                    supported=sum(support[family].values()), scheduled=len(support[family]))}
                results["families"][family] = family_results
                clean = replay(anchor, make_frames(anchor, {
                    "kind": "positive", "gain": 6, "onset": 20, "removal": 110}),
                    telemetry=telemetry, deadline=deadline)
                phase_rows = {row["decision"]["time_seconds"]: row["decision"] for row in clean
                              if row["decision"]["time_seconds"] in (58, 60, 118, 120)}
                family_results["fault_phase_coverage"] = gate(
                    phase_rows[58]["direction_count"] >= 1 and phase_rows[60]["delta_db"] == -.5
                    and phase_rows[118]["corrected_error_db"] < -.75
                    and phase_rows[120]["delta_db"] == .5,
                    unchanged_episode_counterfactual=phase_rows,
                    meaning="Fault60 interrupts pending attenuation; fault120 interrupts active release.")
                for identifier, spec in private_key.items():
                    path = fixtures / f"{family}-{identifier}.json"
                    if file_digest(path) != fixture_hashes[path.name]:
                        raise ValueError("Frozen public fixture changed")
                    frames = json.loads(path.read_text(encoding="utf-8"))
                    start_timing = len(durations)
                    logs = replay(anchor, frames, durations=durations, telemetry=telemetry,
                                  deadline=deadline, until=240)
                    repeat = [digest(logs)]
                    for _ in range(2):
                        repeat.append(digest(replay(anchor, frames, telemetry=telemetry,
                                                    deadline=deadline, until=240)))
                    checked = score(spec, logs, support[family])
                    checked["repeatability"] = gate(len(set(repeat)) == 1, hashes=repeat)
                    with (output / f"{family}-{identifier}.jsonl").open("w", encoding="utf-8") as handle:
                        for row in logs:
                            handle.write(canonical(row) + "\n")
                    save(output / f"{family}-{identifier}-timings.json", durations[start_timing:])
                    checked["causality"] = gate(False, status="not_completed")
                    checked["primary_gates_passed"] = checked["passed"]
                    checked["passed"] = False
                    family_results["cases"][identifier] = checked
                    checked["causality"] = check_causality(anchor, frames, logs,
                                                            deadline=deadline, telemetry=telemetry)
                    checked["passed"] = checked["primary_gates_passed"] and checked["repeatability"]["passed"] and checked["causality"]["passed"]
                    save(output / f"{family}-{identifier}-score.json", checked)
                    current = memory()
                    maximum_working = max(maximum_working, current["working_mib"])
                    if current["peak_mib"] > LIMITS["peak_mib"]:
                        raise MemoryError("512 MiB worker budget exceeded")
                    if progress is not None:
                        progress(family, identifier, checked["passed"])
                family_results["passed"] = family_results["coverage"]["passed"] and family_results["fault_phase_coverage"]["passed"] and all(
                    item["passed"] for item in family_results["cases"].values())
            results["freeze_integrity"] = verify_freeze(root, output, frozen, freeze_hash)
            final = memory()
            elapsed = time.perf_counter() - began
            timed = telemetry.describe()
            resources = {"initial": initial, "final": final, "elapsed_seconds": elapsed,
                         "sampled_working_set_growth_mib": maximum_working - initial["working_mib"],
                         "post_warmup_growth_mib": max(0., final["peak_mib"] - initial["working_mib"]),
                         "growth_measure": "Conservative lifetime peak minus post-warmup working set (includes transient peaks).",
                         **timed}
            resources["passed"] = (elapsed <= 300 and final["peak_mib"] <= 512 and
                resources["post_warmup_growth_mib"] <= 16 and timed["p95_upper_bound_ms"] <= 50 and timed["max_ms"] < 200
                and final["cpu_count"] == 1 and final["idle_priority"])
            results["resources"] = resources
            results["isolation"] = gate(not isolated["denied"], **{
                k: v for k, v in isolated.items() if k != "active"})
            results["status"] = "passed" if results["freeze_integrity"]["passed"] and resources["passed"] and results["isolation"]["passed"] and all(
                family["passed"] for family in results["families"].values()) else "failed"
    except Exception as exc:  # noqa: BLE001 - preserve every failed-run artifact, never retry.
        results.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)},
                       elapsed_seconds=time.perf_counter() - began)
        if "frozen" in locals():
            try:
                results["freeze_integrity"] = verify_freeze(root, output, frozen, freeze_hash)
            except (OSError, ValueError, KeyError) as audit_error:
                results["freeze_integrity"] = gate(False, error=str(audit_error))
        if "initial" in locals():
            final = memory()
            results["resources"] = {"passed": False, "initial": initial, "final": final,
                "elapsed_seconds": time.perf_counter() - began,
                **telemetry.describe(),
                "sampled_working_set_growth_mib": maximum_working - initial["working_mib"],
                "post_warmup_growth_mib": max(0., final["peak_mib"] - initial["working_mib"]),
                "growth_measure": "Conservative lifetime peak minus post-warmup working set (includes transient peaks).",
                "incomplete": True}
        if "isolated" in locals():
            results["isolation"] = gate(not isolated["denied"], **{
                k: v for k, v in isolated.items() if k != "active"})
    results["limitations"] = [
        "Same-time unchanged October anchor is an oracle, not an accepted live musical target.",
        "Energy masks and declared ambiguity fixtures do not detect singers, instruments or bleed.",
        "Virtual arithmetic only; no actual gain, post-FX or final-output evidence.",
        "Synthetic and October development fixtures are not independent performances or held-out evaluation.",
        "No professional-sound or listener-preference claim; January remains untouched.",
    ]
    save(output / "results.json", results)
    return {"status": results["status"], "output": str(output),
            "families": {name: family.get("passed", False) for name, family in results["families"].items()},
            "resources": results.get("resources"), "error": results.get("error"), "live_eligible": False}
