"""Audio chunk uploads and the hand-off to processing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from uuid import UUID

from app.domain.errors import ConflictError, InvalidInputError, NotFoundError
from app.domain.models import AudioChunk, ChunkStatus, Job, JobKind, MeetingStatus
from app.ports.providers import ObjectStorage, SignedUpload
from app.ports.repositories import UnitOfWorkFactory
from app.services.meetings import require_meeting

ALLOWED_AUDIO_TYPES = frozenset({"audio/mp4", "audio/m4a", "audio/aac", "audio/wav", "audio/webm"})
MAX_CHUNK_BYTES = 50 * 1024 * 1024
MAX_SEQ = 99_999


@dataclass(frozen=True)
class ChunkUpload:
    chunk: AudioChunk
    upload: SignedUpload


def chunk_path(user_id: UUID, meeting_id: UUID, seq: int) -> str:
    return f"{user_id}/{meeting_id}/{seq:05d}-{uuid.uuid4()}.m4a"


def normalize_mime(mime_type: str) -> str:
    mime = mime_type.split(";")[0].strip().lower()
    if mime not in ALLOWED_AUDIO_TYPES:
        raise InvalidInputError(f"Unsupported audio type. Allowed: {', '.join(sorted(ALLOWED_AUDIO_TYPES))}")
    return mime


class UploadService:
    def __init__(self, uow: UnitOfWorkFactory, storage: ObjectStorage) -> None:
        self._uow = uow
        self._storage = storage

    async def start_chunk(
        self, *, user_id: UUID, meeting_id: UUID, seq: int, mime_type: str, byte_size: int | None
    ) -> ChunkUpload:
        mime = normalize_mime(mime_type)
        if not 0 <= seq <= MAX_SEQ:
            raise InvalidInputError(f"seq must be between 0 and {MAX_SEQ}")
        if byte_size is not None and not 0 < byte_size <= MAX_CHUNK_BYTES:
            raise InvalidInputError(f"Chunk must be between 1 byte and {MAX_CHUNK_BYTES // (1024 * 1024)} MB")
        async with self._uow() as uow:
            meeting = await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            if meeting.status not in (MeetingStatus.DRAFT, MeetingStatus.RECORDING):
                raise ConflictError("Meeting is no longer accepting audio")
            if any(c.seq == seq for c in await uow.chunks.list(meeting_id=meeting_id)):
                raise ConflictError(f"Chunk {seq} already exists")
            path = chunk_path(user_id, meeting_id, seq)
            upload = await self._storage.create_upload_url(path)
            chunk = await uow.chunks.add(
                meeting_id=meeting_id, seq=seq, storage_path=path, mime_type=mime, byte_size=byte_size
            )
            if meeting.status == MeetingStatus.DRAFT:
                await uow.meetings.set_status(meeting_id=meeting_id, status=MeetingStatus.RECORDING)
            await uow.commit()
            return ChunkUpload(chunk=chunk, upload=upload)

    async def mark_uploaded(self, *, user_id: UUID, meeting_id: UUID, chunk_id: UUID) -> AudioChunk:
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            chunk = await uow.chunks.mark_uploaded(meeting_id=meeting_id, chunk_id=chunk_id)
            if chunk is None:
                raise NotFoundError("Chunk not found")
            await uow.commit()
            return chunk

    async def finish(self, *, user_id: UUID, meeting_id: UUID) -> list[Job]:
        """Queue processing. Safe to retry: enqueue is idempotent per (meeting, kind)."""
        async with self._uow() as uow:
            meeting = await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            chunks = await uow.chunks.list(meeting_id=meeting_id)
            if not any(c.status == ChunkStatus.UPLOADED for c in chunks):
                raise ConflictError("No uploaded audio to process")
            if meeting.status != MeetingStatus.PROCESSING:
                await uow.meetings.set_status(meeting_id=meeting_id, status=MeetingStatus.PROCESSING)
            await uow.jobs.enqueue(meeting_id=meeting_id, user_id=user_id, kind=JobKind.TRANSCRIBE)
            await uow.commit()
            return await uow.jobs.list_for_meeting(meeting_id=meeting_id)
