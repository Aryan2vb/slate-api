"""Persistence ports (Repository + Unit of Work).

Services depend on these protocols, never on SQLAlchemy. Every read is scoped by `user_id` so ownership is enforced
in one layer; a row owned by someone else behaves exactly like a missing row (NotFoundError / None).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, Self
from uuid import UUID

from app.domain.models import (
    Attendee,
    AudioChunk,
    EnhancedNotes,
    Job,
    JobKind,
    Meeting,
    MeetingStatus,
    NoteLine,
    Speaker,
    TemplateKind,
    TranscriptSegment,
)


class NoteLineInput(Protocol):
    """Read-only view of one line to store; frozen dataclasses and Pydantic models both satisfy it."""

    @property
    def id(self) -> UUID: ...
    @property
    def position(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def t_ms(self) -> int | None: ...


class MeetingRepository(Protocol):
    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        template: TemplateKind,
        calendar_event_id: str | None,
        attendees: Sequence[Attendee],
    ) -> Meeting: ...
    async def get(self, *, user_id: UUID, meeting_id: UUID) -> Meeting | None: ...
    async def get_by_calendar_event(self, *, user_id: UUID, calendar_event_id: str) -> Meeting | None: ...
    async def list(self, *, user_id: UUID, limit: int, before: datetime | None) -> list[Meeting]: ...
    async def set_status(self, *, meeting_id: UUID, status: MeetingStatus) -> None: ...
    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> bool: ...


class NoteRepository(Protocol):
    async def list(self, *, meeting_id: UUID) -> list[NoteLine]: ...
    async def replace_all(self, *, meeting_id: UUID, lines: Sequence[NoteLineInput]) -> list[NoteLine]:
        """Idempotent full replace: the client sends its whole notepad; ids keep line identity stable."""
        ...


class ChunkRepository(Protocol):
    async def add(
        self, *, meeting_id: UUID, seq: int, storage_path: str, mime_type: str, byte_size: int | None
    ) -> AudioChunk: ...
    async def mark_uploaded(self, *, meeting_id: UUID, chunk_id: UUID) -> AudioChunk | None: ...
    async def list(self, *, meeting_id: UUID) -> list[AudioChunk]: ...
    async def mark_deleted(self, *, meeting_id: UUID) -> None: ...


class TranscriptRepository(Protocol):
    async def replace(self, *, meeting_id: UUID, segments: Sequence[TranscriptSegment]) -> None: ...
    async def list(self, *, meeting_id: UUID) -> list[TranscriptSegment]: ...


class SpeakerRepository(Protocol):
    async def list(self, *, meeting_id: UUID) -> list[Speaker]: ...
    async def upsert(self, speaker: Speaker) -> Speaker: ...


class EnhancedNotesRepository(Protocol):
    async def save(self, *, meeting_id: UUID, notes: EnhancedNotes, model: str) -> None: ...
    async def get(self, *, meeting_id: UUID) -> EnhancedNotes | None: ...


class JobRepository(Protocol):
    async def enqueue(self, *, meeting_id: UUID, user_id: UUID, kind: JobKind, max_attempts: int = 5) -> Job:
        """Idempotent per (meeting_id, kind): re-enqueueing a finished or failed job resets it to queued."""
        ...

    async def claim_next(self, *, now: datetime) -> Job | None:
        """Atomically lock the oldest runnable job (FOR UPDATE SKIP LOCKED) and mark it running."""
        ...

    async def mark_succeeded(self, *, job_id: UUID) -> None: ...
    async def mark_failed(self, *, job_id: UUID, error: str, retry_at: datetime | None) -> None:
        """retry_at=None means permanent failure; otherwise requeue for that time."""
        ...

    async def reclaim_stale(self, *, locked_before: datetime) -> int: ...
    async def list_for_meeting(self, *, meeting_id: UUID) -> list[Job]: ...


class UnitOfWork(Protocol):
    """One transaction. Use as `async with uow_factory() as uow: ...; await uow.commit()`.

    Leaving the block without commit() rolls back.
    """

    # Read-only so any implementation's concrete repository types satisfy the protocol (covariant).
    @property
    def meetings(self) -> MeetingRepository: ...
    @property
    def notes(self) -> NoteRepository: ...
    @property
    def chunks(self) -> ChunkRepository: ...
    @property
    def transcripts(self) -> TranscriptRepository: ...
    @property
    def speakers(self) -> SpeakerRepository: ...
    @property
    def enhanced(self) -> EnhancedNotesRepository: ...
    @property
    def jobs(self) -> JobRepository: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
