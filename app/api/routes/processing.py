"""Routes that drive or read the AI pipeline for one meeting: catch-up, speaker confirmation, reprocessing."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser
from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import ChunkStatus, Job, JobKind, Speaker
from app.services.catchup import CatchUp, CatchupService
from app.services.speakers import SpeakerService

router = APIRouter(prefix="/meetings/{meeting_id}", tags=["processing"])


class CatchUpRequest(BaseModel):
    live_transcript: str | None = Field(default=None, max_length=50_000)
    minutes: int = Field(default=5, ge=1, le=30)


class ConfirmSpeakerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    attendee_email: str | None = Field(default=None, max_length=320)


class ReprocessRequest(BaseModel):
    kind: JobKind


def _catchup(c: ContainerDep) -> CatchupService:
    return CatchupService(c.uow, c.llm)


def _speakers(c: ContainerDep) -> SpeakerService:
    return SpeakerService(c.uow, c.llm)


@router.post("/catch-up")
async def catch_up(
    meeting_id: UUID,
    body: CatchUpRequest,
    user: CurrentUser,
    service: Annotated[CatchupService, Depends(_catchup)],
) -> CatchUp:
    return await service.catch_up(
        user_id=user.id, meeting_id=meeting_id, minutes=body.minutes, live_transcript=body.live_transcript
    )


@router.put("/speakers/{label}")
async def confirm_speaker(
    meeting_id: UUID,
    label: Annotated[int, Path(ge=0, le=99)],
    body: ConfirmSpeakerRequest,
    user: CurrentUser,
    service: Annotated[SpeakerService, Depends(_speakers)],
) -> Speaker:
    return await service.confirm(
        user_id=user.id, meeting_id=meeting_id, label=label, name=body.name, attendee_email=body.attendee_email
    )


@router.post("/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess(meeting_id: UUID, body: ReprocessRequest, user: CurrentUser, c: ContainerDep) -> Job:
    async with c.uow() as uow:
        if await uow.meetings.get(user_id=user.id, meeting_id=meeting_id) is None:
            raise NotFoundError("Meeting not found")
        if body.kind == JobKind.TRANSCRIBE and not any(
            ch.status == ChunkStatus.UPLOADED for ch in await uow.chunks.list(meeting_id=meeting_id)
        ):
            # Audio is deleted after a successful transcription, so there is nothing to transcribe again.
            raise InvalidInputError("This meeting has no audio left to transcribe")
        job = await uow.jobs.enqueue(meeting_id=meeting_id, user_id=user.id, kind=body.kind)
        await uow.commit()
        return job
