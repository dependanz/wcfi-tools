"""Versioned analysis profiles: operator intent, detectors and permitted proposals.

These profiles are engineering hypotheses, not professional mix targets. They do
not use the legacy soundcheck octave-shape presets or normalize quiet inputs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RoleProfile:
    name: str
    source: str
    role: str
    description: str
    analyses: tuple[str, ...]
    peak_ceiling_dbfs: float = -6.0
    max_attenuation_db: float = 12.0
    version: int = 1

    def describe(self):
        return asdict(self) | {
            "intent_origin": "operator_selected",
            "detector_status": "experimental_deterministic_heuristics",
            "ceiling_interpretation": "Configurable sample-peak headroom, not a vocal/mix loudness target",
            "automatic_boost": False,
            "audible_improvement_validated": False,
        }


PROFILES = {
    "red": RoleProfile("red", "vocal_microphone", "lead_vocal",
        "RED: worship leader; singing with possible band bleed. Preserve vocal body and clarity.",
        ("signal_health", "headroom", "vocal_dynamics", "sibilance_candidates")),
    "kit": RoleProfile("kit", "combined_stereo_drum_feed", "drum_bus",
        "KIT: complete premixed drum kit. Preserve hits and cymbal decays; individual drum control is unavailable.",
        ("signal_health", "headroom", "transient_dynamics", "cymbal_balance", "stereo_health")),
}


def get_profile(name):
    if not isinstance(name, str) or name.casefold() not in PROFILES:
        raise ValueError("Choose an analysis profile: red or kit")
    return PROFILES[name.casefold()]
