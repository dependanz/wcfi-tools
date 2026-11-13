"""Recent-evidence veto for the isolated scalar gain experiment.

Six initial overlapping envelope changes are an observed variability allowance,
not a confidence interval or musical-intent classifier. No I/O or host controls.
The v1 policy and its known failed evidence remain unchanged.
"""
from __future__ import annotations

from statistics import median

from .experimental_audio_gain import ExperimentalAudioGain


class ExperimentalAudioGainV2(ExperimentalAudioGain):
    """Preserve v1 decisions except additional pre-call attenuation vetoes."""

    def __init__(self):
        super().__init__()
        self._recent = None

    def _four_error(self, frames):
        if self._reference is None or len(frames) != 4:
            return None
        thresholds = self._reference["thresholds_dbfs"]
        supported = self._support(frames, thresholds, "AC GUITAR")
        if len(supported) != 4:
            return None
        if [f["endpoint"] for f in supported] != list(range(
                supported[0]["endpoint"], supported[0]["endpoint"] + 4)):
            return None
        return median(f["levels"]["AC GUITAR"] - f["levels"]["RED"] for f in supported) - self._reference["theta_db"]["AC GUITAR"]

    def _calibrate(self):
        super()._calibrate()
        if self._reference is None:
            return
        deltas = []
        for endpoint in range(6, 17, 2):
            current = self._four_error(self._calibration[endpoint - 4:endpoint])
            previous = self._four_error(self._calibration[endpoint - 6:endpoint - 2])
            if current is None or previous is None:
                deltas = None
                break
            deltas.append(current - previous)
        self._reference["recent_calibration_supported"] = deltas is not None
        self._reference["recent_calibration_deltas_db"] = deltas
        self._reference["minimum_recent_change_db"] = min(0., *deltas) if deltas is not None else None

    def _measure(self, endpoint):
        raw, evidence, reason = super()._measure(endpoint)
        history = list(self._history)
        current = self._four_error(history[-4:])
        previous = self._four_error(history[-6:-2]) if len(history) >= 6 else None
        minimum = self._reference["minimum_recent_change_db"]
        recent = {
            "recent_error_db": current,
            "previous_recent_error_db": previous,
            "recent_corrected_error_db": current + self.offset_db if current is not None else None,
            "recent_change_db": current - previous if current is not None and previous is not None else None,
            "reference_minimum_recent_change_db": minimum,
            "recent_support_endpoints": [f["endpoint"] for f in history[-6:]]
                if current is not None and previous is not None else [],
            "recent_veto_reason": None,
            "interpretation": "finite_initial_variability_not_intent_or_validated_uncertainty",
        }
        self._recent = recent
        evidence["recent"] = recent
        return raw, evidence, reason

    def _qualify(self, endpoint, raw, reason, contiguous):
        # Original guards and release/overcorrection handling remain authoritative.
        # The positive-error veto cannot prevent a qualified return-to-reference
        # release, and does not fabricate eligibility when v1 rejects evidence.
        if (reason or contiguous < 8 or abs(raw) <= .5
                or raw + self.offset_db < -.75):
            return super()._qualify(endpoint, raw, reason, contiguous)
        recent = self._recent
        veto = None
        if not self._reference["recent_calibration_supported"]:
            veto = "recent_calibration_unavailable"
        elif (recent is None or recent["recent_error_db"] is None
              or recent["previous_recent_error_db"] is None):
            veto = "recent_pair_support_unavailable"
        elif recent["recent_corrected_error_db"] < 1.5:
            veto = "recent_corrected_error_insufficient"
        elif recent["recent_change_db"] < recent["reference_minimum_recent_change_db"] - 1e-9:
            veto = "recent_fall_exceeds_initial_variation"
        if recent is not None:
            recent["recent_veto_reason"] = veto
        if veto:
            self._clear_episode()
            self.phase = "hold"
            # The inherited evaluator makes its single ineligible controller call;
            # no speculative synchronous mutation is made and then concealed.
            return False, veto
        return super()._qualify(endpoint, raw, reason, contiguous)

    def _evaluate(self, endpoint, slot):
        row = super()._evaluate(endpoint, slot)
        row["policy_version"] = "experimental_audio_gain_v2"
        return row
