"""Tries a list of models in order, moving on when one is temporarily unavailable.

Hosted models go "busy" (HTTP 503/429) independently of each other, often for minutes. Falling through to the next
model inside one call finishes the job in seconds, where the job queue's backoff would wait minutes between tries.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from pydantic import BaseModel

from app.domain.errors import ProviderError
from app.ports.providers import LanguageModel

log = structlog.get_logger()


class FallbackLanguageModel:
    """Implements LanguageModel. Only retryable provider errors move on; anything else is raised at once."""

    def __init__(self, models: Sequence[LanguageModel]) -> None:
        if not models:
            raise ValueError("FallbackLanguageModel needs at least one model")
        self._models = list(models)
        self.name = self._models[0].name

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        last: ProviderError | None = None
        for model in self._models:
            try:
                result = await model.generate(system=system, prompt=prompt, schema=schema, max_tokens=max_tokens)
            except ProviderError as e:
                if not e.retryable:
                    raise
                log.warning("llm.fallback", model=model.name, error=str(e)[:200])
                last = e
                continue
            self.name = model.name  # recorded with the saved notes
            return result
        assert last is not None
        raise last
