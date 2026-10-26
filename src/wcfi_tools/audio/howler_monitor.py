"""Run the same causal detector on a file or a microphone input."""
from __future__ import annotations

import math


def monitor(source, *, seconds=None, min_frequency=100., max_frequency=10000.,
            min_duration=.5, threshold_db=15., emit=None):
    """Consume an AudioInput. Events and reports contain measurements, never audio.

    Times count received samples. Unknown capture gaps are reported explicitly,
    and detection history is cleared across them. Scores are heuristic evidence.
    """
    import numpy as np

    from .howler import HowlerDetector

    rate = source.info["sample_rate"]
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("seconds must be a positive finite number")
    detector = HowlerDetector(rate, min_frequency=min_frequency, max_frequency=max_frequency,
                              min_duration=min_duration, threshold_db=threshold_db)
    send = emit or (lambda row: None)
    limit = max(1, int(seconds * rate)) if seconds is not None else None
    samples = gaps = clipped = event_count = feedback_count = 0
    peak = sum_squares = 0.
    events = []
    # Bound report memory for an indefinitely running listener. All notifications
    # still reach emit (including JSONL stdout), even after this archive fills.
    event_limit = 1000
    last_level = 0
    interrupted = False
    session = {
        "type": "session", "detector": detector.configuration["algorithm"],
        "input": dict(source.info), "settings": detector.configuration,
        "time_basis": "received_samples_excluding_unknown_capture_gaps",
        "score_kind": "heuristic_not_probability",
    }
    send(session)

    def dispatch(rows):
        nonlocal event_count, feedback_count
        for row in rows:
            if row["event"] == "end":
                event_count += 1
                feedback_count += row["classification"] == "feedback_like"
                if len(events) < event_limit:
                    events.append(row)
            send({"type": "detection", **row})

    try:
        while limit is None or samples < limit:
            block = source.read()
            if block is None:
                break
            audio = np.asarray(block.samples)
            if limit is not None:
                audio = audio[:limit - samples]
            if audio.ndim != 1 or not np.isfinite(audio).all():
                raise ValueError("Input must contain finite mono audio samples")
            if not len(audio):
                raise ValueError("Input returned an empty audio block")
            if block.gap:
                gaps += 1
                dispatch(detector.finish())
                detector.reset(start_seconds=samples / rate)
                send({"type": "gap", "time_seconds": samples / rate,
                      "message": "Capture gap; persistence history reset. Gap length is unknown."})
            samples += len(audio)
            block_peak = float(np.max(np.abs(audio)))
            peak = max(peak, block_peak)
            clipped += int(np.count_nonzero(np.abs(audio) >= .999))
            power = float(np.dot(audio.astype(np.float64), audio.astype(np.float64)))
            sum_squares += power
            dispatch(detector.feed(audio))
            if source.info["kind"] == "microphone" and samples - last_level >= rate:
                last_level = samples
                send({"type": "level", "time_seconds": samples / rate,
                      "rms_dbfs": 10 * math.log10(max(power / len(audio), 1e-12)),
                      "peak_dbfs": 20 * math.log10(max(block_peak, 1e-6))})
    except KeyboardInterrupt:
        interrupted = True
    dispatch(detector.finish())
    if samples == 0 and not interrupted:
        raise ValueError("No audio samples were received")
    report = {
        **session, "type": "summary", "duration_seconds": samples / rate,
        "interrupted": interrupted, "capture_gaps": gaps,
        "rms_dbfs": 10 * math.log10(max(sum_squares / max(samples, 1), 1e-12)),
        "peak_dbfs": 20 * math.log10(max(peak, 1e-6)),
        "clipped_samples": clipped, "event_count": event_count,
        "feedback_like_count": feedback_count, "events": events,
        "events_omitted": max(0, event_count - event_limit),
        "interpretation": "Tonal candidates, not proof of acoustic feedback or its physical cause.",
    }
    send(report)
    return report
