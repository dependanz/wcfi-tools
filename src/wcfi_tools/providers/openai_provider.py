"""OpenAI provider: transcription (gpt-4o-transcribe) and structured-JSON summarization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _client(api_key: str, timeout: float = 600.0):
    from openai import OpenAI

    return OpenAI(api_key=api_key, timeout=timeout)


def _supports_reasoning_effort(model: str) -> bool:
    return model.lower().startswith(("gpt-5", "o1", "o3", "o4", "gpt-oss"))


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return {"repr": repr(value)}


class OpenAISummarizer:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float = 600.0) -> None:
        self.model = model
        self.client = _client(api_key, timeout)

    def structured_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        reasoning_effort: str = "low",
    ) -> dict[str, Any]:
        text = ""
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "input": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            }
            if reasoning_effort != "none" and _supports_reasoning_effort(self.model):
                kwargs["reasoning"] = {"effort": reasoning_effort}
            response = self.client.responses.create(**kwargs)
            text = self._extract_text(response)
        except (AttributeError, TypeError):
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
            )
            text = completion.choices[0].message.content or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"OpenAI response was not valid JSON for schema {schema_name}: {exc}\n{text[:2000]}"
            ) from exc

    @staticmethod
    def _extract_text(response: Any) -> str:
        text = getattr(response, "output_text", None)
        if isinstance(text, str) and text:
            return text
        data = _to_jsonable(response)
        parts: list[str] = []
        for output in data.get("output", []):
            if not isinstance(output, dict):
                continue
            for content in output.get("content", []):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts)


class OpenAITranscriber:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float = 600.0) -> None:
        self.model = model
        self.client = _client(api_key, timeout)

    def transcribe(self, audio_path: Path, *, prompt: str) -> tuple[str, dict[str, Any]]:
        handle = audio_path.open("rb")
        try:
            result = self.client.audio.transcriptions.create(
                model=self.model,
                file=handle,
                response_format="text",
                prompt=prompt,
            )
        finally:
            handle.close()
        if isinstance(result, str):
            return result.strip(), {"text": result}
        raw = _to_jsonable(result)
        text = raw.get("text", "") if isinstance(raw, dict) else ""
        if not text:
            text = json.dumps(raw, ensure_ascii=False)
        return text.strip(), raw
