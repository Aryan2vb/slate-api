"""SQLAlchemy implementations of the repository ports. Each repository works inside the session its UnitOfWork owns
and never commits; transaction boundaries belong to the caller."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.db import mappers
from app.adapters.db.tables import (
    AudioChunkRow,
    EnhancedNotesRow,
    JobRow,
    MeetingRow,
    NoteLineRow,
    SpeakerRow,
    TranscriptSegmentRow,
)
from app.domain.models import (
    Attendee,
    AudioChunk,
    ChunkStatus,
    EnhancedNotes,
    Job,
    JobKind,
    JobStatus,
    Meeting,
    MeetingStatus,
    NoteLine,
    Speaker,
    TemplateKind,
    TranscriptSegment,
)
from app.ports.repositories import NoteLineInput


def _rowcount(result: Any) -> int:
    return cast(CursorResult[Any], result).rowcount


class SqlMeetingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        template: TemplateKind,
        calendar_event_id: str | None,
        attendees: Sequence[Attendee],
    ) -> Meeting:
        row = MeetingRow(
            user_id=user_id,
            title=title,
            template=template.value,
            status=MeetingStatus.DRAFT.value,
            calendar_event_id=calendar_event_id,
            attendees=[a.model_dump(mode="json") for a in attendees],
        )
        self._s.add(row)
        await self._s.flush()
        await self._s.refresh(row)
        return mappers.meeting(row)

    async def get(self, *, user_id: UUID, meeting_id: UUID) -> Meeting | None:
        row = await self._s.scalar(select(MeetingRow).where(MeetingRow.id == meeting_id, MeetingRow.user_id == user_id))
        return mappers.meeting(row) if row else None

    async def get_by_calendar_event(self, *, user_id: UUID, calendar_event_id: str) -> Meeting | None:
        row = await self._s.scalar(
            select(MeetingRow).where(MeetingRow.user_id == user_id, MeetingRow.calendar_event_id == calendar_event_id)
        )
        return mappers.meeting(row) if row else None

    async def list(self, *, user_id: UUID, limit: int, before: datetime | None) -> list[Meeting]:
        stmt = select(MeetingRow).where(MeetingRow.user_id == user_id)
        if before is not None:
            stmt = stmt.where(MeetingRow.created_at < before)
        rows = await self._s.scalars(stmt.order_by(MeetingRow.created_at.desc()).limit(limit))
        return [mappers.meeting(r) for r in rows]

    async def set_status(self, *, meeting_id: UUID, status: MeetingStatus) -> None:
        await self._s.execute(update(MeetingRow).where(MeetingRow.id == meeting_id).values(status=status.value))

    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> bool:
        result = await self._s.execute(
            delete(MeetingRow).where(MeetingRow.id == meeting_id, MeetingRow.user_id == user_id)
        )
        return _rowcount(result) > 0


class SqlNoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list(self, *, meeting_id: UUID) -> list[NoteLine]:
        rows = await self._s.scalars(
            select(NoteLineRow).where(NoteLineRow.meeting_id == meeting_id).order_by(NoteLineRow.position)
        )
        return [mappers.note_line(r) for r in rows]

    async def replace_all(self, *, meeting_id: UUID, lines: Sequence[NoteLineInput]) -> list[NoteLine]:
        ids = [line.id for line in lines]
        await self._s.execute(
            delete(NoteLineRow).where(NoteLineRow.meeting_id == meeting_id, NoteLineRow.id.not_in(ids))
        )
        if lines:
            stmt = insert(NoteLineRow).values(
                [
                    {"id": x.id, "meeting_id": meeting_id, "position": x.position, "text": x.text, "t_ms": x.t_ms}
                    for x in lines
                ]
            )
            # A client-chosen id that already belongs to another meeting is left untouched rather than hijacked.
            stmt = stmt.on_conflict_do_update(
                index_elements=[NoteLineRow.id],
                set_={
                    "position": stmt.excluded.position,
                    "text": stmt.excluded.text,
                    "t_ms": stmt.excluded.t_ms,
                    "updated_at": func.now(),
                },
                where=NoteLineRow.meeting_id == stmt.excluded.meeting_id,
            )
            await self._s.execute(stmt)
        return await self.list(meeting_id=meeting_id)


class SqlChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self, *, meeting_id: UUID, seq: int, storage_path: str, mime_type: str, byte_size: int | None
    ) -> AudioChunk:
        row = AudioChunkRow(
            meeting_id=meeting_id,
            seq=seq,
            storage_path=storage_path,
            mime_type=mime_type,
            byte_size=byte_size,
            status=ChunkStatus.PENDING.value,
        )
        self._s.add(row)
        await self._s.flush()
        await self._s.refresh(row)
        return mappers.audio_chunk(row)

    async def mark_uploaded(self, *, meeting_id: UUID, chunk_id: UUID) -> AudioChunk | None:
        row = await self._s.scalar(
            update(AudioChunkRow)
            .where(AudioChunkRow.id == chunk_id, AudioChunkRow.meeting_id == meeting_id)
            .values(status=ChunkStatus.UPLOADED.value)
            .returning(AudioChunkRow)
        )
        return mappers.audio_chunk(row) if row else None

    async def list(self, *, meeting_id: UUID) -> list[AudioChunk]:
        rows = await self._s.scalars(
            select(AudioChunkRow).where(AudioChunkRow.meeting_id == meeting_id).order_by(AudioChunkRow.seq)
        )
        return [mappers.audio_chunk(r) for r in rows]

    async def mark_deleted(self, *, meeting_id: UUID) -> None:
        await self._s.execute(
            update(AudioChunkRow).where(AudioChunkRow.meeting_id == meeting_id).values(status=ChunkStatus.DELETED.value)
        )


class SqlTranscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def replace(self, *, meeting_id: UUID, segments: Sequence[TranscriptSegment]) -> None:
        await self._s.execute(delete(TranscriptSegmentRow).where(TranscriptSegmentRow.meeting_id == meeting_id))
        if segments:
            await self._s.execute(
                insert(TranscriptSegmentRow).values(
                    [{"meeting_id": meeting_id, **seg.model_dump()} for seg in segments]
                )
            )

    async def list(self, *, meeting_id: UUID) -> list[TranscriptSegment]:
        rows = await self._s.scalars(
            select(TranscriptSegmentRow)
            .where(TranscriptSegmentRow.meeting_id == meeting_id)
            .order_by(TranscriptSegmentRow.start_ms)
        )
        return [mappers.transcript_segment(r) for r in rows]


class SqlSpeakerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list(self, *, meeting_id: UUID) -> list[Speaker]:
        rows = await self._s.scalars(
            select(SpeakerRow).where(SpeakerRow.meeting_id == meeting_id).order_by(SpeakerRow.label)
        )
        return [mappers.speaker(r) for r in rows]

    async def upsert(self, speaker: Speaker) -> Speaker:
        values = speaker.model_dump()
        stmt = insert(SpeakerRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SpeakerRow.meeting_id, SpeakerRow.label],
            set_={k: stmt.excluded[k] for k in ("name", "attendee_email", "confirmed")},
        ).returning(SpeakerRow)
        row = (await self._s.scalars(stmt)).one()
        return mappers.speaker(row)


class SqlEnhancedNotesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def save(self, *, meeting_id: UUID, notes: EnhancedNotes, model: str) -> None:
        stmt = insert(EnhancedNotesRow).values(
            meeting_id=meeting_id, content=notes.model_dump(mode="json"), model=model
        )
        await self._s.execute(
            stmt.on_conflict_do_update(
                index_elements=[EnhancedNotesRow.meeting_id],
                set_={"content": stmt.excluded.content, "model": stmt.excluded.model, "updated_at": func.now()},
            )
        )

    async def get(self, *, meeting_id: UUID) -> EnhancedNotes | None:
        row = await self._s.get(EnhancedNotesRow, meeting_id)
        return mappers.enhanced_notes(row) if row else None


_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
_FINISHED = (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value)


class SqlJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def enqueue(self, *, meeting_id: UUID, user_id: UUID, kind: JobKind, max_attempts: int = 5) -> Job:
        stmt = insert(JobRow).values(
            meeting_id=meeting_id,
            user_id=user_id,
            kind=kind.value,
            status=JobStatus.QUEUED.value,
            attempts=0,
            max_attempts=max_attempts,
        )
        # A finished job is reset in place (same id); a queued or running one is left alone, so retries of the
        # enqueue call never duplicate or interrupt work.
        stmt = stmt.on_conflict_do_update(
            index_elements=[JobRow.meeting_id, JobRow.kind],
            set_={
                "status": JobStatus.QUEUED.value,
                "attempts": 0,
                "max_attempts": stmt.excluded.max_attempts,
                "run_after": func.now(),
                "locked_at": None,
                "last_error": None,
                "updated_at": func.now(),
            },
            where=JobRow.status.in_(_FINISHED),
        ).returning(JobRow)
        row = (await self._s.scalars(stmt)).one_or_none()
        if row is None:
            row = (
                await self._s.scalars(select(JobRow).where(JobRow.meeting_id == meeting_id, JobRow.kind == kind.value))
            ).one()
        return mappers.job(row)

    async def claim_next(self, *, now: datetime) -> Job | None:
        row = await self._s.scalar(
            select(JobRow)
            .where(JobRow.status == JobStatus.QUEUED.value, JobRow.run_after <= now)
            .order_by(JobRow.run_after)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status = JobStatus.RUNNING.value
        row.attempts += 1
        row.locked_at = now
        await self._s.flush()
        await self._s.refresh(row)
        return mappers.job(row)

    async def mark_succeeded(self, *, job_id: UUID) -> None:
        await self._s.execute(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(status=JobStatus.SUCCEEDED.value, locked_at=None, last_error=None)
        )

    async def mark_failed(self, *, job_id: UUID, error: str, retry_at: datetime | None) -> None:
        values: dict[str, Any] = {"last_error": error, "locked_at": None}
        if retry_at is None:
            values["status"] = JobStatus.FAILED.value
        else:
            values |= {"status": JobStatus.QUEUED.value, "run_after": retry_at}
        await self._s.execute(update(JobRow).where(JobRow.id == job_id).values(**values))

    async def reclaim_stale(self, *, locked_before: datetime) -> int:
        result = await self._s.execute(
            update(JobRow)
            .where(JobRow.status == JobStatus.RUNNING.value, JobRow.locked_at < locked_before)
            .values(status=JobStatus.QUEUED.value, locked_at=None)
        )
        return _rowcount(result)

    async def list_for_meeting(self, *, meeting_id: UUID) -> list[Job]:
        rows = await self._s.scalars(select(JobRow).where(JobRow.meeting_id == meeting_id).order_by(JobRow.created_at))
        return [mappers.job(r) for r in rows]
