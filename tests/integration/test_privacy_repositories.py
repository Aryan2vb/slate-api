import uuid
from uuid import UUID

import pytest

from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory
from app.domain.models import EnhancedNotes, JobKind, Meeting, Speaker, TemplateKind, TranscriptSegment
from tests.integration.test_repositories import Line

pytestmark = pytest.mark.db

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


async def _meeting(f: SqlAlchemyUnitOfWorkFactory, user: UUID, title: str, folder_id: UUID | None = None) -> Meeting:
    async with f() as uow:
        m = await uow.meetings.create(
            user_id=user,
            title=title,
            template=TemplateKind.LECTURE,
            calendar_event_id=None,
            attendees=[],
            folder_id=folder_id,
        )
        await uow.notes.replace_all(meeting_id=m.id, lines=[Line(id=uuid.uuid4(), position=0, text=f"{title} DFA")])
        await uow.chunks.add(
            meeting_id=m.id, seq=0, storage_path=f"{user}/{m.id}/0.m4a", mime_type="audio/mp4", byte_size=1
        )
        await uow.transcripts.replace(
            meeting_id=m.id, segments=[TranscriptSegment(speaker=0, start_ms=0, end_ms=1, text="DFA talk")]
        )
        await uow.speakers.upsert(Speaker(meeting_id=m.id, label=0, name="P"))
        await uow.enhanced.save(meeting_id=m.id, notes=EnhancedNotes(title="t", tldr="t", sections=[]), model="x")
        await uow.jobs.enqueue(meeting_id=m.id, user_id=user, kind=JobKind.ENHANCE)
        await uow.commit()
    return m


async def test_list_all_delete_all_and_search_by_meeting(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        folder = await uow.folders.create(user_id=ALICE, name="Compilers", icon=None)
        await uow.folders.create(user_id=BOB, name="Theirs", icon=None)
        await uow.commit()
    first = await _meeting(uow_factory, ALICE, "First", folder.id)
    second = await _meeting(uow_factory, ALICE, "Second")
    bobs = await _meeting(uow_factory, BOB, "Bob")

    async with uow_factory() as uow:
        assert [m.id for m in await uow.meetings.list_all(user_id=ALICE)] == [first.id, second.id]
        hits = await uow.search.search(user_id=ALICE, query="DFA", folder_id=None, limit=20, meeting_id=second.id)
        assert hits and {h.meeting_id for h in hits} == {second.id}
        none = await uow.search.search(user_id=BOB, query="DFA", folder_id=None, limit=20, meeting_id=second.id)
        assert none == []

    async with uow_factory() as uow:
        assert await uow.meetings.delete_all(user_id=ALICE) == 2
        assert await uow.folders.delete_all(user_id=ALICE) == 1
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.meetings.list_all(user_id=ALICE) == []
        assert await uow.folders.list(user_id=ALICE) == []
        for m in (first, second):  # child rows went with the meetings (ON DELETE CASCADE)
            assert await uow.notes.list(meeting_id=m.id) == []
            assert await uow.chunks.list(meeting_id=m.id) == []
            assert await uow.transcripts.list(meeting_id=m.id) == []
            assert await uow.speakers.list(meeting_id=m.id) == []
            assert await uow.enhanced.get(meeting_id=m.id) is None
            assert await uow.jobs.list_for_meeting(meeting_id=m.id) == []
        assert [m.id for m in await uow.meetings.list_all(user_id=BOB)] == [bobs.id]
        assert len(await uow.folders.list(user_id=BOB)) == 1
        assert len(await uow.chunks.list(meeting_id=bobs.id)) == 1
