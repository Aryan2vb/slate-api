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
    Folder,
    FolderSummary,
    Job,
    JobKind,
    Meeting,
    MeetingStatus,
    NoteLine,
    SearchHit,
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


class FolderRepository(Protocol):
    async def create(self, *, user_id: UUID, name: str, icon: str | None) -> Folder: ...
    async def get(self, *, user_id: UUID, folder_id: UUID) -> Folder | None: ...
    async def list(self, *, user_id: UUID) -> list[FolderSummary]:
        """Every folder of the user with its meeting count, ordered by name (case-insensitive)."""
        ...

    async def update(
        self, *, user_id: UUID, folder_id: UUID, name: str, icon: str | None, vocabulary: Sequence[str]
    ) -> Folder | None:
        """Replaces the editable fields. None if the folder is missing or someone else's."""
        ...

    async def delete(self, *, user_id: UUID, folder_id: UUID) -> bool:
        """Its meetings stay, unfiled (folder_id becomes NULL)."""
        ...


class MeetingRepository(Protocol):
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
        """`folder_id` must already be verified as the user's."""
        ...

    async def get(self, *, user_id: UUID, meeting_id: UUID) -> Meeting | None: ...
    async def get_by_calendar_event(self, *, user_id: UUID, calendar_event_id: str) -> Meeting | None: ...
    async def list(
        self,
        *,
        user_id: UUID,
        limit: int,
        before: datetime | None,
        folder_id: UUID | None = None,
        unfiled: bool = False,
    ) -> list[Meeting]:
        """Newest first. `folder_id` keeps one folder's meetings; `unfiled` keeps those in no folder."""
        ...

    async def set_folder(self, *, user_id: UUID, meeting_id: UUID, folder_id: UUID | None) -> Meeting | None:
        """Moves a meeting (None = unfile). `folder_id` must already be verified as the user's."""
        ...

    async def set_status(self, *, meeting_id: UUID, status: MeetingStatus) -> None: ...
    async def set_languages(self, *, meeting_id: UUID, languages: Sequence[str]) -> None: ...
    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> bool: ...


class NoteRepository(Protocol):
    async def list(self, *, meeting_id: UUID) -> list[NoteLine]: ...
    async def list_for_folder(self, *, user_id: UUID, folder_id: UUID) -> list[NoteLine]:
        """Every note line of the user's meetings in one folder (meeting by meeting, in position order)."""
        ...

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


class SearchRepository(Protocol):
    async def search(self, *, user_id: UUID, query: str, folder_id: UUID | None, limit: int) -> list[SearchHit]:
        """The user's note lines, transcript segments and enhanced-note blocks matching `query`.

        Best match first, then the most recent meeting. `query` is already trimmed and non-empty.
        """
        ...


class UnitOfWork(Protocol):
    """One transaction. Use as `async with uow_factory() as uow: ...; await uow.commit()`.

    Leaving the block without commit() rolls back.
    """

    # Read-only so any implementation's concrete repository types satisfy the protocol (covariant).
    @property
    def folders(self) -> FolderRepository: ...
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
    @property
    def search(self) -> SearchRepository: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
