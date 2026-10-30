"""'What did I miss?': a short summary of the last few minutes of a meeting, live or recorded."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.errors import InvalidInputError, NotFoundError
from app.ports.providers import LanguageModel
from app.ports.repositories import UnitOfWorkFactory
from app.services.prompts import CATCHUP_SYSTEM, catchup_prompt, format_transcript

# Conversational speech runs ~150 words a minute at ~6 characters a word. The on-device live transcript carries no
# timestamps, so its tail is cut by length instead of time.
CHARS_PER_MINUTE = 900
MAX_POINTS = 5


class CatchUp(BaseModel):
    summary: str
    points: list[str] = Field(default_factory=list)


class CatchupService:
    def __init__(self, uow: UnitOfWorkFactory, llm: LanguageModel) -> None:
        self._uow = uow
        self._llm = llm

    async def catch_up(
        self, *, user_id: UUID, meeting_id: UUID, minutes: int, live_transcript: str | None = None
    ) -> CatchUp:
        async with self._uow() as uow:
            meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            segments = [] if live_transcript else await uow.transcripts.list(meeting_id=meeting_id)

        if live_transcript and live_transcript.strip():
            excerpt = live_transcript.strip()[-minutes * CHARS_PER_MINUTE :]
        else:
            end = max((s.end_ms for s in segments), default=0)
            excerpt = format_transcript([s for s in segments if s.end_ms >= end - minutes * 60_000])
        if not excerpt.strip():
            raise InvalidInputError("Nothing has been transcribed yet")

        result = await self._llm.generate(
            system=CATCHUP_SYSTEM,
            prompt=catchup_prompt(title=meeting.title, minutes=minutes, excerpt=excerpt),
            schema=CatchUp,
            max_tokens=1000,
        )
        return result.model_copy(update={"points": result.points[:MAX_POINTS]})
