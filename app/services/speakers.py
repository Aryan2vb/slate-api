"""Suggests names for diarized speaker labels, and records the user's confirmations."""

from __future__ import annotations

from uuid import UUID

import structlog
from pydantic import BaseModel

from app.domain.errors import NotFoundError
from app.domain.models import Speaker, SpeakerSuggestion
from app.ports.providers import LanguageModel
from app.ports.repositories import UnitOfWorkFactory
from app.services.prompts import SPEAKERS_SYSTEM, format_attendees, format_transcript, speakers_prompt

log = structlog.get_logger()

MIN_CONFIDENCE = 0.6
# Introductions happen early; the opening of the meeting carries most of the naming evidence.
MAX_TRANSCRIPT_CHARS = 30_000


class SpeakerSuggestions(BaseModel):
    suggestions: list[SpeakerSuggestion]


class SpeakerService:
    def __init__(self, uow: UnitOfWorkFactory, llm: LanguageModel) -> None:
        self._uow = uow
        self._llm = llm

    async def suggest(self, *, user_id: UUID, meeting_id: UUID) -> list[Speaker]:
        """Stores confident suggestions as unconfirmed speakers. Confirmed speakers are never overwritten."""
        async with self._uow() as uow:
            meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            segments = await uow.transcripts.list(meeting_id=meeting_id)
            existing = {s.label: s for s in await uow.speakers.list(meeting_id=meeting_id)}

        labels = sorted({s.speaker for s in segments} - {label for label, s in existing.items() if s.confirmed})
        if not labels:
            return []

        result = await self._llm.generate(
            system=SPEAKERS_SYSTEM,
            prompt=speakers_prompt(
                attendees=format_attendees(meeting.attendees),
                transcript=format_transcript(segments)[:MAX_TRANSCRIPT_CHARS],
                labels=labels,
            ),
            schema=SpeakerSuggestions,
            max_tokens=2000,
        )

        accepted: dict[int, SpeakerSuggestion] = {}
        for s in result.suggestions:
            usable = s.label in labels and s.confidence >= MIN_CONFIDENCE and s.name.strip()
            if usable and (s.label not in accepted or s.confidence > accepted[s.label].confidence):
                accepted[s.label] = s

        saved: list[Speaker] = []
        async with self._uow() as uow:
            # Re-read inside the write so a confirmation made while the model was running still wins.
            current = {s.label: s for s in await uow.speakers.list(meeting_id=meeting_id)}
            for label, s in accepted.items():
                if current.get(label) and current[label].confirmed:
                    continue
                saved.append(
                    await uow.speakers.upsert(
                        Speaker(
                            meeting_id=meeting_id,
                            label=label,
                            name=s.name.strip(),
                            attendee_email=s.attendee_email,
                            confirmed=False,
                        )
                    )
                )
            await uow.commit()
        log.info("speakers.suggested", meeting_id=str(meeting_id), labels=len(labels), saved=len(saved))
        return saved

    async def confirm(
        self, *, user_id: UUID, meeting_id: UUID, label: int, name: str, attendee_email: str | None
    ) -> Speaker:
        async with self._uow() as uow:
            if await uow.meetings.get(user_id=user_id, meeting_id=meeting_id) is None:
                raise NotFoundError("Meeting not found")
            speaker = await uow.speakers.upsert(
                Speaker(
                    meeting_id=meeting_id, label=label, name=name.strip(), attendee_email=attendee_email, confirmed=True
                )
            )
            await uow.commit()
            return speaker
