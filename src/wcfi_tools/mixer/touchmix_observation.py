"""Read-only decoding of additional TouchMix message classes seen in a capture.

These schemas are observations, not official QSC protocol documentation.
No outbound messages are constructed or transmitted by this module.
"""

from __future__ import annotations

import math
import struct

from .touchmix import Frame, TouchMixError


def summarize_frame(frame: Frame) -> dict[str, object]:
    """Summarize selected captured message layouts without sensitive raw bytes.

    Kind/subtype meanings remain provisional. An index in a 4/2 event is NOT
    necessarily a fader index, and none of these messages is a write ACK.
    """
    result: dict[str, object] = {
        "kind": frame.kind,
        "subtype": frame.subtype,
        "payload_bytes": len(frame.payload),
    }
    if (frame.kind, frame.subtype) == (4, 2):
        if len(frame.payload) != 6:
            raise TouchMixError("Unexpected 4/2 payload length")
        candidate_index, value = struct.unpack("<Hf", frame.payload)
        if not math.isfinite(value):
            raise TouchMixError("Non-finite 4/2 value")
        return {**result, "class": "single_value_candidate",
                "candidate_index": candidate_index, "float32_value": value}

    if (frame.kind, frame.subtype) in ((6, 1), (7, 1)):
        if len(frame.payload) < 6:
            raise TouchMixError("Short grouped frame")
        sequence, declared_bytes = struct.unpack_from("<IH", frame.payload)
        actual_bytes = len(frame.payload) - 6
        if declared_bytes != actual_bytes or declared_bytes % 4:
            raise TouchMixError("Invalid grouped-frame byte length")
        # The 6/1 request body appears to contain packed selector words and
        # the 7/1 reply body appears to contain float words. Do not assign
        # meanings or expose raw values at this stage.
        return {**result, "class": "grouped_request_candidate" if frame.kind == 6
                else "grouped_response_candidate", "sequence": sequence,
                "word_count": declared_bytes // 4}

    if (frame.kind, frame.subtype) == (8, 3):
        if len(frame.payload) != 12:
            raise TouchMixError("Unexpected 8/3 payload length")
        return {**result, "class": "session_message_candidate", "word_count": 3}

    return {**result, "class": "opaque"}
