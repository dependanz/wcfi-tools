"""Local, CLI-launched web UI (the first brick of a future hosted web app)."""

from .annotator import build_app, run_annotator

__all__ = ["build_app", "run_annotator"]
