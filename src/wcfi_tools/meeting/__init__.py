"""Meeting-minutes summarization core."""

from .pipeline import PipelineResult, summarize_meeting

__all__ = ["summarize_meeting", "PipelineResult"]
