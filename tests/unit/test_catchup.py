import uuid

import pytest

from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import TemplateKind, TranscriptSegment
from app.services.catchup import CHARS_PER_MINUTE, CatchUp, CatchupService
from tests.fakes import FakeLlm, InMemoryUnitOfWorkFactory

USER = uuid.uuid4()


async def _setup(segments: list[TranscriptSegment], response: CatchUp | None = None):
    uow = InMemoryUnitOfWorkFactory()
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=USER, title="Standup", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
        await u.transcripts.replace(meeting_id=meeting.id, segments=segments)
    llm = FakeLlm([response or CatchUp(summary="s", points=[str(i) for i in range(8)])])
    return CatchupService(uow, llm), llm, meeting


def _seg(minute: int, text: str) -> TranscriptSegment:
    return TranscriptSegment(speaker=0, start_ms=minute * 60_000, end_ms=minute * 60_000 + 30_000, text=text)


async def test_stored_transcript_uses_only_the_last_minutes_and_caps_points() -> None:
    service, llm, meeting = await _setup([_seg(0, "old topic"), _seg(9, "recent topic")])

    result = await service.catch_up(user_id=USER, meeting_id=meeting.id, minutes=2)

    prompt = llm.prompts[0][1]
    assert "recent topic" in prompt and "old topic" not in prompt
    assert len(result.points) == 5


async def test_live_transcript_tail_wins_over_stored() -> None:
    service, llm, meeting = await _setup([_seg(0, "stored")])
    live = "x" * (CHARS_PER_MINUTE * 3) + "latest words"

    await service.catch_up(user_id=USER, meeting_id=meeting.id, minutes=1, live_transcript=live)

    prompt = llm.prompts[0][1]
    assert "latest words" in prompt and "stored" not in prompt
    assert prompt.count("x") <= CHARS_PER_MINUTE


async def test_nothing_transcribed_is_invalid() -> None:
    service, _, meeting = await _setup([])
    with pytest.raises(InvalidInputError):
        await service.catch_up(user_id=USER, meeting_id=meeting.id, minutes=5)


async def test_other_users_meeting_is_not_found() -> None:
    service, _, meeting = await _setup([_seg(0, "x")])
    with pytest.raises(NotFoundError):
        await service.catch_up(user_id=uuid.uuid4(), meeting_id=meeting.id, minutes=5)
