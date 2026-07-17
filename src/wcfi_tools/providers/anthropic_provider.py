"""Anthropic (Claude) provider: structured-JSON summarization via forced tool use.

Claude does not transcribe audio, so this module only provides a ``Summarizer``.
Structured output is obtained by exposing a single tool whose ``input_schema`` is the
requested JSON schema and forcing the model to call it.
"""

from __future__ import annotations

from typing import Any


class AnthropicSummarizer:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 16000, timeout: float = 600.0) -> None:
        from anthropic import Anthropic

        self.model = model
        self.max_tokens = max_tokens
        self.client = Anthropic(api_key=api_key, timeout=timeout)

    def structured_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        reasoning_effort: str = "low",  # accepted for interface parity; not used by Claude
    ) -> dict[str, Any]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": schema_name,
                    "description": "Emit the structured result. You MUST call this tool.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": schema_name},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == schema_name:
                return dict(block.input)
        raise RuntimeError(f"Claude did not return a tool_use block for schema {schema_name}.")
