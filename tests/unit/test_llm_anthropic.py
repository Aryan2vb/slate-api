import json
from collections.abc import Callable

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.adapters.llm.anthropic import FALLBACK_BETA, AnthropicLanguageModel
from app.domain.errors import ProviderError


class Answer(BaseModel):
    title: str
    points: list[str]


def _message(content: list[dict], stop_reason: str = "end_turn", **extra: object) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        **extra,
    }


def _llm(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicLanguageModel:
    client = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    return AnthropicLanguageModel(client=client)


async def test_success_returns_validated_instance_and_sends_expected_request() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        text = json.dumps({"title": "Pricing", "points": ["Q3 up 5%"]})
        return httpx2.Response(200, json=_message([{"type": "text", "text": text}]))

    result = await _llm(handler).generate(system="sys", prompt="hi", schema=Answer, max_tokens=1000)

    assert result == Answer(title="Pricing", points=["Q3 up 5%"])
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"] == "sys"
    assert FALLBACK_BETA in seen[0].headers["anthropic-beta"]


async def test_refusal_is_not_retryable() -> None:
    stop_details = {"type": "refusal", "category": "cyber", "explanation": "declined"}
    llm = _llm(lambda r: httpx2.Response(200, json=_message([], "refusal", stop_details=stop_details)))
    with pytest.raises(ProviderError) as exc:
        await llm.generate(system="s", prompt="p", schema=Answer)
    assert not exc.value.retryable and "cyber" in str(exc.value)


async def test_max_tokens_truncation_is_not_retryable() -> None:
    llm = _llm(lambda r: httpx2.Response(200, json=_message([{"type": "text", "text": '{"title": "x'}], "max_tokens")))
    with pytest.raises(ProviderError) as exc:
        await llm.generate(system="s", prompt="p", schema=Answer)
    assert not exc.value.retryable


async def test_rate_limit_is_retryable() -> None:
    error = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    llm = _llm(lambda r: httpx2.Response(429, json=error))
    with pytest.raises(ProviderError) as exc:
        await llm.generate(system="s", prompt="p", schema=Answer)
    assert exc.value.retryable


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (404, False), (529, True)])
async def test_status_errors_classified(status: int, retryable: bool) -> None:
    llm = _llm(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}}))
    with pytest.raises(ProviderError) as exc:
        await llm.generate(system="s", prompt="p", schema=Answer)
    assert exc.value.retryable is retryable


async def test_invalid_json_is_not_retryable() -> None:
    llm = _llm(lambda r: httpx2.Response(200, json=_message([{"type": "text", "text": '{"title": 5}'}])))
    with pytest.raises(ProviderError) as exc:
        await llm.generate(system="s", prompt="p", schema=Answer)
    assert not exc.value.retryable


async def test_connection_error_is_retryable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(ProviderError) as exc:
        await _llm(handler).generate(system="s", prompt="p", schema=Answer)
    assert exc.value.retryable
