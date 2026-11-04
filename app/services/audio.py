"""The recording kept for playback after transcription: signed playback URLs, deletion, and the expiry purge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from app.domain.errors import NotFoundError
from app.ports.providers import ObjectStorage
from app.ports.repositories import UnitOfWorkFactory
from app.services.meetings import require_meeting

log = structlog.get_logger(__name__)

PLAYBACK_URL_TTL_S = 3600
PURGE_BATCH = 200
PURGE_MAX_BATCHES = 10  # bounds one purge run; anything left waits for the next one


def kept_audio_path(user_id: UUID, meeting_id: UUID) -> str:
    """Next to the meeting's chunks, under the user's prefix (so deleting a user's objects includes it)."""
    return f"{user_id}/{meeting_id}/full.m4a"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AudioPlayback:
    url: str
    url_expires_at: datetime
    expires_at: datetime  # when the kept recording itself is deleted
    duration_ms: int | None


class AudioService:
    def __init__(self, uow: UnitOfWorkFactory, storage: ObjectStorage, clock: Callable[[], datetime] = _utcnow) -> None:
        self._uow = uow
        self._storage = storage
        self._clock = clock

    async def playback(self, *, user_id: UUID, meeting_id: UUID) -> AudioPlayback:
        now = self._clock()
        async with self._uow() as uow:
            meeting = await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
        if not meeting.has_audio(now) or meeting.audio_path is None or meeting.audio_expires_at is None:
            raise NotFoundError("No audio for this meeting")
        url = await self._storage.create_download_url(meeting.audio_path, PLAYBACK_URL_TTL_S)
        return AudioPlayback(
            url=url,
            # Never past the recording's own expiry in what we promise; the signed URL itself lasts the full TTL.
            url_expires_at=min(now + timedelta(seconds=PLAYBACK_URL_TTL_S), meeting.audio_expires_at),
            expires_at=meeting.audio_expires_at,
            duration_ms=meeting.audio_duration_ms,
        )

    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> None:
        """Deletes the kept recording now. Idempotent: no recording is not an error."""
        async with self._uow() as uow:
            meeting = await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
        if meeting.audio_path is None:
            return
        # Object first: if storage fails the row still points at it, so a retry (or the purge) finishes the job.
        await self._storage.delete([meeting.audio_path])
        async with self._uow() as uow:
            await uow.meetings.clear_audio(meeting_ids=[meeting_id])
            await uow.commit()
        log.info("audio.deleted", meeting_id=str(meeting_id))

    async def purge_expired(self) -> int:
        """Deletes every expired kept recording, objects first, then the columns. Safe to rerun at any time."""
        now = self._clock()
        purged = 0
        for _ in range(PURGE_MAX_BATCHES):
            async with self._uow() as uow:
                expired = await uow.meetings.list_expired_audio(now=now, limit=PURGE_BATCH)
            if not expired:
                break
            await self._storage.delete([m.audio_path for m in expired if m.audio_path])
            async with self._uow() as uow:
                await uow.meetings.clear_audio(meeting_ids=[m.id for m in expired])
                await uow.commit()
            purged += len(expired)
            if len(expired) < PURGE_BATCH:
                break
        log.info("audio.purged", count=purged)
        return purged
