"""Share links (owner side), the public page behind them, and the Markdown export of one meeting."""

import re
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import HTMLResponse

from app.api.deps import ContainerDep, CurrentUser
from app.api.schemas import CreateShareIn, ShareOut
from app.domain.errors import NotFoundError
from app.services.meetings import MeetingService
from app.services.share import ShareService, export_filename, not_found_html, to_html, to_markdown

router = APIRouter(tags=["sharing"])

SHARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
# The public page has no script, so the policy allows none; inline CSS only.
PUBLIC_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
}


def share_service(container: ContainerDep) -> ShareService:
    s = container.settings
    return ShareService(container.uow, public_base_url=s.public_base_url, ttl_days=s.share_ttl_days)


Service = Annotated[ShareService, Depends(share_service)]


@router.post("/meetings/{meeting_id}/share", status_code=status.HTTP_201_CREATED)
async def create_share(
    meeting_id: UUID, user: CurrentUser, svc: Service, body: CreateShareIn | None = None
) -> ShareOut:
    """A public read-only link to the meeting's notes. Calling again rotates it: the previous link stops working."""
    include_transcript = body.include_transcript if body else False
    share = await svc.create(user_id=user.id, meeting_id=meeting_id, include_transcript=include_transcript)
    return ShareOut.of(share, svc.url_for(share))


@router.get("/meetings/{meeting_id}/share")
async def get_share(meeting_id: UUID, user: CurrentUser, svc: Service) -> ShareOut:
    share = await svc.get(user_id=user.id, meeting_id=meeting_id)
    return ShareOut.of(share, svc.url_for(share))


@router.delete("/meetings/{meeting_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(meeting_id: UUID, user: CurrentUser, svc: Service) -> Response:
    await svc.revoke(user_id=user.id, meeting_id=meeting_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/meetings/{meeting_id}/export")
async def export_meeting(
    meeting_id: UUID,
    user: CurrentUser,
    container: ContainerDep,
    export_format: Annotated[Literal["md"], Query(alias="format")] = "md",
    include_transcript: bool = False,
) -> Response:
    """The meeting's notes as a Markdown file download (the same content the share page shows)."""
    detail = await MeetingService(container.uow, container.storage).detail(user_id=user.id, meeting_id=meeting_id)
    title = detail.meeting.title
    filename = export_filename(title, export_format)
    # filename= is the ASCII fallback; filename* (RFC 6266) keeps non-Latin titles intact where browsers support it.
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(f'{title}.{export_format}', safe='')}"
    return Response(
        to_markdown(detail, include_transcript=include_transcript),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


@router.get("/s/{token}", response_class=HTMLResponse, include_in_schema=False)
async def public_share(token: str, request: Request, container: ContainerDep, svc: Service) -> HTMLResponse:
    # Per client IP (uvicorn runs with --proxy-headers, so this is the caller, not Render's proxy).
    ip = request.client.host if request.client else "unknown"
    limit = container.settings.rate_limit_share_views_per_hour
    await container.rate_limiter.hit(f"share_view:{ip}", limit=limit, window_s=3600)
    if not SHARE_TOKEN.fullmatch(token):
        return HTMLResponse(not_found_html(), status_code=404, headers=PUBLIC_PAGE_HEADERS)
    try:
        shared = await svc.open(token)
    except NotFoundError:
        return HTMLResponse(not_found_html(), status_code=404, headers=PUBLIC_PAGE_HEADERS)
    page = to_html(shared.detail, include_transcript=shared.share.include_transcript)
    return HTMLResponse(page, headers=PUBLIC_PAGE_HEADERS)
