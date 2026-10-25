"""Causal scalar observations for the October-only gain-controller harness.

The same-time anchor is an engineering oracle, not an accepted musical balance.
Energy masks do not identify performers or distinguish them from bleed. This
module uses only saved scalar JSON and has no audio, host, or network dependency.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

ROLES = ("RED", "AC GUITAR", "KIT", "BASS", "KEYS", "EGUITAR")
_ROLE_SET = frozenset(ROLES)
CONTEXT_ROLES = ROLES[2:]
SOURCE_START = 240
SOURCE_STOP = 480
SOURCE_SUFFIX = (
    "_work", "oct11-replacement-2026-10-22", "2026-10-11-094938-features.json"
)
MAX_INPUT_BYTES = 8 * 1024 * 1024
WINDOW_SECONDS = 8


def _finite(value: Any) -> bool:
    # Saved scalar levels are ordinary floats. Keep the original numeric-subclass
    # semantics for other callers without paying three type checks per float.
    if type(value) is float:
        return math.isfinite(value)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linearly interpolated percentile; callers supply only their trailing window."""
    if not values or not 0 <= fraction <= 1 or not all(_finite(v) for v in values):
        raise ValueError("percentile requires finite values and a fraction in [0, 1]")
    return _validated_percentile(values, fraction)


def _validated_percentile(values: Sequence[float], fraction: float) -> float:
    """Arithmetic only; internal callers have already validated this exact window."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def load_october(path: str | Path) -> dict[str, tuple[float, ...]]:
    """Load only frozen October indices [240, 480); never read an audio file.

    The exact relative suffix is allowlisted, including after symlink resolution.
    The original JSON has no timestamp/sample-rate fields: its six named source
    objects and scalar level arrays are the saved scanner's available metadata.
    """
    source = Path(path)
    if tuple(source.parts[-3:]) != SOURCE_SUFFIX:
        raise ValueError("only the frozen October feature JSON is allowed")
    if tuple(source.resolve().parts[-3:]) != SOURCE_SUFFIX:
        raise ValueError("resolved source is not the frozen October feature JSON")
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("feature JSON exceeds the 8 MiB input bound")
    with source.open("rb") as handle:
        payload = handle.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("feature JSON exceeds the 8 MiB input bound")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid feature JSON") from exc
    if not isinstance(data, dict) or set(data) != _ROLE_SET:
        raise ValueError("feature JSON must contain exactly the six frozen source roles")
    result = {}
    lengths = set()
    for role in ROLES:
        source_data = data[role]
        if not isinstance(source_data, dict) or set(source_data) != {"levels", "bands"}:
            raise ValueError(f"invalid source metadata for {role}")
        levels = source_data["levels"]
        if not isinstance(source_data["bands"], dict) or not isinstance(levels, list):
            raise ValueError(f"invalid source metadata for {role}")  # noqa: TRY004
        if len(levels) < SOURCE_STOP:
            raise ValueError(f"source {role} is shorter than the frozen range")
        lengths.add(len(levels))
        selected = levels[SOURCE_START:SOURCE_STOP]
        if not all(_finite(value) for value in selected):
            raise ValueError(f"source {role} has invalid selected level values")
        result[role] = tuple(float(value) for value in selected)
    if len(lengths) != 1:
        raise ValueError("source metadata has unequal level-array lengths")
    return result


class Adapter:
    """Convert one-second scalar frames to causal two-second decision observations.

    Frame timestamps are integer endpoints, starting at 1. No future anchor value
    is inspected to form support, estimates, or validity. ``reset(t, ...)`` occurs
    after frame t and expects the next frame at t+1. Offsets supplied on reset
    affect reference comparisons only; raw source-support masks remain immutable.

    Ordinary insufficient raw support is an unavailable opportunity, not a corrupt
    frame. Declared faults, current silence, and inferred context/RED attribution
    disagreements invalidate accumulated history. After any such invalidation,
    eight fresh seconds are required here; the downstream controller additionally
    requires its three fresh eligible evaluations. No automatic rearm is performed.

    The first revision or completed-result mismatch is latched even on odd frames
    that emit no observation. Every later emission carries that durable mismatch
    until explicit reset accepts BOTH a new revision and a new anchor. A transient
    manual change therefore cannot disappear between two scheduled evaluations.
    """

    def __init__(
        self,
        anchor_levels: Mapping[str, Sequence[float]],
        source_offset_seconds: int = SOURCE_START,
    ) -> None:
        if set(anchor_levels) != _ROLE_SET:
            raise ValueError("anchor must contain exactly the six frozen source roles")
        # Copy scalar sequences, but do not derive statistics or validity from
        # future values. Prefix tests may deliberately corrupt an unseen suffix.
        self.anchor = {role: tuple(anchor_levels[role]) for role in ROLES}
        if len({len(values) for values in self.anchor.values()}) != 1:
            raise ValueError("anchor source lengths must agree")
        if not isinstance(source_offset_seconds, int) or isinstance(source_offset_seconds, bool):
            raise TypeError("source offset must be an integer")
        self.source_offset_seconds = source_offset_seconds
        self.history: deque[dict[str, Any]] = deque(maxlen=9)
        # Only validation of anchor frames already reached by the event clock is
        # retained. Thresholds, source support and estimates are never cached.
        self._anchor_validity: deque[tuple[int, tuple[str, ...]]] = deque(maxlen=8)
        self._anchor_invalid_counts = dict.fromkeys(ROLES, 0)
        self._last_time = 0
        self._contiguous = 0
        self._tags: tuple[Any, Any] | None = None
        self._revisions = ("base", "base")
        self._revision_event: dict[str, Any] | None = None
        self._anchor_offsets = {role: 0.0 for role in ROLES}

    def reset(
        self,
        time_seconds: int,
        revision: str,
        anchor_revision: str,
        anchor_offsets: Mapping[str, float] | None = None,
    ) -> None:
        if (
            not isinstance(time_seconds, int)
            or isinstance(time_seconds, bool)
            or time_seconds < self._last_time
        ):
            raise ValueError("reset boundary must not precede the last frame")
        supplied = dict(anchor_offsets or {})
        if not set(supplied).issubset(ROLES) or not all(_finite(v) for v in supplied.values()):
            raise ValueError("reset requires finite offsets for known source roles")
        if not isinstance(revision, str) or not isinstance(anchor_revision, str):
            raise TypeError("reset revisions must be strings")
        if not revision or not anchor_revision or len(revision) > 256 or len(anchor_revision) > 256:
            raise ValueError("reset revisions require 1 through 256 characters")
        if revision == self._revisions[0] or anchor_revision == self._revisions[1]:
            raise ValueError("reset requires a new revision and a new anchor_revision")
        self.history.clear()
        self._anchor_validity.clear()
        self._anchor_invalid_counts = dict.fromkeys(ROLES, 0)
        self._contiguous = 0
        self._last_time = time_seconds
        self._revisions = (revision, anchor_revision)
        self._revision_event = None
        self._anchor_offsets = {role: float(supplied.get(role, 0)) for role in ROLES}
        self._tags = None

    def _support(self, end: int) -> dict[str, Any]:
        start = max(0, end - WINDOW_SECONDS)
        evidence: dict[str, Any] = {
            "sample_interval": [self.source_offset_seconds + start, self.source_offset_seconds + end],
            "shared_support": [],
            "supported_context": [],
            "source_support": {},
            "thresholds_dbfs": {},
            "common_estimate_db": None,
            "source_level_changes_db": {},
            "opportunity": False,
            "support_kind": "raw_energy_mask_not_semantic_activity",
        }
        if end > len(self.anchor["RED"]):
            evidence["support_reason"] = "anchor_exhausted"
            return evidence
        if end < WINDOW_SECONDS:
            evidence["support_reason"] = "warmup"
            return evidence
        cached_validation = (
            len(self._anchor_validity) == WINDOW_SECONDS
            and self._anchor_validity[0][0] == start
            and self._anchor_validity[-1][0] == end - 1
        )
        for role in ROLES:
            values = self.anchor[role][start:end]
            valid = (
                self._anchor_invalid_counts[role] == 0 if cached_validation else
                all(_finite(value) for value in values)
            )
            if not valid:
                evidence["support_reason"] = "invalid_anchor"
                return evidence
            # This exact trailing window was validated above, including nonfinite
            # values. Do not repeat all eight validations inside percentile().
            threshold = max(-60.0, _validated_percentile(values, 0.9) - 24.0)
            evidence["thresholds_dbfs"][role] = threshold
            evidence["source_support"][role] = [
                start + index for index, value in enumerate(values) if value > threshold
            ]
        support = evidence["source_support"]
        shared = sorted(set(support["AC GUITAR"]).intersection(support["RED"]))
        context = [role for role in CONTEXT_ROLES if len(support[role]) >= 4]
        evidence["shared_support"] = shared
        evidence["supported_context"] = context
        evidence["opportunity"] = len(shared) >= 4 and len(context) >= 2
        evidence["support_reason"] = (
            "supported" if evidence["opportunity"] else "insufficient_raw_support"
        )
        return evidence

    def _frame_reason(self, frame: Mapping[str, Any]) -> str | None:
        if frame.get("valid", True) is not True:
            return str(frame.get("reason") or "declared_invalid")
        for flag in ("attribution_ambiguous", "alignment_unknown", "duplicate_or_bleed_ambiguous"):
            if frame.get(flag, False):
                return flag
        if frame.get("alignment_valid", True) is not True:
            return "alignment_unknown"
        for key, limit, inclusive in (
            ("observation_age_seconds", 0.5, True),
            ("state_age_seconds", 2.5, False),
        ):
            age = frame.get(key, 0.0)
            if not _finite(age) or age < 0:
                return "invalid_age"
            if age >= limit if inclusive else age > limit:
                return "stale_observation" if inclusive else "stale_state"
        levels = frame.get("levels")
        if not isinstance(levels, Mapping) or set(levels) != _ROLE_SET:
            return "missing_or_unknown_source"
        if not all(_finite(levels[role]) for role in ROLES):
            return "nonfinite_level"
        return None

    def push(self, frame: Mapping[str, Any]) -> dict[str, Any] | None:
        time = frame.get("time_seconds")
        if not isinstance(time, int) or isinstance(time, bool) or time <= self._last_time:
            raise ValueError("frame endpoints must be increasing positive integer seconds")
        gap = time != self._last_time + 1
        self._last_time = time
        if gap:
            self._anchor_validity.clear()
            self._anchor_invalid_counts = dict.fromkeys(ROLES, 0)
        if time <= len(self.anchor["RED"]):
            # One validation per new, already reached anchor frame. Counts let
            # a covered trailing window reuse those bits without rescanning them.
            invalid_roles = tuple(role for role in ROLES if not _finite(self.anchor[role][time - 1]))
            if len(self._anchor_validity) == WINDOW_SECONDS:
                for role in self._anchor_validity.popleft()[1]:
                    self._anchor_invalid_counts[role] -= 1
            self._anchor_validity.append((time - 1, invalid_roles))
            for role in invalid_roles:
                self._anchor_invalid_counts[role] += 1
        revision = frame.get("revision", "base")
        anchor_revision = frame.get("anchor_revision", "base")
        completed_revision = frame.get("completed_revision", self._revisions[0])
        tags = (frame.get("epoch"), frame.get("format"))
        reason = self._frame_reason(frame)
        if not isinstance(revision, str) or not isinstance(anchor_revision, str):
            reason = "invalid_revision"
        if gap:
            reason = reason or "timing_gap"
        if self._tags is not None and tags != self._tags:
            reason = reason or "epoch_or_format_change"
        revision_mismatch = (revision, anchor_revision) != self._revisions
        completed_mismatch = completed_revision != self._revisions[0]
        if self._revision_event is None and (revision_mismatch or completed_mismatch):
            self._revision_event = {
                "time_seconds": time,
                "revision": revision,
                "anchor_revision": anchor_revision,
                "completed_revision": completed_revision,
                "reason": "revision_change" if revision_mismatch else "inflight_revision_change",
            }
        if self._revision_event is not None:
            reason = self._revision_event["reason"]
            revision = self._revision_event["revision"]
            anchor_revision = self._revision_event["anchor_revision"]
            completed_revision = self._revision_event["completed_revision"]
        self._tags = tags
        if reason:
            self.history.clear()
            self._contiguous = 0
        else:
            self.history.append({"time_seconds": time, "levels": dict(frame["levels"])})
            self._contiguous = min(WINDOW_SECONDS, self._contiguous + 1)
        if time % 2:
            return None
        evidence = self._support(time)
        eligible = False
        raw_error = None
        reason = reason or (
            "warmup" if time < WINDOW_SECONDS else
            "fresh_history_required" if self._contiguous < WINDOW_SECONDS else
            evidence["support_reason"] if not evidence["opportunity"] else None
        )
        if reason is None:
            current = {row["time_seconds"] - 1: row["levels"] for row in self.history}
            shared = evidence["shared_support"]
            supports = evidence["source_support"]
            context = evidence["supported_context"]
            checked = {"RED": shared, "AC GUITAR": shared}
            checked.update({role: supports[role] for role in context})
            if any(current[index][role] <= -60 for role, indices in checked.items() for index in indices):
                reason = "current_silence_on_fixed_support"
                self.history.clear()
                self._contiguous = 0
            else:
                changes = {
                    role: median(
                        current[index][role] - self.anchor[role][index] - self._anchor_offsets[role]
                        for index in indices
                    )
                    for role, indices in checked.items()
                }
                common = median(changes[role] for role in context)
                evidence["source_level_changes_db"] = changes
                evidence["common_estimate_db"] = common
                raw_error = median(
                    (current[index]["AC GUITAR"] - current[index]["RED"])
                    - (self.anchor["AC GUITAR"][index] - self.anchor["RED"][index])
                    - (self._anchor_offsets["AC GUITAR"] - self._anchor_offsets["RED"])
                    for index in shared
                )
                if max(changes[role] for role in context) - min(changes[role] for role in context) > 0.5:
                    reason = "context_gain_disagreement"
                elif abs(changes["RED"] - common) > 0.5:
                    reason = "red_common_gain_disagreement"
                else:
                    eligible = True
                    reason = "eligible"
                if not eligible:
                    # Even a window-level attribution disagreement is invalid
                    # decision evidence. Do not reuse its apparently fresh frames
                    # immediately after the source relationship becomes consistent.
                    self.history.clear()
                    self._contiguous = 0
        if reason == "invalid_anchor":
            self.history.clear()
            self._contiguous = 0
        evidence["runtime_veto"] = evidence["opportunity"] and not eligible
        return {
            "time_seconds": time,
            "raw_error_db": raw_error,
            "eligible": eligible,
            "reason": reason,
            "observation_age_seconds": frame.get("observation_age_seconds", 0.0),
            "state_age_seconds": frame.get("state_age_seconds", 0.0),
            "contiguous_seconds": self._contiguous,
            "revision": revision,
            "anchor_revision": anchor_revision,
            "completed_revision": completed_revision,
            "revision_event": dict(self._revision_event) if self._revision_event is not None else None,
            "opportunity": evidence["opportunity"],
            "evidence": evidence,
        }
