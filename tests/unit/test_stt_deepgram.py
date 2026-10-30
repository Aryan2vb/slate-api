import httpx
import pytest
import respx

from app.adapters.stt.deepgram import DEEPGRAM_URL, DeepgramSpeechToText, normalize_keyterms
from app.domain.errors import ProviderError
from app.ports.providers import TranscriptionOptions

UTTERANCES_BODY = {
    "metadata": {"duration": 9.5},
    "results": {
        "channels": [{"detected_language": "en", "alternatives": [{"words": []}]}],
        "utterances": [
            {"start": 0.0, "end": 4.0, "speaker": 0, "transcript": "Hi, I'm Priya."},
            {"start": 4.0, "end": 9.5, "speaker": 1, "transcript": "Q3 pricing goes up."},
            {"start": 9.5, "end": 9.5, "speaker": 1, "transcript": "  "},
        ],
    },
}


def _stt() -> DeepgramSpeechToText:
    return DeepgramSpeechToText("dg-key", client=httpx.AsyncClient())


def test_normalize_keyterms_trims_dedupes_and_caps() -> None:
    assert normalize_keyterms(["  Kubernetes ", "kubernetes", "", "Priya  Shah", "x" * 101]) == [
        "Kubernetes",
        "Priya Shah",
    ]
    assert len(normalize_keyterms([f"term{i}" for i in range(80)])) == 50


@respx.mock
async def test_maps_utterances_and_sends_params() -> None:
    route = respx.post(DEEPGRAM_URL).mock(return_value=httpx.Response(200, json=UTTERANCES_BODY))
    t = await _stt().transcribe(b"audio", "audio/mp4", TranscriptionOptions(keyterms=["Slate", "Priya"]))

    assert [(s.speaker, s.start_ms, s.end_ms, s.text) for s in t.segments] == [
        (0, 0, 4000, "Hi, I'm Priya."),
        (1, 4000, 9500, "Q3 pricing goes up."),
    ]
    assert t.duration_ms == 9500 and t.language == "en" and t.provider == "deepgram"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Token dg-key"
    assert request.headers["Content-Type"] == "audio/mp4"
    params = request.url.params
    assert params["model"] == "nova-3" and params["diarize"] == "true" and params["utterances"] == "true"
    assert params.get_list("keyterm") == ["Slate", "Priya"]


@respx.mock
async def test_falls_back_to_words_when_no_utterances() -> None:
    words = [
        {"word": "hi", "punctuated_word": "Hi,", "start": 0.0, "end": 0.5, "speaker": 0},
        {"word": "there", "punctuated_word": "there.", "start": 0.5, "end": 1.0, "speaker": 0},
        {"word": "yes", "punctuated_word": "Yes.", "start": 1.2, "end": 1.6, "speaker": 1},
    ]
    body = {"results": {"channels": [{"alternatives": [{"words": words}]}]}}
    respx.post(DEEPGRAM_URL).mock(return_value=httpx.Response(200, json=body))
    t = await _stt().transcribe(b"audio", "audio/wav", TranscriptionOptions())
    assert [(s.speaker, s.start_ms, s.end_ms, s.text) for s in t.segments] == [
        (0, 0, 1000, "Hi, there."),
        (1, 1200, 1600, "Yes."),
    ]


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (400, False), (401, False)])
async def test_http_errors_classified(status: int, retryable: bool) -> None:
    respx.post(DEEPGRAM_URL).mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(ProviderError) as exc:
        await _stt().transcribe(b"audio", "audio/wav", TranscriptionOptions())
    assert exc.value.retryable is retryable


@respx.mock
async def test_timeout_is_retryable() -> None:
    respx.post(DEEPGRAM_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        await _stt().transcribe(b"audio", "audio/wav", TranscriptionOptions())
    assert exc.value.retryable


async def test_empty_audio_rejected() -> None:
    with pytest.raises(ProviderError) as exc:
        await _stt().transcribe(b"", "audio/wav", TranscriptionOptions())
    assert not exc.value.retryable
