"""Spoken-language detection flows from Deepgram's multilingual output into the meeting and the note prompts."""

import uuid

import httpx

from app.adapters.stt.deepgram import DeepgramSpeechToText, parse_response
from app.domain.models import (
    ChunkStatus,
    TemplateKind,
    Transcript,
    TranscriptSegment,
)
from app.ports.providers import TranscriptionOptions
from app.services.prompts import catchup_prompt, enhance_prompt, language_rule
from app.services.transcription import TranscriptionService
from tests.fakes import FakeAudio, FakeStorage, FakeStt, InMemoryUnitOfWorkFactory


def _word(text: str, lang: str, start: float) -> dict:
    return {"word": text, "punctuated_word": text, "start": start, "end": start + 0.3, "speaker": 0, "language": lang}


def test_languages_ordered_by_word_count_then_reported_tags() -> None:
    body = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "Pro plan की pricing",
                            "languages": ["hi", "en"],
                            "words": [_word("Pro", "en", 0), _word("plan", "en", 0.4), _word("की", "hi", 0.8)],
                        }
                    ]
                }
            ]
        }
    }
    t = parse_response(body, "deepgram")
    assert t.languages == ["en", "hi"]
    assert t.language == "en"


def test_detected_language_is_used_when_words_carry_none() -> None:
    body = {"results": {"channels": [{"detected_language": "ta", "alternatives": [{"words": []}]}]}}
    assert parse_response(body, "deepgram").languages == ["ta"]


def test_no_language_information_means_unknown() -> None:
    assert parse_response({"results": {"channels": [{"alternatives": [{"words": []}]}]}}, "deepgram").languages == []


def test_requests_multilingual_mode_by_default_and_honours_an_override() -> None:
    stt = DeepgramSpeechToText("k", client=httpx.AsyncClient())
    assert stt._params(TranscriptionOptions()).get("language") == "multi"
    assert stt._params(TranscriptionOptions(language="ta")).get("language") == "ta"
    pinned = DeepgramSpeechToText("k", language=None, client=httpx.AsyncClient())
    assert pinned._params(TranscriptionOptions()).get("language") is None


async def test_transcription_stores_detected_languages_on_the_meeting() -> None:
    uow = InMemoryUnitOfWorkFactory()
    storage = FakeStorage()
    user = uuid.uuid4()
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=user, title="Sync", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
        chunk = await u.chunks.add(
            meeting_id=meeting.id, seq=0, storage_path="p/0.m4a", mime_type="audio/mp4", byte_size=3
        )
        await u.chunks.mark_uploaded(meeting_id=meeting.id, chunk_id=chunk.id)
    storage.objects["p/0.m4a"] = b"aud"
    stt = FakeStt(
        Transcript(
            provider="fake",
            languages=["hi", "en"],
            segments=[TranscriptSegment(speaker=0, start_ms=0, end_ms=1000, text="आज का agenda simple है")],
        )
    )
    await TranscriptionService(uow, storage, FakeAudio(), stt).transcribe(user_id=user, meeting_id=meeting.id)
    async with uow() as u:
        stored = await u.meetings.get(user_id=user, meeting_id=meeting.id)
    assert stored is not None and stored.languages == ["hi", "en"]
    assert [c.status for c in await uow().chunks.list(meeting_id=meeting.id)] == [ChunkStatus.DELETED]


def test_prompts_ask_for_notes_in_the_spoken_language() -> None:
    rule = language_rule(["hi", "en"])
    assert "Hindi, English" in rule and "Hinglish" in rule and "Do not translate" in rule
    assert "detected" not in language_rule([])  # unknown: still mirror the transcript, no false claim
    prompt = enhance_prompt(
        title="t", template=TemplateKind.GENERAL, attendees="", notes="", transcript="x", languages=["hi"]
    )
    assert "Hindi" in prompt and "kept exactly as written" in prompt
    assert "Hindi" in catchup_prompt(title="t", minutes=5, excerpt="x", languages=["hi"])
