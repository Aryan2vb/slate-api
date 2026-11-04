"""SQLAlchemy implementations of the repository ports. Each repository works inside the session its UnitOfWork owns
and never commits; transaction boundaries belong to the caller."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    CursorResult,
    Float,
    Integer,
    String,
    case,
    column,
    delete,
    func,
    literal,
    literal_column,
    null,
    select,
    true,
    union_all,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.db import mappers
from app.adapters.db.tables import (
    SIMPLE_TS_CONFIG,
    AudioChunkRow,
    EnhancedNotesRow,
    FolderRow,
    JobRow,
    MeetingRow,
    NoteLineRow,
    ShareRow,
    SpeakerRow,
    TranscriptSegmentRow,
    simple_tsvector,
)
from app.domain.models import (
    Attendee,
    AudioChunk,
    ChunkStatus,
    EnhancedNotes,
    Folder,
    FolderSummary,
    Job,
    JobKind,
    JobStatus,
    Meeting,
    MeetingStatus,
    NoteLine,
    SearchHit,
    SearchKind,
    Share,
    Speaker,
    TemplateKind,
    TranscriptSegment,
)
from app.ports.repositories import NoteLineInput


def _rowcount(result: Any) -> int:
    return cast(CursorResult[Any], result).rowcount


class SqlFolderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, *, user_id: UUID, name: str, icon: str | None) -> Folder:
        row = FolderRow(user_id=user_id, name=name, icon=icon, vocabulary=[])
        self._s.add(row)
        await self._s.flush()
        await self._s.refresh(row)
        return mappers.folder(row)

    async def get(self, *, user_id: UUID, folder_id: UUID) -> Folder | None:
        row = await self._s.scalar(select(FolderRow).where(FolderRow.id == folder_id, FolderRow.user_id == user_id))
        return mappers.folder(row) if row else None

    async def list(self, *, user_id: UUID) -> list[FolderSummary]:
        counts = (
            select(MeetingRow.folder_id, func.count().label("n"))
            .where(MeetingRow.user_id == user_id, MeetingRow.folder_id.is_not(None))
            .group_by(MeetingRow.folder_id)
            .subquery()
        )
        rows = await self._s.execute(
            select(FolderRow, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.folder_id == FolderRow.id)
            .where(FolderRow.user_id == user_id)
            .order_by(func.lower(FolderRow.name), FolderRow.name, FolderRow.created_at)
        )
        return [FolderSummary(folder=mappers.folder(row), meeting_count=n) for row, n in rows]

    async def update(
        self, *, user_id: UUID, folder_id: UUID, name: str, icon: str | None, vocabulary: Sequence[str]
    ) -> Folder | None:
        row = await self._s.scalar(
            update(FolderRow)
            .where(FolderRow.id == folder_id, FolderRow.user_id == user_id)
            .values(name=name, icon=icon, vocabulary=list(vocabulary))
            .returning(FolderRow)
        )
        return mappers.folder(row) if row else None

    async def delete(self, *, user_id: UUID, folder_id: UUID) -> bool:
        # meetings.folder_id is ON DELETE SET NULL, so the folder's meetings become unfiled.
        result = await self._s.execute(delete(FolderRow).where(FolderRow.id == folder_id, FolderRow.user_id == user_id))
        return _rowcount(result) > 0

    async def delete_all(self, *, user_id: UUID) -> int:
        return _rowcount(await self._s.execute(delete(FolderRow).where(FolderRow.user_id == user_id)))


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
        folder_id: UUID | None = None,
    ) -> Meeting:
        row = MeetingRow(
            user_id=user_id,
            title=title,
            folder_id=folder_id,
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

    async def list(
        self,
        *,
        user_id: UUID,
        limit: int,
        before: datetime | None,
        folder_id: UUID | None = None,
        unfiled: bool = False,
    ) -> list[Meeting]:
        stmt = select(MeetingRow).where(MeetingRow.user_id == user_id)
        if before is not None:
            stmt = stmt.where(MeetingRow.created_at < before)
        if folder_id is not None:
            stmt = stmt.where(MeetingRow.folder_id == folder_id)
        elif unfiled:
            stmt = stmt.where(MeetingRow.folder_id.is_(None))
        rows = await self._s.scalars(stmt.order_by(MeetingRow.created_at.desc()).limit(limit))
        return [mappers.meeting(r) for r in rows]

    async def set_folder(self, *, user_id: UUID, meeting_id: UUID, folder_id: UUID | None) -> Meeting | None:
        row = await self._s.scalar(
            update(MeetingRow)
            .where(MeetingRow.id == meeting_id, MeetingRow.user_id == user_id)
            .values(folder_id=folder_id)
            .returning(MeetingRow)
        )
        return mappers.meeting(row) if row else None

    async def set_status(self, *, meeting_id: UUID, status: MeetingStatus) -> None:
        await self._s.execute(update(MeetingRow).where(MeetingRow.id == meeting_id).values(status=status.value))

    async def set_languages(self, *, meeting_id: UUID, languages: Sequence[str]) -> None:
        await self._s.execute(update(MeetingRow).where(MeetingRow.id == meeting_id).values(languages=list(languages)))

    async def set_audio(
        self, *, meeting_id: UUID, path: str | None, expires_at: datetime | None, duration_ms: int | None
    ) -> None:
        await self._s.execute(
            update(MeetingRow)
            .where(MeetingRow.id == meeting_id)
            .values(audio_path=path, audio_expires_at=expires_at, audio_duration_ms=duration_ms)
        )

    async def list_expired_audio(self, *, now: datetime, limit: int) -> list[Meeting]:
        rows = await self._s.scalars(
            select(MeetingRow)
            .where(MeetingRow.audio_expires_at <= now)  # served by the partial ix_meetings_audio_expires_at
            .order_by(MeetingRow.audio_expires_at)
            .limit(limit)
        )
        return [mappers.meeting(r) for r in rows]

    async def clear_audio(self, *, meeting_ids: Sequence[UUID]) -> None:
        if meeting_ids:
            await self._s.execute(
                update(MeetingRow)
                .where(MeetingRow.id.in_(list(meeting_ids)))
                .values(audio_path=None, audio_expires_at=None, audio_duration_ms=None)
            )

    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> bool:
        result = await self._s.execute(
            delete(MeetingRow).where(MeetingRow.id == meeting_id, MeetingRow.user_id == user_id)
        )
        return _rowcount(result) > 0

    async def list_all(self, *, user_id: UUID) -> list[Meeting]:
        rows = await self._s.scalars(
            select(MeetingRow).where(MeetingRow.user_id == user_id).order_by(MeetingRow.created_at, MeetingRow.id)
        )
        return [mappers.meeting(r) for r in rows]

    async def delete_all(self, *, user_id: UUID) -> int:
        # Every child table (note_lines, audio_chunks, transcript_segments, speakers, enhanced_notes, jobs, and any
        # later table keyed by meeting_id) references meetings ON DELETE CASCADE.
        return _rowcount(await self._s.execute(delete(MeetingRow).where(MeetingRow.user_id == user_id)))


class SqlNoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list(self, *, meeting_id: UUID) -> list[NoteLine]:
        rows = await self._s.scalars(
            select(NoteLineRow).where(NoteLineRow.meeting_id == meeting_id).order_by(NoteLineRow.position)
        )
        return [mappers.note_line(r) for r in rows]

    async def list_for_folder(self, *, user_id: UUID, folder_id: UUID) -> list[NoteLine]:
        rows = await self._s.scalars(
            select(NoteLineRow)
            .join(MeetingRow, MeetingRow.id == NoteLineRow.meeting_id)
            .where(MeetingRow.user_id == user_id, MeetingRow.folder_id == folder_id)
            .order_by(MeetingRow.created_at, NoteLineRow.position)
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


# --- Search ---------------------------------------------------------------------------------------------------

# Below this length, or when the query carries punctuation ("LR(1)", "FIRST/FOLLOW", "C++"), a substring match
# (ILIKE) is what the user means; the 'simple' parser would split such terms apart or find nothing.
MIN_FTS_QUERY_CHARS = 3
_ASCII_PUNCT = "!-/:-@\\[-`{-~"
_HAS_PUNCT = re.compile(f"[{_ASCII_PUNCT}]")
_WORD = re.compile(f"[^\\s{_ASCII_PUNCT}]+")  # any run of non-space, non-ASCII-punctuation (keeps Devanagari)


def tsquery_for(query: str, *, prefix: bool = True) -> str | None:
    """A tsquery for word-like queries ("'compil':* & 'design':*"), prefix-matching so results appear while the user
    types; None when the query should be matched as a substring instead."""
    if len(query) < MIN_FTS_QUERY_CHARS or _HAS_PUNCT.search(query):
        return None
    words = _WORD.findall(query)
    # Words hold no quotes or backslashes (ASCII punctuation is excluded), so quoting them is safe.
    return " & ".join(f"'{w}'{':*' if prefix else ''}" for w in words) or None


def like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


type _Col[T] = ColumnElement[T] | InstrumentedAttribute[T]


class SqlSearchRepository:
    """Full-text search with the 'simple' configuration (no stemming, no stop words: content is Hindi/English
    mixed), using the GIN expression indexes from tables.py; ILIKE substring matching for short or punctuated
    queries. Every branch joins meetings and filters on user_id, so ownership is enforced in SQL.

    Enhanced notes are stored as one JSONB document per meeting; AI-written blocks (sections[].blocks[] with
    source "ai") are expanded with jsonb_array_elements. User-sourced blocks are skipped: they repeat the user's
    own note lines, which are already searched directly.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def search(
        self, *, user_id: UUID, query: str, folder_id: UUID | None, limit: int, meeting_id: UUID | None = None
    ) -> list[SearchHit]:
        tsquery_text = tsquery_for(query)
        pattern = like_pattern(query)
        tsq = func.to_tsquery(SIMPLE_TS_CONFIG, tsquery_text) if tsquery_text else None
        exact_tsq = func.to_tsquery(SIMPLE_TS_CONFIG, tsquery_for(query, prefix=False))

        def match(text: _Col[str]) -> ColumnElement[bool]:
            if tsq is None:
                return text.ilike(pattern, escape="\\")
            return simple_tsvector(text).op("@@")(tsq)

        def rank(text: _Col[str]) -> ColumnElement[float]:
            if tsq is None:
                return sql_cast(literal(1.0), Float)
            # Whole-word hits rank above prefix-only ones ("DFA" above "DFAs").
            exact = case((simple_tsvector(text).op("@@")(exact_tsq), 1.0), else_=0.0)
            return sql_cast(func.ts_rank_cd(simple_tsvector(text), tsq), Float) + exact

        def branch(
            kind: SearchKind,
            text: _Col[str],
            start_ms: _Col[Any],
            end_ms: _Col[Any],
            speaker: _Col[Any],
        ):
            stmt = select(
                literal_column(f"'{kind.value}'", String).label("kind"),
                MeetingRow.id.label("meeting_id"),
                MeetingRow.title.label("meeting_title"),
                MeetingRow.folder_id.label("folder_id"),
                text.label("text"),
                start_ms.label("start_ms"),
                end_ms.label("end_ms"),
                speaker.label("speaker"),
                MeetingRow.created_at.label("created_at"),
                rank(text).label("rank"),
            ).where(MeetingRow.user_id == user_id, match(text))
            if meeting_id is not None:
                stmt = stmt.where(MeetingRow.id == meeting_id)
            return stmt.where(MeetingRow.folder_id == folder_id) if folder_id is not None else stmt

        no_int = sql_cast(null(), Integer)
        notes = branch(SearchKind.NOTE, NoteLineRow.text, NoteLineRow.t_ms, no_int, no_int).join_from(
            NoteLineRow, MeetingRow, MeetingRow.id == NoteLineRow.meeting_id
        )
        seg = TranscriptSegmentRow
        segments = branch(SearchKind.TRANSCRIPT, seg.text, seg.start_ms, seg.end_ms, seg.speaker).join_from(
            seg, MeetingRow, MeetingRow.id == seg.meeting_id
        )

        sections = (
            func.jsonb_array_elements(EnhancedNotesRow.content["sections"])
            .table_valued(column("value", JSONB))
            .lateral("section")
        )
        blocks = (
            func.jsonb_array_elements(sections.c.value["blocks"]).table_valued(column("value", JSONB)).lateral("block")
        )
        block = blocks.c.value

        def evidence_ms(key: str) -> ColumnElement[Any]:  # the block's first evidence span, if any
            return sql_cast(block[("evidence", "0", key)].astext, Integer)

        ai_notes = (
            branch(SearchKind.AI_NOTE, block["text"].astext, evidence_ms("start_ms"), evidence_ms("end_ms"), no_int)
            .select_from(EnhancedNotesRow)
            .join(MeetingRow, MeetingRow.id == EnhancedNotesRow.meeting_id)
            .join(sections, true())
            .join(blocks, true())
            .where(block["source"].astext == "ai")
        )
        if tsq is not None:  # index-backed prefilter on the whole document before expanding its blocks
            ai_notes = ai_notes.where(simple_tsvector(EnhancedNotesRow.content).op("@@")(tsq))

        hits = union_all(notes, segments, ai_notes).subquery("hits")
        rows = await self._s.execute(
            select(hits).order_by(hits.c.rank.desc(), hits.c.created_at.desc(), hits.c.start_ms).limit(limit)
        )
        return [SearchHit.model_validate(dict(r)) for r in rows.mappings()]


class SqlShareRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self, *, meeting_id: UUID, user_id: UUID, token: str, include_transcript: bool, expires_at: datetime
    ) -> Share:
        row = ShareRow(
            meeting_id=meeting_id,
            user_id=user_id,
            token=token,
            include_transcript=include_transcript,
            expires_at=expires_at,
        )
        self._s.add(row)
        await self._s.flush()
        await self._s.refresh(row)
        return mappers.share(row)

    async def get_active(self, *, user_id: UUID, meeting_id: UUID, now: datetime) -> Share | None:
        row = await self._s.scalar(
            select(ShareRow).where(
                ShareRow.meeting_id == meeting_id,
                ShareRow.user_id == user_id,
                ShareRow.revoked_at.is_(None),
                ShareRow.expires_at > now,
            )
        )
        return mappers.share(row) if row else None

    async def get_by_token(self, *, token: str, now: datetime) -> Share | None:
        row = await self._s.scalar(
            select(ShareRow).where(ShareRow.token == token, ShareRow.revoked_at.is_(None), ShareRow.expires_at > now)
        )
        return mappers.share(row) if row else None

    async def revoke_active(self, *, user_id: UUID, meeting_id: UUID, now: datetime) -> int:
        result = await self._s.execute(
            update(ShareRow)
            .where(ShareRow.meeting_id == meeting_id, ShareRow.user_id == user_id, ShareRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return _rowcount(result)


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
