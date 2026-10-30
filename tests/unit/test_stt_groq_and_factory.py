import httpx
import pytest
import respx
from pydantic import SecretStr

from app.adapters.stt.factory import build_speech_to_text
from app.adapters.stt.groq_whisper import GROQ_TRANSCRIPTIONS_URL, MAX_UPLOAD_BYTES, GroqWhisperSpeechToText
from app.adapters.stt.stub import StubSpeechToText
from app.config import Settings
from app.domain.errors import ProviderError
from app.ports.providers import TranscriptionOptions


@respx.mock
async def test_groq_maps_segments_to_speaker_zero() -> None:
    body = {
        "language": "english",
        "duration": 3.0,
        "segments": [{"start": 0.0, "end": 1.5, "text": " Hello. "}, {"start": 1.5, "end": 3.0, "text": "World."}],
    }
    route = respx.post(GROQ_TRANSCRIPTIONS_URL).mock(return_value=httpx.Response(200, json=body))
    stt = GroqWhisperSpeechToText("gk", client=httpx.AsyncClient())
    t = await stt.transcribe(b"audio", "audio/mp4", TranscriptionOptions(keyterms=["Slate"]))
    assert [(s.speaker, s.start_ms, s.end_ms, s.text) for s in t.segments] == [
        (0, 0, 1500, "Hello."),
        (0, 1500, 3000, "World."),
    ]
    sent = route.calls.last.request.content
    assert b"whisper-large-v3-turbo" in sent and b"verbose_json" in sent and b'filename="audio.m4a"' in sent


async def test_groq_rejects_oversized_audio_without_calling() -> None:
    stt = GroqWhisperSpeechToText("gk", client=httpx.AsyncClient())
    with pytest.raises(ProviderError) as exc:
        await stt.transcribe(b"x" * (MAX_UPLOAD_BYTES + 1), "audio/wav", TranscriptionOptions())
    assert not exc.value.retryable


@respx.mock
async def test_groq_rate_limit_retryable() -> None:
    respx.post(GROQ_TRANSCRIPTIONS_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(ProviderError) as exc:
        await GroqWhisperSpeechToText("gk", client=httpx.AsyncClient()).transcribe(
            b"a", "audio/wav", TranscriptionOptions()
        )
    assert exc.value.retryable


def test_factory_selects_adapter() -> None:
    assert isinstance(build_speech_to_text(Settings(stt_provider="fake")), StubSpeechToText)
    assert build_speech_to_text(Settings(stt_provider="deepgram", deepgram_api_key=SecretStr("k"))).name == "deepgram"
    assert build_speech_to_text(Settings(stt_provider="groq", groq_api_key=SecretStr("k"))).name == "groq-whisper"
    with pytest.raises(ValueError):
        build_speech_to_text(Settings(stt_provider="deepgram", deepgram_api_key=SecretStr("")))


async def test_stub_is_deterministic() -> None:
    a = await StubSpeechToText().transcribe(b"abc", "audio/wav", TranscriptionOptions())
    b = await StubSpeechToText().transcribe(b"abc", "audio/wav", TranscriptionOptions())
    assert a == b and len(a.segments) == 2
