"""Claude via the Anthropic SDK, using structured outputs so every response validates against the caller's schema."""

from __future__ import annotations

import anthropic
from pydantic import BaseModel, ValidationError

from app.domain.errors import ProviderError

DEFAULT_MODEL = "claude-opus-5-5"
# Adaptive thinking shares max_tokens with the visible output; this headroom keeps the JSON from being truncated.
THINKING_HEADROOM = 8_000
# Server-side refusal fallback: the API re-runs a declined request on Anthropic's recommended model for the
# refusal category. The "default" form requires exactly this beta header.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLanguageModel:
    def __init__(
        self,
        api_key: str = "",
        *,
        model: str | None = None,
        effort: str = "medium",
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        if client is None and not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for the anthropic LLM provider")
        self._model = model or DEFAULT_MODEL
        self._effort = effort
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)
        self.name = f"anthropic:{self._model}"

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        try:
            response = await self._client.beta.messages.parse(
                model=self._model,
                max_tokens=max_tokens + THINKING_HEADROOM,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},  # type: ignore[typeddict-item]
                output_format=schema,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise ProviderError(self.name, f"rate limited: {e.message}", retryable=True) from e
        except anthropic.APIStatusError as e:
            retryable = e.status_code >= 500 or e.status_code in (408, 409, 429)
            raise ProviderError(self.name, f"HTTP {e.status_code}: {e.message}", retryable=retryable) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(self.name, f"connection error: {e.message}", retryable=True) from e
        except (ValidationError, ValueError) as e:
            raise ProviderError(self.name, f"output did not match {schema.__name__}: {e}", retryable=False) from e

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ProviderError(self.name, f"model declined the request (category={category})", retryable=False)
        if response.stop_reason == "max_tokens":
            raise ProviderError(self.name, "output truncated at max_tokens", retryable=False)

        parsed = response.parsed_output
        if parsed is None:
            raise ProviderError(self.name, f"no {schema.__name__} in response", retryable=False)
        return parsed
