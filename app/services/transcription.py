"""Turns a meeting's uploaded audio chunks into a stored, diarized transcript, then deletes the audio."""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

import structlog

from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import ChunkStatus, Meeting, MeetingStatus, NoteLine, Transcript
from app.ports.providers import AudioAssembler, ObjectStorage, SpeechToText, TranscriptionOptions
from app.ports.repositories import UnitOfWorkFactory

log = structlog.get_logger()

MAX_KEYTERMS = 50
_STOPWORDS = frozenset(
    [
        "about",
        "after",
        "again",
        "also",
        "and",
        "between",
        "call",
        "check",
        "from",
        "have",
        "into",
        "meeting",
        "next",
        "review",
        "sync",
        "that",
        "their",
        "there",
        "this",
        "weekly",
        "with",
        "your",
    ]
)


# A token from the user's notes that looks like jargon: an acronym, mixed case, digits or joining punctuation
# ("eVTOL", "PT-01", "LR(1)", "FIRST/FOLLOW"). Plain lowercase words are too common to be worth boosting.
_NOTE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9()+#./-]*[A-Za-z0-9)+#]")


def _is_jargon(token: str) -> bool:
    return (
        (any(c.isdigit() for c in token) and any(c.isalpha() for c in token))
        or any(c.isupper() for c in token[1:])
        or any(c in "()+#/-" for c in token)
    )


def build_keyterms(meeting: Meeting, notes: Sequence[NoteLine] = ()) -> list[str]:
    """Terms the recognizer should favour: attendee names, then jargon the user typed, then title words.

    What the user types while listening is the best evidence of the vocabulary in the room, so a misheard term
    ("EV toll" for "eVTOL") is far less likely once the user has written it down once.
    """
    terms: list[str] = []
    for attendee in meeting.attendees:
        if attendee.name:
            terms.append(attendee.name.strip())
    for line in sorted(notes, key=lambda n: n.position):
        terms.extend(t for t in _NOTE_TOKEN.findall(line.text) if _is_jargon(t))
    for word in re.findall(r"[A-Za-z][A-Za-z0-9&+.-]{3,}", meeting.title):
        if word.lower() not in _STOPWORDS:
            terms.append(word)
    seen: set[str] = set()
    unique = [t for t in terms if t and not (t.lower() in seen or seen.add(t.lower()))]
    return unique[:MAX_KEYTERMS]


class TranscriptionService:
    def __init__(
        self, uow: UnitOfWorkFactory, storage: ObjectStorage, audio: AudioAssembler, stt: SpeechToText
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._audio = audio
        self._stt = stt

    async def transcribe(self, *, user_id: UUID, meeting_id: UUID) -> Transcript:
        async with self._uow() as uow:
            meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            chunks = [c for c in await uow.chunks.list(meeting_id=meeting_id) if c.status == ChunkStatus.UPLOADED]
            if not chunks:
                raise InvalidInputError("Meeting has no uploaded audio")
            notes = await uow.notes.list(meeting_id=meeting_id)
            await uow.meetings.set_status(meeting_id=meeting_id, status=MeetingStatus.PROCESSING)
            await uow.commit()

        # Provider work runs outside any transaction: downloads and STT can take minutes.
        parts = [await self._storage.download(c.storage_path) for c in chunks]
        audio, mime_type = await self._audio.concat(parts, chunks[0].mime_type)
        options = TranscriptionOptions(diarize=True, keyterms=build_keyterms(meeting, notes))
        transcript = await self._stt.transcribe(audio, mime_type, options)
        log.info(
            "transcription.completed",
            meeting_id=str(meeting_id),
            provider=transcript.provider,
            segments=len(transcript.segments),
            chunks=len(chunks),
            languages=transcript.languages,
        )

        async with self._uow() as uow:
            await uow.transcripts.replace(meeting_id=meeting_id, segments=transcript.segments)
            await uow.meetings.set_languages(meeting_id=meeting_id, languages=transcript.languages)
            await uow.commit()

        # Audio is deleted only once the transcript is durably saved. A failed delete leaves orphaned objects,
        # which is a cleanup problem, not a reason to redo a transcription that already succeeded.
        try:
            await self._storage.delete([c.storage_path for c in chunks])
        except Exception as exc:
            log.warning("transcription.audio_delete_failed", meeting_id=str(meeting_id), error=str(exc))
        else:
            async with self._uow() as uow:
                await uow.chunks.mark_deleted(meeting_id=meeting_id)
                await uow.commit()
        return transcript
