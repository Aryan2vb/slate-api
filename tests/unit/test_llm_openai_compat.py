import json

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from app.adapters.llm.factory import build_language_model
from app.adapters.llm.openai_compat import GROQ_BASE_URL, OpenAICompatibleLanguageModel
from app.adapters.llm.stub import StubLanguageModel
from app.config import Settings
from app.domain.errors import ProviderError
from app.domain.models import EnhancedNotes, SpeakerSuggestion

URL = f"{GROQ_BASE_URL}/chat/completions"


class Answer(BaseModel):
    title: str
    points: list[str]


def _completion(content: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}]}
    )


def _llm() -> OpenAICompatibleLanguageModel:
    return OpenAICompatibleLanguageModel("gk", client=httpx.AsyncClient())


GOOD = json.dumps({"title": "t", "points": ["a"]})


@respx.mock
async def test_success_with_json_schema() -> None:
    route = respx.post(URL).mock(return_value=_completion(GOOD))
    assert await _llm().generate(system="s", prompt="p", schema=Answer) == Answer(title="t", points=["a"])
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["response_format"]["type"] == "json_schema"
    assert route.calls.last.request.headers["Authorization"] == "Bearer gk"


@respx.mock
async def test_falls_back_to_json_object_when_schema_rejected() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(400, text='{"error": "response_format json_schema not supported"}'),
            _completion(GOOD),
        ]
    )
    llm = _llm()
    assert (await llm.generate(system="s", prompt="p", schema=Answer)).title == "t"
    assert json.loads(route.calls[1].request.content)["response_format"] == {"type": "json_object"}


@respx.mock
async def test_repairs_invalid_output_once() -> None:
    route = respx.post(URL).mock(side_effect=[_completion('{"title": 1}'), _completion(GOOD)])
    assert (await _llm().generate(system="s", prompt="p", schema=Answer)).points == ["a"]
    repair_messages = json.loads(route.calls[1].request.content)["messages"]
    assert repair_messages[-2]["role"] == "assistant" and "did not match" in repair_messages[-1]["content"]


@respx.mock
async def test_gives_up_after_one_repair() -> None:
    respx.post(URL).mock(side_effect=[_completion("not json"), _completion("{}")])
    with pytest.raises(ProviderError) as exc:
        await _llm().generate(system="s", prompt="p", schema=Answer)
    assert not exc.value.retryable


@respx.mock
async def test_rate_limit_retryable_and_truncation_not() -> None:
    respx.post(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(ProviderError) as exc:
        await _llm().generate(system="s", prompt="p", schema=Answer)
    assert exc.value.retryable

    respx.post(URL).mock(return_value=_completion('{"title":', "length"))
    with pytest.raises(ProviderError) as exc:
        await _llm().generate(system="s", prompt="p", schema=Answer)
    assert not exc.value.retryable


async def test_stub_builds_valid_domain_schemas() -> None:
    stub = StubLanguageModel()
    notes = await stub.generate(system="s", prompt="p", schema=EnhancedNotes)
    assert isinstance(notes, EnhancedNotes) and notes.sections == []
    suggestion = await stub.generate(system="s", prompt="p", schema=SpeakerSuggestion)
    assert 0 <= suggestion.confidence <= 1


def test_factory_selects_adapter() -> None:
    assert isinstance(build_language_model(Settings(llm_provider="fake")), StubLanguageModel)
    assert (
        build_language_model(Settings(llm_provider="groq", groq_api_key=SecretStr("k"))).name
        == "groq:openai/gpt-oss-120b"
    )
    llm = build_language_model(
        Settings(llm_provider="anthropic", anthropic_api_key=SecretStr("k"), llm_model="claude-haiku-5-5")
    )
    assert llm.name == "anthropic:claude-haiku-5-5"
