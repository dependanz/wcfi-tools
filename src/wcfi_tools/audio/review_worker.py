"""Resource-limited child process for finite app analysis, with no playback IO."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def run(request, output):
    # Keep NumPy/FFT imports after the worker's CPU/thread budget is installed.
    import soundfile as sf

    from ..ableton.client import connect
    from .review_capture import capture_pairs
    from .role_analysis import observe, render_proposals
    from .role_replay import save_observation, take_windows

    if request["source"] == "take":
        windows = take_windows(Path(request["take_path"]), start=request["start"], seconds=request["seconds"])
        captures = {name: {"before": a, "after": a, "rate": rate,
            "state": {"kind": "local_bypass_reference", "devices": []},
            "provenance": provenance | {"post_fx_kind": "bypass_reference_not_current_ableton_chain"}}
            for name, (a, rate, provenance) in windows.items()}
    else:
        captures = capture_pairs(connect(), request["seconds"])
    reports = {}
    for name, capture in captures.items():
        context_available = request["source"] == "take" or capture["shared_time_available"]
        reports[name] = save_observation(output, name, capture["before"], capture["after"],
            capture["rate"], name.lower(), provenance=capture["provenance"],
            chain_state=capture["state"], aligned=request["source"] == "take",
            context=captures["KIT"]["after"] if name == "RED" and context_available else None, previews=True,
            withhold_hf_reason=("Live did not expose a shared sample clock. RED/KIT bleed context is unverified; HF treatment is withheld."
                                if name == "RED" and not context_available else None))
        # Gain-only audition corresponds exactly to the supported Ableton adapter;
        # the combined experimental candidate must not stand in for this action.
        gain = [p for p in reports[name]["proposals"] if p["kind"] == "software_gain"]
        if gain:
            audio, _ = render_proposals(capture["after"], capture["rate"], gain)
            file = name + "-gain.wav"
            sf.write(output / file, audio, capture["rate"], subtype="FLOAT")
            reports[name]["audio"]["gain"] = file
            reports[name]["gain_preview"] = observe(audio, capture["rate"], name.lower(), stage="preview")
    (output / "result.json").write_text(json.dumps(reports, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    from ..commands.role_analysis import limit_resources

    folder = Path(sys.argv[1]).resolve(strict=True)
    try:
        limit_resources()
        run(json.loads((folder / "request.json").read_text(encoding="utf-8")), folder)
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        (folder / "error.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        raise SystemExit(1) from None
