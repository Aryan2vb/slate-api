from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.deps import ContainerDep, CurrentUser, rate_limit
from app.api.schemas import ChunkOut, JobsOut, StartChunkIn, StartChunkOut
from app.services.uploads import UploadService

router = APIRouter(prefix="/meetings/{meeting_id}", tags=["uploads"])


def upload_service(container: ContainerDep) -> UploadService:
    return UploadService(container.uow, container.storage)


Service = Annotated[UploadService, Depends(upload_service)]


@router.post("/chunks", status_code=status.HTTP_201_CREATED)
async def start_chunk(meeting_id: UUID, body: StartChunkIn, user: CurrentUser, svc: Service) -> StartChunkOut:
    result = await svc.start_chunk(
        user_id=user.id, meeting_id=meeting_id, seq=body.seq, mime_type=body.mime_type, byte_size=body.byte_size
    )
    return StartChunkOut(chunk_id=result.chunk.id, upload=result.upload)


@router.post("/chunks/{chunk_id}/uploaded")
async def chunk_uploaded(meeting_id: UUID, chunk_id: UUID, user: CurrentUser, svc: Service) -> ChunkOut:
    return ChunkOut.of(await svc.mark_uploaded(user_id=user.id, meeting_id=meeting_id, chunk_id=chunk_id))


@router.post("/finish", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(rate_limit("processing"))])
async def finish(meeting_id: UUID, user: CurrentUser, svc: Service) -> JobsOut:
    return JobsOut.of(await svc.finish(user_id=user.id, meeting_id=meeting_id))
