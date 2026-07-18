"""Local speaker identity: a voiceprint database and cluster-naming logic.

Kept UI-agnostic and dependency-light (pure Python + math) so the same core backs the CLI
walk-through today and a web front-end later.
"""

from .identify import (
    Proposal,
    attribute_turns,
    enroll_confirmed,
    propose_from_voiceprints,
    select_representative_turns,
)
from .voiceprints import VoiceprintDB, cosine_similarity, db_path

__all__ = [
    "VoiceprintDB",
    "cosine_similarity",
    "db_path",
    "Proposal",
    "propose_from_voiceprints",
    "attribute_turns",
    "enroll_confirmed",
    "select_representative_turns",
]
