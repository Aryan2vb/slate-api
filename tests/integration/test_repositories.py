import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory
from app.domain.models import (
    Attendee,
    EnhancedNotes,
    JobKind,
    JobStatus,
    Meeting,
    NoteBlock,
    Section,
    Speaker,
    TemplateKind,
    TranscriptSegment,
)

pytestmark = pytest.mark.db

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


@dataclass
class Line:
    id: UUID
    position: int
    text: str
    t_ms: int | None = None


def now() -> datetime:
    return datetime.now(UTC)


async def make_meeting(uow_factory: SqlAlchemyUnitOfWorkFactory, user_id: UUID = USER_A) -> Meeting:
    async with uow_factory() as uow:
        meeting = await uow.meetings.create(
            user_id=user_id,
            title="Pricing review",
            template=TemplateKind.SALES,
            calendar_event_id=None,
            attendees=[Attendee(email="priya@example.com", name="Priya")],
        )
        await uow.commit()
    return meeting


async def test_meeting_round_trip_and_ownership(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    async with uow_factory() as uow:
        own = await uow.meetings.get(user_id=USER_A, meeting_id=meeting.id)
        other = await uow.meetings.get(user_id=USER_B, meeting_id=meeting.id)
        assert own is not None and own == meeting
        assert own.template is TemplateKind.SALES
        assert own.attendees == [Attendee(email="priya@example.com", name="Priya")]
        assert other is None
        assert await uow.meetings.list(user_id=USER_B, limit=10, before=None) == []
        assert await uow.meetings.delete(user_id=USER_B, meeting_id=meeting.id) is False
        assert await uow.meetings.delete(user_id=USER_A, meeting_id=meeting.id) is True


async def test_get_by_calendar_event_is_scoped_to_owner(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        meeting = await uow.meetings.create(
            user_id=USER_A, title="Standup", template=TemplateKind.GENERAL, calendar_event_id="evt-1", attendees=[]
        )
        # The same calendar event id may exist for another user (unique per user, not globally).
        other = await uow.meetings.create(
            user_id=USER_B, title="Standup", template=TemplateKind.GENERAL, calendar_event_id="evt-1", attendees=[]
        )
        await uow.commit()
    async with uow_factory() as uow:
        found = await uow.meetings.get_by_calendar_event(user_id=USER_A, calendar_event_id="evt-1")
        assert found is not None and found.id == meeting.id
        theirs = await uow.meetings.get_by_calendar_event(user_id=USER_B, calendar_event_id="evt-1")
        assert theirs is not None and theirs.id == other.id
        assert await uow.meetings.get_by_calendar_event(user_id=USER_A, calendar_event_id="evt-2") is None


async def test_uncommitted_work_rolls_back(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        meeting = await uow.meetings.create(
            user_id=USER_A, title="Draft", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
    async with uow_factory() as uow:
        assert await uow.meetings.get(user_id=USER_A, meeting_id=meeting.id) is None


async def test_meeting_list_pages_newest_first(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    first = await make_meeting(uow_factory)
    second = await make_meeting(uow_factory)
    async with uow_factory() as uow:
        page = await uow.meetings.list(user_id=USER_A, limit=10, before=None)
        assert [m.id for m in page] == [second.id, first.id]
        older = await uow.meetings.list(user_id=USER_A, limit=10, before=second.created_at)
        assert [m.id for m in older] == [first.id]


async def test_notes_replace_all_is_idempotent_and_drops_missing_lines(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    meeting = await make_meeting(uow_factory)
    a, b = uuid.uuid4(), uuid.uuid4()
    lines = [Line(a, 0, "pricing up Q3", 1200), Line(b, 1, "ask about churn")]
    async with uow_factory() as uow:
        first = await uow.notes.replace_all(meeting_id=meeting.id, lines=lines)
        second = await uow.notes.replace_all(meeting_id=meeting.id, lines=lines)
        await uow.commit()
    assert [(n.id, n.text, n.t_ms) for n in first] == [(a, "pricing up Q3", 1200), (b, "ask about churn", None)]
    assert [(n.id, n.text) for n in second] == [(n.id, n.text) for n in first]

    async with uow_factory() as uow:
        result = await uow.notes.replace_all(meeting_id=meeting.id, lines=[Line(b, 0, "ask about churn rate")])
        await uow.commit()
    assert [(n.id, n.position, n.text) for n in result] == [(b, 0, "ask about churn rate")]

    async with uow_factory() as uow:
        assert await uow.notes.replace_all(meeting_id=meeting.id, lines=[]) == []


async def test_note_ids_from_another_meeting_are_not_hijacked(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    mine = await make_meeting(uow_factory)
    theirs = await make_meeting(uow_factory, USER_B)
    shared_id = uuid.uuid4()
    async with uow_factory() as uow:
        await uow.notes.replace_all(meeting_id=theirs.id, lines=[Line(shared_id, 0, "theirs")])
        await uow.notes.replace_all(meeting_id=mine.id, lines=[Line(shared_id, 0, "mine")])
        await uow.commit()
    async with uow_factory() as uow:
        assert [n.text for n in await uow.notes.list(meeting_id=theirs.id)] == ["theirs"]
        assert await uow.notes.list(meeting_id=mine.id) == []


async def test_chunks_transcript_speakers_and_enhanced(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    notes = EnhancedNotes(
        title="Pricing",
        tldr="Prices rise 5% in Q3.",
        sections=[Section(heading="Pricing", blocks=[NoteBlock(source="user", text="pricing up Q3")])],
    )
    async with uow_factory() as uow:
        chunk = await uow.chunks.add(
            meeting_id=meeting.id,
            seq=0,
            storage_path=f"{USER_A}/{meeting.id}/0.m4a",
            mime_type="audio/mp4",
            byte_size=10,
        )
        assert await uow.chunks.mark_uploaded(meeting_id=uuid.uuid4(), chunk_id=chunk.id) is None
        uploaded = await uow.chunks.mark_uploaded(meeting_id=meeting.id, chunk_id=chunk.id)
        assert uploaded is not None and uploaded.status == "uploaded"
        await uow.chunks.mark_deleted(meeting_id=meeting.id)
        assert [c.status for c in await uow.chunks.list(meeting_id=meeting.id)] == ["deleted"]

        segments = [
            TranscriptSegment(speaker=1, start_ms=4000, end_ms=9000, text="Q3 up 5%."),
            TranscriptSegment(speaker=0, start_ms=0, end_ms=4000, text="Hi, I'm Priya."),
        ]
        await uow.transcripts.replace(meeting_id=meeting.id, segments=segments)
        await uow.transcripts.replace(meeting_id=meeting.id, segments=segments)
        assert [s.start_ms for s in await uow.transcripts.list(meeting_id=meeting.id)] == [0, 4000]

        await uow.speakers.upsert(Speaker(meeting_id=meeting.id, label=0, name="Priya"))
        confirmed = await uow.speakers.upsert(Speaker(meeting_id=meeting.id, label=0, name="Priya S", confirmed=True))
        assert confirmed.confirmed and confirmed.name == "Priya S"
        assert len(await uow.speakers.list(meeting_id=meeting.id)) == 1

        assert await uow.enhanced.get(meeting_id=meeting.id) is None
        await uow.enhanced.save(meeting_id=meeting.id, notes=notes, model="fake")
        await uow.enhanced.save(meeting_id=meeting.id, notes=notes.model_copy(update={"tldr": "v2"}), model="fake")
        saved = await uow.enhanced.get(meeting_id=meeting.id)
        assert saved is not None and saved.tldr == "v2" and saved.sections == notes.sections


async def test_enqueue_is_idempotent_and_resets_finished_jobs(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    async with uow_factory() as uow:
        job = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.TRANSCRIBE)
        again = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.TRANSCRIBE)
        assert again.id == job.id and again.status is JobStatus.QUEUED
        await uow.commit()

    async with uow_factory() as uow:
        claimed = await uow.jobs.claim_next(now=now())
        assert claimed is not None and claimed.id == job.id
        running = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.TRANSCRIBE)
        assert running.status is JobStatus.RUNNING and running.attempts == 1
        await uow.jobs.mark_failed(job_id=job.id, error="boom", retry_at=None)
        reset = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.TRANSCRIBE)
        assert (reset.id, reset.status, reset.attempts, reset.last_error) == (job.id, JobStatus.QUEUED, 0, None)
        assert len(await uow.jobs.list_for_meeting(meeting_id=meeting.id)) == 1


async def test_concurrent_claims_never_return_the_same_job(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    async with uow_factory() as uow:
        for kind in JobKind:
            await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=kind)
        await uow.commit()

    gate = asyncio.Event()

    async def claim() -> UUID | None:
        async with uow_factory() as uow:
            job = await uow.jobs.claim_next(now=now() + timedelta(seconds=1))
            await gate.wait()  # hold the row lock until every claimer has run its SELECT
            await uow.commit()
            return job.id if job else None

    tasks = [asyncio.create_task(claim()) for _ in range(5)]
    await asyncio.sleep(0.3)
    gate.set()
    claimed = [j for j in await asyncio.gather(*tasks) if j is not None]
    assert len(claimed) == len(JobKind)
    assert len(set(claimed)) == len(claimed)


async def test_claim_respects_run_after(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    async with uow_factory() as uow:
        job = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.ENHANCE)
        await uow.jobs.mark_failed(job_id=job.id, error="429", retry_at=now() + timedelta(minutes=5))
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.jobs.claim_next(now=now()) is None
        retried = await uow.jobs.claim_next(now=now() + timedelta(minutes=6))
        assert retried is not None and retried.id == job.id and retried.last_error == "429"


async def test_mark_failed_retry_vs_permanent_and_succeeded(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    async with uow_factory() as uow:
        retry = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.TRANSCRIBE)
        perm = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.SPEAKERS)
        ok = await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.ENHANCE)
        retry_at = now() + timedelta(seconds=30)
        await uow.jobs.mark_failed(job_id=retry.id, error="timeout", retry_at=retry_at)
        await uow.jobs.mark_failed(job_id=perm.id, error="bad audio", retry_at=None)
        await uow.jobs.mark_succeeded(job_id=ok.id)
        jobs = {j.id: j for j in await uow.jobs.list_for_meeting(meeting_id=meeting.id)}
    assert jobs[retry.id].status is JobStatus.QUEUED and jobs[retry.id].run_after == retry_at
    assert jobs[perm.id].status is JobStatus.FAILED and jobs[perm.id].last_error == "bad audio"
    assert jobs[ok.id].status is JobStatus.SUCCEEDED and jobs[ok.id].last_error is None


async def test_reclaim_stale_requeues_only_old_running_jobs(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    meeting = await make_meeting(uow_factory)
    t0 = now()
    async with uow_factory() as uow:
        await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.TRANSCRIBE)
        await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER_A, kind=JobKind.SPEAKERS)
        stale = await uow.jobs.claim_next(now=t0 - timedelta(hours=1) + timedelta(hours=2))
        assert stale is not None
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.jobs.reclaim_stale(locked_before=t0) == 0
        assert await uow.jobs.reclaim_stale(locked_before=t0 + timedelta(hours=2)) == 1
        reclaimed = next(j for j in await uow.jobs.list_for_meeting(meeting_id=meeting.id) if j.id == stale.id)
        assert reclaimed.status is JobStatus.QUEUED and reclaimed.locked_at is None and reclaimed.attempts == 1
