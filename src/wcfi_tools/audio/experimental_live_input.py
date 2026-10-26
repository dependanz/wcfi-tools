"""Pure scalar adapter for explicitly approximate publication-clock observations.

No capture or host operations exist here. Native publication time is not audio
sample time. The thresholds below are empirical acceptance rules, not a bound
on acoustic synchronization. This separate contract never asserts
``alignment_known=True`` and does not authorize fader writes.

``PublicationClockGainV2(expected_sources).feed(group)`` accepts six unmodified
scalar Source.description rows under ``windows``. Group sequence/time_seconds
are consecutive observation indices, starting at one. All header clock values
are seconds in the native QPC clock domain. State freshness uses the request
start as a conservative lower bound on state acquisition, while preserving the
actual response receipt. Inspect ``input_status`` after every call, including
warmup calls returning no decision rows.

Groups 1..9 are one fixed timing qualification interval. A failure is retained
as startup no-go. Group 9, if qualified, becomes policy endpoint 1; groups 9..24
form the one fixed 16-second starting-balance reference. No search or rearm.
An explicitly flagged, genuinely zero-energy channel contributes zero linear
power. Two zero-energy channels map to a declared -120 dBFS absence floor;
missing/unflagged/nonfinite values are never interpreted as silence.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math

from .experimental_audio_gain import ROLES
from .experimental_audio_gain_v2 import ExperimentalAudioGainV2


RATE = 48000
PUBLICATION_AGE_LIMIT = .250
PUBLICATION_SKEW_LIMIT = .050
ADVANCE_RESIDUAL_LIMIT = 512 / RATE
STATE_AGE_LIMIT = 2.5
SILENCE_FLOOR_DBFS = -120.0
CONTRACT = "experimental_publication_clock_v1"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def _stereo_db(values, zeros):
    # Stable mean power, including a genuinely zero-energy channel's zero power.
    present = [value for value, zero in zip(values, zeros) if not zero]
    if not present:
        return SILENCE_FLOOR_DBFS
    maximum = max(present)
    return maximum + 10 * math.log10(sum(10 ** ((v - maximum) / 10) for v in present) / 2)


class PublicationClockGainV2(ExperimentalAudioGainV2):
    """Replace only input validation; inherit all v2 musical/gain decisions."""

    def __init__(self, expected_sources):
        super().__init__()
        if not isinstance(expected_sources, dict) or set(expected_sources) != set(ROLES):
            raise ValueError("Exactly six expected source identities are required")
        for item in expected_sources.values():
            if (not isinstance(item, dict) or not _identifier(item.get("node_id"))
                    or not _integer(item.get("pid"), 1) or not _integer(item.get("epoch"))):
                raise ValueError("Expected source needs node_id, positive pid, and epoch")
        if len({x["node_id"] for x in expected_sources.values()}) != 6:
            raise ValueError("Expected source identities must be unique")
        if len({x["pid"] for x in expected_sources.values()}) != 1:
            raise ValueError("Expected sources must belong to one host")
        self._expected = deepcopy(expected_sources)
        self._timing = {role: deque(maxlen=9) for role in ROLES}
        self._last_group = 0
        self._last_qpc_receipt = None
        self._contract_identity = None
        self._start_group = None
        self._startup_failure = None
        self._pending_slot = None
        self._input_status = {"reason": "not_started", "host_sample_alignment": False,
                              "time_basis": "publication_clock_approximate", "policy_started": False}

    @property
    def input_status(self):
        return deepcopy(self._input_status)

    def _clear_timing(self):
        for history in self._timing.values():
            history.clear()

    def _validate(self, group, index):
        evidence = {"contract": CONTRACT, "observation_group_index": index,
            "time_basis": "publication_clock_approximate", "host_sample_alignment": False,
            "acoustic_skew_bound_seconds": None, "publication_windows_qualified": False,
            "timing_warmup_complete": False, "end_publication_skew_seconds": None,
            "max_counter_qpc_residual_seconds": None, "observation_age_seconds": None,
            "state_age_seconds": None, "zero_energy_channels": {},
            "silence_floor_dbfs": SILENCE_FLOOR_DBFS, "reason": None}
        reason = None
        received = group.get("received_at_qpc")
        observed = group.get("state_observed_at_qpc")
        query_start = group.get("state_query_started_at_qpc")
        if (not all(_number(x) for x in (received, observed, query_start))
                or not 0 <= query_start <= observed <= received):
            self._hard("invalid_publication_or_state_clock")
        elif self._last_qpc_receipt is not None and received < self._last_qpc_receipt:
            self._hard("publication_receipt_clock_rollback")
        else:
            self._last_qpc_receipt = received
            evidence["state_age_seconds"] = received - query_start
            evidence["state_response_age_seconds"] = received - observed
            evidence["state_time_basis"] = "acquisition_lower_bound_from_request_start"
            if received - query_start > STATE_AGE_LIMIT:
                reason = "stale_state"
        if group.get("host_sample_alignment") is not False or group.get("alignment_known") is True:
            self._hard("unsupported_alignment_claim")
        keys = ("source_map_revision", "routing_revision", "state_revision")
        if not all(_identifier(group.get(key)) for key in keys):
            self._hard("unknown_state_or_mapping_identity")
        else:
            identity = {key: group[key] for key in keys}
            if self._contract_identity is None:
                self._contract_identity = identity
            elif self._contract_identity != identity:
                self._hard("state_or_mapping_changed")
        windows = group.get("windows")
        if not isinstance(windows, dict) or set(windows) != set(ROLES):
            reason = reason or "missing_source_window"
            windows = {}
        levels, points, publications = {}, {}, []
        residuals, spans = [], []
        for role in ROLES:
            row = windows.get(role)
            if not isinstance(row, dict):
                reason = reason or "missing_source_window"
                continue
            expected = self._expected[role]
            if (row.get("source") != role or row.get("node_id") != expected["node_id"]
                    or type(row.get("pid")) is not int or row["pid"] != expected["pid"]
                    or type(row.get("epoch")) is not int or row["epoch"] != expected["epoch"]
                    or row.get("sample_rate") != RATE or row.get("channels") != 2
                    or row.get("stage") != "after_effects_before_fader"):
                self._hard("source_identity_epoch_or_format_changed")
                continue
            first, end = row.get("window_start_local_frame"), row.get("window_end_local_frame")
            if not (_integer(first) and _integer(end) and end - first == RATE):
                reason = reason or "incomplete_source_window"
                continue
            counters = [row.get(key) for key in ("missing_frames", "read_failures", "stale_reads")]
            bad_samples = row.get("nonfinite_samples")
            if (row.get("complete") is not True or row.get("measurement_valid") is not True
                    or type(row.get("observed_frames")) is not int or row["observed_frames"] != RATE
                    or any(type(x) is not int or x != 0 for x in counters)
                    or not isinstance(bad_samples, (list, tuple)) or len(bad_samples) != 2
                    or any(type(x) is not int or x != 0 for x in bad_samples)):
                reason = reason or "invalid_or_gapped_source_window"
            values = row.get("rms_dbfs")
            zeros = row.get("zero_energy_channels", [False, False])
            if (not isinstance(values, (list, tuple)) or len(values) != 2
                    or not isinstance(zeros, (list, tuple)) or len(zeros) != 2
                    or any(type(x) is not bool for x in zeros)
                    or any((value is not None if zero else not _number(value))
                           for value, zero in zip(values, zeros))):
                reason = reason or "nonfinite_source_rms"
                continue
            evidence["zero_energy_channels"][role] = list(zeros)
            levels[role] = _stereo_db(values, zeros)
            amin, amax = row.get("capture_age_min_ms"), row.get("capture_age_max_ms")
            if not (_number(amin) and _number(amax) and 0 <= amin <= amax < 250):
                reason = reason or "stale_source_window"
            status = row.get("last_status")
            callback = row.get("callback_last_ns")
            if (not isinstance(status, dict) or status.get("active") is not True
                    or not _integer(callback, 1) or status.get("callback_ns") != callback
                    or not _integer(status.get("written_frame"))
                    or not 0 <= status["written_frame"] - end <= 32768):
                reason = reason or "unknown_publication_endpoint"
                continue
            publication = callback / 1e9 - (status["written_frame"] - end) / RATE
            if not _number(received) or not 0 <= received - publication < PUBLICATION_AGE_LIMIT:
                reason = reason or "stale_publication_window"
            publications.append(publication)
            point = (index, first, end, publication)
            history = self._timing[role]
            if history:
                previous = history[-1]
                if first < previous[2] or publication <= previous[3]:
                    self._hard("source_counter_or_publication_clock_rollback")
                elif index != previous[0] + 1 or first != previous[2]:
                    reason = reason or "source_window_discontinuity"
                for older in history:
                    if index - older[0] <= 8:
                        residuals.append(abs((end - older[2]) / RATE - (publication - older[3])))
                spans.append(index - history[0][0])
            else:
                spans.append(0)
            points[role] = point
        if len(publications) == 6:
            evidence["end_publication_skew_seconds"] = max(publications) - min(publications)
            if _number(received):
                evidence["observation_age_seconds"] = max(received - x for x in publications)
            if evidence["end_publication_skew_seconds"] > PUBLICATION_SKEW_LIMIT + 1e-9:
                reason = reason or "publication_skew_exceeded"
        if residuals:
            evidence["max_counter_qpc_residual_seconds"] = max(residuals)
            if max(residuals) > ADVANCE_RESIDUAL_LIMIT + 1e-9:
                reason = reason or "counter_qpc_advance_residual_exceeded"
        full_history = len(spans) == 6 and min(spans) >= 8
        if self._hard_reason:
            reason = self._hard_reason
        if reason:
            self._clear_timing()
        else:
            for role in ROLES:
                self._timing[role].append(points[role])
        evidence["timing_warmup_complete"] = full_history and reason is None
        if reason is None and not full_history:
            reason = "publication_timing_warmup"
        evidence["reason"] = reason
        evidence["publication_windows_qualified"] = reason is None
        return levels, evidence

    def feed(self, group):
        """Feed an observed group; return inherited v2 decision rows when due."""
        if not isinstance(group, dict):
            self._hard("malformed_publication_group")
            self._input_status = {**self._input_status, "reason": self._hard_reason}
            return []
        index = group.get("time_seconds")
        if (not _integer(index, 1) or group.get("sequence") != index
                or type(group.get("sequence")) is not int or index <= self._last_group):
            self._hard("duplicate_or_invalid_observation_group")
            self._input_status = {**self._input_status, "reason": self._hard_reason}
            return []
        if self._last_group == 0 and index != 1:
            self._startup_failure = "missing_initial_timing_groups"
        if index != self._last_group + 1:
            self._clear_timing()
            if self._start_group is None:
                self._startup_failure = "missing_initial_timing_groups"
        self._last_group = index
        levels, evidence = self._validate(group, index)
        if self._start_group is None:
            if evidence["reason"] not in (None, "publication_timing_warmup"):
                self._startup_failure = self._startup_failure or evidence["reason"]
            if index == 9 and evidence["publication_windows_qualified"] and self._startup_failure is None:
                self._start_group = index
            elif index >= 9:
                self._startup_failure = self._startup_failure or "fixed_timing_warmup_failed"
        if self._startup_failure:
            evidence["reason"] = "startup_no_go:" + self._startup_failure
            evidence["publication_windows_qualified"] = False
        endpoint = index - self._start_group + 1 if self._start_group is not None else None
        evidence.update(policy_started=self._start_group is not None, policy_endpoint=endpoint)
        self._input_status = deepcopy(evidence)
        if endpoint is None:
            return []
        slot = {"endpoint": endpoint, "levels": levels if evidence["publication_windows_qualified"] else None,
                "valid": evidence["publication_windows_qualified"], "reason": evidence["reason"],
                "observation_age_seconds": evidence["observation_age_seconds"],
                "state_age_seconds": evidence["state_age_seconds"], "input_evidence": deepcopy(evidence)}
        self._pending_slot = slot
        try:
            return super().feed({"time_seconds": endpoint, "_publication_slot": slot})
        finally:
            self._pending_slot = None

    def _frame(self, incoming, endpoint):
        # Only this adapter's validated synchronous slot can enter inherited logic.
        if incoming.get("_publication_slot") is not self._pending_slot or self._pending_slot is None:
            self._hard("unvalidated_publication_slot", endpoint)
            return {"endpoint": endpoint, "levels": None, "valid": False, "reason": self._hard_reason,
                    "observation_age_seconds": None, "state_age_seconds": None}
        return deepcopy(self._pending_slot)

    def _evaluate(self, endpoint, slot):
        row = super()._evaluate(endpoint, slot)
        row["input_contract"] = CONTRACT
        row["host_sample_alignment"] = False
        row["input_evidence"] = deepcopy(slot.get("input_evidence"))
        row["observation_group_index"] = self._start_group + endpoint - 1
        return row
