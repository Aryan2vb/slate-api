import pytest
from pydantic import BaseModel

from app.adapters.llm.fallback import FallbackLanguageModel
from app.domain.errors import ProviderError


class Out(BaseModel):
    text: str


class Scripted:
    def __init__(self, name: str, error: ProviderError | None = None) -> None:
        self.name = name
        self.error = error
        self.calls = 0

    async def generate(self, *, system, prompt, schema, max_tokens=8000):
        self.calls += 1
        if self.error:
            raise self.error
        return schema(text=self.name)


async def test_busy_model_falls_through_to_the_next() -> None:
    busy = Scripted("a", ProviderError("a", "HTTP 503", retryable=True))
    ok = Scripted("b")
    llm = FallbackLanguageModel([busy, ok, Scripted("c")])

    out = await llm.generate(system="s", prompt="p", schema=Out)

    assert out.text == "b"
    assert llm.name == "b"


async def test_permanent_errors_are_not_masked_by_fallbacks() -> None:
    bad = Scripted("a", ProviderError("a", "HTTP 400", retryable=False))
    other = Scripted("b")
    with pytest.raises(ProviderError, match="400"):
        await FallbackLanguageModel([bad, other]).generate(system="s", prompt="p", schema=Out)
    assert other.calls == 0


async def test_raises_the_last_error_when_every_model_is_busy() -> None:
    models = [Scripted(n, ProviderError(n, f"{n} busy", retryable=True)) for n in "ab"]
    with pytest.raises(ProviderError, match="b busy") as exc:
        await FallbackLanguageModel(models).generate(system="s", prompt="p", schema=Out)
    assert exc.value.retryable  # the job queue still retries later
