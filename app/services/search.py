"""Search across the user's notes, transcripts and enhanced notes, optionally within one folder."""

from __future__ import annotations

from uuid import UUID

import structlog

from app.domain.errors import InvalidInputError
from app.domain.models import SearchHit
from app.ports.repositories import UnitOfWorkFactory
from app.services.folders import require_folder

log = structlog.get_logger(__name__)

MAX_QUERY_CHARS = 200
MAX_RESULTS = 50
SNIPPET_CHARS = 200


def make_snippet(text: str, query: str, width: int = SNIPPET_CHARS) -> str:
    """`text` cut to about `width` characters around the first case-insensitive occurrence of the query (or of its
    first word), with an ellipsis on each side that was cut."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    lowered = text.casefold()
    words = query.casefold().split()
    at = lowered.find(query.casefold())
    if at < 0 and words:
        at = lowered.find(words[0])
    start = max(0, min(max(at, 0) - width // 3, len(text) - width))
    end = start + width
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


class SearchService:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def search(self, *, user_id: UUID, query: str, folder_id: UUID | None, limit: int) -> list[SearchHit]:
        query = " ".join(query.split())
        if not query:
            raise InvalidInputError("Search query must not be empty")
        if len(query) > MAX_QUERY_CHARS:
            raise InvalidInputError(f"Search query may hold at most {MAX_QUERY_CHARS} characters")
        limit = max(1, min(limit, MAX_RESULTS))
        async with self._uow() as uow:
            if folder_id is not None:
                await require_folder(uow, user_id=user_id, folder_id=folder_id)
            hits = await uow.search.search(user_id=user_id, query=query, folder_id=folder_id, limit=limit)
        # Hit counts only: the query itself is the user's content and stays out of the logs.
        log.info("search.completed", results=len(hits), scoped=folder_id is not None, query_chars=len(query))
        return [h.model_copy(update={"text": make_snippet(h.text, query)}) for h in hits]
