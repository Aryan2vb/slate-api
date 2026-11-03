"""Meeting lifecycle: create, list, file into a folder, read the full detail view, delete."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import structlog

from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import (
    Attendee,
    EnhancedNotes,
    Job,
    JobStatus,
    Meeting,
    MeetingStatus,
    NoteLine,
    Speaker,
    TemplateKind,
    TranscriptSegment,
)
from app.ports.providers import ObjectStorage
from app.ports.repositories import UnitOfWork, UnitOfWorkFactory
from app.services.folders import require_folder

log = structlog.get_logger(__name__)


class Stage(StrEnum):
    """One value the app can render a progress indicator from."""

    DRAFT = "draft"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    IDENTIFYING_SPEAKERS = "identifying_speakers"
    ENHANCING = "enhancing"
    READY = "ready"
    FAILED = "failed"


_STAGE_FOR_JOB = {"transcribe": Stage.TRANSCRIBING, "speakers": Stage.IDENTIFYING_SPEAKERS, "enhance": Stage.ENHANCING}


@dataclass(frozen=True)
class MeetingDetail:
    meeting: Meeting
    notes: list[NoteLine]
    transcript: list[TranscriptSegment]
    speakers: list[Speaker]
    enhanced: EnhancedNotes | None
    jobs: list[Job]
    stage: Stage


def derive_stage(meeting: Meeting, jobs: Sequence[Job]) -> Stage:
    if any(j.status == JobStatus.FAILED for j in jobs) or meeting.status == MeetingStatus.FAILED:
        return Stage.FAILED
    active = [j for j in jobs if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)]
    if active:
        return _STAGE_FOR_JOB.get(active[0].kind.value, Stage.TRANSCRIBING)
    return {
        MeetingStatus.DRAFT: Stage.DRAFT,
        MeetingStatus.RECORDING: Stage.RECORDING,
        MeetingStatus.PROCESSING: Stage.TRANSCRIBING,
        MeetingStatus.READY: Stage.READY,
    }.get(meeting.status, Stage.DRAFT)


async def require_meeting(uow: UnitOfWork, *, user_id: UUID, meeting_id: UUID) -> Meeting:
    """Ownership check shared by every meeting-scoped service. Someone else's meeting is indistinguishable from none."""
    meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
    if meeting is None:
        raise NotFoundError("Meeting not found")
    return meeting


class MeetingService:
    def __init__(self, uow: UnitOfWorkFactory, storage: ObjectStorage) -> None:
        self._uow = uow
        self._storage = storage

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
        async with self._uow() as uow:
            if folder_id is not None:
                await require_folder(uow, user_id=user_id, folder_id=folder_id)
            if calendar_event_id is not None:
                existing = await uow.meetings.get_by_calendar_event(
                    user_id=user_id, calendar_event_id=calendar_event_id
                )
                if existing is not None:
                    return existing
            meeting = await uow.meetings.create(
                user_id=user_id,
                title=title,
                template=template,
                calendar_event_id=calendar_event_id,
                attendees=attendees,
                folder_id=folder_id,
            )
            await uow.commit()
            log.info("meeting.created", meeting_id=str(meeting.id), template=meeting.template.value)
            return meeting

    async def list(
        self,
        *,
        user_id: UUID,
        limit: int,
        before: datetime | None,
        folder_id: UUID | None = None,
        unfiled: bool = False,
    ) -> list[Meeting]:
        if folder_id is not None and unfiled:
            raise InvalidInputError("Filter by folder_id or unfiled, not both")
        async with self._uow() as uow:
            if folder_id is not None:
                await require_folder(uow, user_id=user_id, folder_id=folder_id)
            return await uow.meetings.list(
                user_id=user_id, limit=limit, before=before, folder_id=folder_id, unfiled=unfiled
            )

    async def move(self, *, user_id: UUID, meeting_id: UUID, folder_id: UUID | None) -> Meeting:
        """Files a meeting under one of the user's folders, or unfiles it (folder_id None)."""
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            if folder_id is not None:
                await require_folder(uow, user_id=user_id, folder_id=folder_id)
            meeting = await uow.meetings.set_folder(user_id=user_id, meeting_id=meeting_id, folder_id=folder_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            await uow.commit()
        log.info("meeting.moved", meeting_id=str(meeting_id), folder_id=str(folder_id) if folder_id else None)
        return meeting

    async def detail(self, *, user_id: UUID, meeting_id: UUID) -> MeetingDetail:
        async with self._uow() as uow:
            meeting = await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            jobs = await uow.jobs.list_for_meeting(meeting_id=meeting_id)
            return MeetingDetail(
                meeting=meeting,
                notes=await uow.notes.list(meeting_id=meeting_id),
                transcript=await uow.transcripts.list(meeting_id=meeting_id),
                speakers=await uow.speakers.list(meeting_id=meeting_id),
                enhanced=await uow.enhanced.get(meeting_id=meeting_id),
                jobs=jobs,
                stage=derive_stage(meeting, jobs),
            )

    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> None:
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            paths = [c.storage_path for c in await uow.chunks.list(meeting_id=meeting_id)]
            await uow.meetings.delete(user_id=user_id, meeting_id=meeting_id)
            await uow.commit()
        log.info("meeting.deleted", meeting_id=str(meeting_id), audio_objects=len(paths))
        # After commit: a storage failure must not resurrect the meeting; orphaned objects are cheaper than that.
        if paths:
            await self._storage.delete(paths)
