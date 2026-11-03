from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import ContainerDep, CurrentUser
from app.api.schemas import CreateMeetingIn, MeetingDetailOut, MeetingListOut, MeetingOut, MoveMeetingIn
from app.domain.models import Meeting
from app.services.meetings import MeetingService

router = APIRouter(prefix="/meetings", tags=["meetings"])


def meeting_service(container: ContainerDep) -> MeetingService:
    return MeetingService(container.uow, container.storage)


Service = Annotated[MeetingService, Depends(meeting_service)]


def meeting_page(items: list[Meeting], limit: int) -> MeetingListOut:
    """One page of a newest-first listing; `next_before` is set while a full page came back."""
    next_before = items[-1].created_at if len(items) == limit else None
    return MeetingListOut(items=[MeetingOut.of(m) for m in items], next_before=next_before)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_meeting(body: CreateMeetingIn, user: CurrentUser, svc: Service) -> MeetingOut:
    meeting = await svc.create(
        user_id=user.id,
        title=body.title,
        template=body.template,
        calendar_event_id=body.calendar_event_id,
        attendees=body.attendee_models(),
        folder_id=body.folder_id,
    )
    return MeetingOut.of(meeting)


@router.get("")
async def list_meetings(
    user: CurrentUser,
    svc: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: datetime | None = None,
    folder_id: UUID | None = None,
    unfiled: bool = False,
) -> MeetingListOut:
    items = await svc.list(user_id=user.id, limit=limit, before=before, folder_id=folder_id, unfiled=unfiled)
    return meeting_page(items, limit)


@router.get("/{meeting_id}")
async def get_meeting(meeting_id: UUID, user: CurrentUser, svc: Service) -> MeetingDetailOut:
    return MeetingDetailOut.of(await svc.detail(user_id=user.id, meeting_id=meeting_id))


@router.put("/{meeting_id}/folder")
async def move_meeting(meeting_id: UUID, body: MoveMeetingIn, user: CurrentUser, svc: Service) -> MeetingOut:
    """Files the meeting under one of the caller's folders; `folder_id: null` unfiles it."""
    return MeetingOut.of(await svc.move(user_id=user.id, meeting_id=meeting_id, folder_id=body.folder_id))


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(meeting_id: UUID, user: CurrentUser, svc: Service) -> Response:
    await svc.delete(user_id=user.id, meeting_id=meeting_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
