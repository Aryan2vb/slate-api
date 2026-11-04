"""Playback of the recording kept after transcription (see AUDIO_RETENTION_DAYS)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import ContainerDep, CurrentUser
from app.api.schemas import AudioPlaybackOut
from app.services.audio import AudioService

router = APIRouter(prefix="/meetings/{meeting_id}/audio", tags=["audio"])


def audio_service(container: ContainerDep) -> AudioService:
    return AudioService(container.uow, container.storage)


Service = Annotated[AudioService, Depends(audio_service)]


@router.get("")
async def get_audio(meeting_id: UUID, user: CurrentUser, svc: Service) -> AudioPlaybackOut:
    """A signed URL (valid one hour) to stream or download the recording. 404 once it is deleted or expired."""
    playback = await svc.playback(user_id=user.id, meeting_id=meeting_id)
    return AudioPlaybackOut.model_validate(playback)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audio(meeting_id: UUID, user: CurrentUser, svc: Service) -> Response:
    """Deletes the kept recording now; the notes and transcript stay."""
    await svc.delete(user_id=user.id, meeting_id=meeting_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
