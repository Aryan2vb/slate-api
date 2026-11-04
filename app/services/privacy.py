"""Privacy controls: what the user has stored, a full export of it, and deleting all of it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from app.domain.models import AuthUser
from app.ports.providers import ObjectStorage
from app.ports.repositories import UnitOfWorkFactory

log = structlog.get_logger(__name__)

EXPORT_FORMAT = "slate-export"
EXPORT_VERSION = 1
# Internal bookkeeping that means nothing outside Slate (storage locations); everything the user made is exported.
_MEETING_INTERNAL_FIELDS = {"user_id", "audio_path"}


@dataclass(frozen=True)
class Profile:
    user_id: UUID
    email: str | None
    meeting_count: int
    folder_count: int
    audio_retention_days: int


@dataclass(frozen=True)
class DeletionReport:
    meetings: int
    folders: int
    storage_objects: int


def user_storage_prefix(user_id: UUID) -> str:
    """Every object of a user lives under this prefix (chunks and the kept recording: "<user_id>/<meeting_id>/...")."""
    return f"{user_id}/"


class PrivacyService:
    def __init__(self, uow: UnitOfWorkFactory, storage: ObjectStorage, *, audio_retention_days: int) -> None:
        self._uow = uow
        self._storage = storage
        self._audio_retention_days = audio_retention_days

    async def profile(self, user: AuthUser) -> Profile:
        async with self._uow() as uow:
            meetings = await uow.meetings.list_all(user_id=user.id)
            folders = await uow.folders.list(user_id=user.id)
        return Profile(
            user_id=user.id,
            email=user.email,
            meeting_count=len(meetings),
            folder_count=len(folders),
            audio_retention_days=self._audio_retention_days,
        )

    async def export(self, user: AuthUser) -> dict[str, Any]:
        """Everything the user stored, as one JSON-ready document. Read in one transaction so it is consistent."""
        async with self._uow() as uow:
            folders = [s.folder for s in await uow.folders.list(user_id=user.id)]
            names = {f.id: f.name for f in folders}
            meetings: list[dict[str, Any]] = []
            for m in await uow.meetings.list_all(user_id=user.id):
                enhanced = await uow.enhanced.get(meeting_id=m.id)
                meetings.append(
                    {
                        **m.model_dump(mode="json", exclude=_MEETING_INTERNAL_FIELDS),
                        "folder_name": names.get(m.folder_id) if m.folder_id else None,
                        "notes": [
                            n.model_dump(mode="json", exclude={"meeting_id"})
                            for n in await uow.notes.list(meeting_id=m.id)
                        ],
                        "transcript": [s.model_dump(mode="json") for s in await uow.transcripts.list(meeting_id=m.id)],
                        "speakers": [
                            s.model_dump(mode="json", exclude={"meeting_id"})
                            for s in await uow.speakers.list(meeting_id=m.id)
                        ],
                        "enhanced_notes": enhanced.model_dump(mode="json") if enhanced else None,
                    }
                )
        log.info("account.exported", meetings=len(meetings), folders=len(folders))
        return {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "user": {"id": str(user.id), "email": user.email},
            "folders": [f.model_dump(mode="json", exclude={"user_id"}) for f in folders],
            "meetings": meetings,
        }

    async def delete_account(self, *, user_id: UUID) -> DeletionReport:
        """Deletes every row the user owns, then every stored object under their prefix.

        Meetings take their child rows with them (ON DELETE CASCADE: note lines, audio chunks, transcript, speakers,
        enhanced notes, jobs, shares); folders are deleted explicitly since meetings only reference them. Storage
        goes after the commit, as in meeting deletion; the prefix sweep makes a retried request finish the job even
        when the rows are already gone. The Supabase auth user itself is not deleted here.
        """
        async with self._uow() as uow:
            meetings = await uow.meetings.list_all(user_id=user_id)
            paths = [c.storage_path for m in meetings for c in await uow.chunks.list(meeting_id=m.id)]
            paths += [m.audio_path for m in meetings if m.audio_path]
            meeting_count = await uow.meetings.delete_all(user_id=user_id)
            folder_count = await uow.folders.delete_all(user_id=user_id)
            await uow.commit()
        if paths:
            await self._storage.delete(paths)
        swept = await self._storage.delete_prefix(user_storage_prefix(user_id))
        report = DeletionReport(meetings=meeting_count, folders=folder_count, storage_objects=len(set(paths)) + swept)
        log.info(
            "account.deleted",
            meetings=report.meetings,
            folders=report.folders,
            storage_objects=report.storage_objects,
        )
        return report
