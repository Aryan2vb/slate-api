"""Folders (courses): CRUD, a folder's meetings, and vocabulary suggestions mined from its notes."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import ContainerDep, CurrentUser
from app.api.routes.meetings import Service as MeetingsService
from app.api.routes.meetings import meeting_page
from app.api.schemas import (
    CreateFolderIn,
    FolderOut,
    FoldersOut,
    FolderSummaryOut,
    MeetingListOut,
    UpdateFolderIn,
    VocabularySuggestionsOut,
)
from app.services.folders import FolderService

router = APIRouter(prefix="/folders", tags=["folders"])


def folder_service(container: ContainerDep) -> FolderService:
    return FolderService(container.uow)


Service = Annotated[FolderService, Depends(folder_service)]


@router.get("")
async def list_folders(user: CurrentUser, svc: Service) -> FoldersOut:
    return FoldersOut(items=[FolderSummaryOut.of_summary(s) for s in await svc.list(user_id=user.id)])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_folder(body: CreateFolderIn, user: CurrentUser, svc: Service) -> FolderOut:
    return FolderOut.of(await svc.create(user_id=user.id, name=body.name, icon=body.icon))


@router.patch("/{folder_id}")
async def update_folder(folder_id: UUID, body: UpdateFolderIn, user: CurrentUser, svc: Service) -> FolderOut:
    return FolderOut.of(await svc.update(user_id=user.id, folder_id=folder_id, **body.changes()))


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(folder_id: UUID, user: CurrentUser, svc: Service) -> Response:
    await svc.delete(user_id=user.id, folder_id=folder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{folder_id}/meetings")
async def list_folder_meetings(
    folder_id: UUID,
    user: CurrentUser,
    svc: MeetingsService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: datetime | None = None,
) -> MeetingListOut:
    items = await svc.list(user_id=user.id, limit=limit, before=before, folder_id=folder_id)
    return meeting_page(items, limit)


@router.get("/{folder_id}/vocabulary/suggestions")
async def vocabulary_suggestions(folder_id: UUID, user: CurrentUser, svc: Service) -> VocabularySuggestionsOut:
    return VocabularySuggestionsOut.of(await svc.vocabulary_suggestions(user_id=user.id, folder_id=folder_id))
