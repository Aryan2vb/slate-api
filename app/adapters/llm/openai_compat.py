"""Any OpenAI-compatible chat-completions host (Groq by default). Asks for JSON matching the schema, validates it
with Pydantic, and gives the model one chance to repair invalid output."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.http_errors import raise_for_status, transport_error
from app.domain.errors import ProviderError

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"


def _rejects_json_schema(response: httpx.Response) -> bool:
    """Hosts without json_schema support answer 400 and name the parameter; anything else is a real error."""
    return response.status_code == 400 and ("json_schema" in response.text or "response_format" in response.text)


class OpenAICompatibleLanguageModel:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = GROQ_BASE_URL,
        model: str | None = None,
        provider: str = "groq",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(f"An API key is required for the {provider} LLM provider")
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model or GROQ_DEFAULT_MODEL
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0))
        self._json_schema_supported = True
        self.name = f"{provider}:{self._model}"

    def _response_format(self, schema: type[BaseModel]) -> dict[str, Any]:
        if self._json_schema_supported:
            return {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False},
            }
        return {"type": "json_object"}

    async def _complete(self, messages: list[dict[str, str]], schema: type[BaseModel], max_tokens: int) -> str:
        while True:
            body = {
                "model": self._model,
                "messages": messages,
                "max_completion_tokens": max_tokens,
                "response_format": self._response_format(schema),
            }
            try:
                response = await self._client.post(
                    self._url, json=body, headers={"Authorization": f"Bearer {self._api_key}"}
                )
            except httpx.HTTPError as e:
                raise transport_error(self.name, e) from e
            if self._json_schema_supported and _rejects_json_schema(response):
                self._json_schema_supported = False
                continue
            raise_for_status(self.name, response)
            try:
                choice = response.json()["choices"][0]
                content = choice["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise ProviderError(self.name, f"unexpected response shape: {e}", retryable=False) from e
            if choice.get("finish_reason") == "length":
                raise ProviderError(self.name, "output truncated at max_tokens", retryable=False)
            return content

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        # The schema is always in the system prompt: json_object mode has no other way to see it, and it helps
        # hosts whose json_schema mode is advisory.
        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        messages = [
            {
                "role": "system",
                "content": f"{system}\n\nRespond with only a JSON object matching this JSON Schema:\n{schema_json}",
            },
            {"role": "user", "content": prompt},
        ]
        content = await self._complete(messages, schema, max_tokens)
        try:
            return schema.model_validate_json(content)
        except ValidationError as first_error:
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "That JSON did not match the schema. Errors:\n"
                    f"{first_error}\nReturn only the corrected JSON object.",
                },
            ]
        content = await self._complete(messages, schema, max_tokens)
        try:
            return schema.model_validate_json(content)
        except ValidationError as e:
            raise ProviderError(
                self.name, f"output did not match {schema.__name__} after a repair attempt: {e}", retryable=False
            ) from e
