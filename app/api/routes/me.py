"""The signed-in user's account: what is stored, a full export, and deleting everything."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import ContainerDep, CurrentUser
from app.services.privacy import PrivacyService

router = APIRouter(prefix="/me", tags=["account"])


class MeOut(BaseModel):
    user_id: UUID
    email: str | None
    meeting_count: int
    folder_count: int
    audio_retention_days: int  # how long a kept recording stays playable (0: recordings are not kept)


def privacy_service(container: ContainerDep) -> PrivacyService:
    retention = int(getattr(container.settings, "audio_retention_days", 0))
    return PrivacyService(container.uow, container.storage, audio_retention_days=retention)


Service = Annotated[PrivacyService, Depends(privacy_service)]


@router.get("")
async def me(user: CurrentUser, svc: Service) -> MeOut:
    p = await svc.profile(user)
    return MeOut(
        user_id=p.user_id,
        email=p.email,
        meeting_count=p.meeting_count,
        folder_count=p.folder_count,
        audio_retention_days=p.audio_retention_days,
    )


@router.get("/export")
async def export(user: CurrentUser, svc: Service) -> JSONResponse:
    filename = f"slate-export-{datetime.now(UTC).date().isoformat()}.json"
    return JSONResponse(
        await svc.export(user),
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(user: CurrentUser, svc: Service) -> Response:
    await svc.delete_account(user_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
