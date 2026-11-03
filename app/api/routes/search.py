"""Search across the user's notes, transcripts and enhanced notes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import ContainerDep, CurrentUser, rate_limit
from app.api.schemas import SearchOut
from app.services.search import MAX_QUERY_CHARS, MAX_RESULTS, SearchService

router = APIRouter(prefix="/search", tags=["search"])


def search_service(container: ContainerDep) -> SearchService:
    return SearchService(container.uow)


@router.get("", dependencies=[Depends(rate_limit("search"))])
async def search(
    user: CurrentUser,
    svc: Annotated[SearchService, Depends(search_service)],
    q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_CHARS)],
    folder_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_RESULTS)] = 20,
) -> SearchOut:
    return SearchOut.of(await svc.search(user_id=user.id, query=q, folder_id=folder_id, limit=limit))
