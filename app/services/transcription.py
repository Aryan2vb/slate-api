"""Turns a meeting's uploaded audio chunks into a stored, diarized transcript, then deletes the chunks.

With audio retention on, the assembled recording is kept (one object per meeting) for playback until it expires."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import ChunkStatus, Meeting, MeetingStatus, NoteLine, Transcript
from app.domain.vocabulary import dedupe_terms, jargon_terms
from app.ports.providers import AudioAssembler, ObjectStorage, SpeechToText, TranscriptionOptions
from app.ports.repositories import UnitOfWorkFactory
from app.services.audio import kept_audio_path

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


def build_keyterms(meeting: Meeting, notes: Sequence[NoteLine] = (), vocabulary: Sequence[str] = ()) -> list[str]:
    """Terms the recognizer should favour: attendee names, then the folder's (course's) vocabulary, then jargon the
    user typed, then title words.

    What the user types while listening is the best evidence of the vocabulary in the room, so a misheard term
    ("EV toll" for "eVTOL") is far less likely once the user has written it down once. A course's vocabulary is
    the same evidence curated across every lecture of that course.
    """
    terms: list[str] = []
    for attendee in meeting.attendees:
        if attendee.name:
            terms.append(attendee.name.strip())
    terms.extend(vocabulary)
    for line in sorted(notes, key=lambda n: n.position):
        terms.extend(jargon_terms(line.text))
    for word in re.findall(r"[A-Za-z][A-Za-z0-9&+.-]{3,}", meeting.title):
        if word.lower() not in _STOPWORDS:
            terms.append(word)
    return dedupe_terms(terms)[:MAX_KEYTERMS]


class TranscriptionService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        storage: ObjectStorage,
        audio: AudioAssembler,
        stt: SpeechToText,
        *,
        audio_retention_days: int = 0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._audio = audio
        self._stt = stt
        self._audio_retention_days = audio_retention_days  # 0: delete the recording as soon as it is transcribed
        self._clock = clock

    async def transcribe(self, *, user_id: UUID, meeting_id: UUID) -> Transcript:
        async with self._uow() as uow:
            meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            chunks = [c for c in await uow.chunks.list(meeting_id=meeting_id) if c.status == ChunkStatus.UPLOADED]
            if not chunks:
                raise InvalidInputError("Meeting has no uploaded audio")
            notes = await uow.notes.list(meeting_id=meeting_id)
            folder = await uow.folders.get(user_id=user_id, folder_id=meeting.folder_id) if meeting.folder_id else None
            await uow.meetings.set_status(meeting_id=meeting_id, status=MeetingStatus.PROCESSING)
            await uow.commit()

        # Provider work runs outside any transaction: downloads and STT can take minutes.
        parts = [await self._storage.download(c.storage_path) for c in chunks]
        audio, mime_type = await self._audio.concat(parts, chunks[0].mime_type)
        options = TranscriptionOptions(
            diarize=True,
            keyterms=build_keyterms(meeting, notes, folder.vocabulary if folder else ()),
        )
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

        if self._audio_retention_days > 0:
            await self._keep_audio(user_id, meeting_id, audio, mime_type, transcript)

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

    async def _keep_audio(
        self, user_id: UUID, meeting_id: UUID, audio: bytes, mime_type: str, transcript: Transcript
    ) -> None:
        """Stores the assembled recording for playback. Playback is a nicety: a failure here is logged, and the
        transcription still succeeds (the chunks are deleted as usual)."""
        path = kept_audio_path(user_id, meeting_id)
        expires_at = self._clock() + timedelta(days=self._audio_retention_days)
        duration_ms = transcript.duration_ms or max((s.end_ms for s in transcript.segments), default=None)
        try:
            await self._storage.upload(path, audio, mime_type)
            async with self._uow() as uow:
                still_there = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id) is not None
                if still_there:
                    await uow.meetings.set_audio(
                        meeting_id=meeting_id, path=path, expires_at=expires_at, duration_ms=duration_ms
                    )
                    await uow.commit()
            if not still_there:  # deleted while transcribing: nothing would ever clean this object up
                await self._storage.delete([path])
                return
        except Exception as exc:
            log.warning("transcription.audio_keep_failed", meeting_id=str(meeting_id), error=str(exc))
            return
        log.info(
            "transcription.audio_kept",
            meeting_id=str(meeting_id),
            bytes=len(audio),
            expires_at=expires_at.isoformat(),
        )
