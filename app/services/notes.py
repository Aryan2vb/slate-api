"""The user's notepad: full, idempotent replace of the timestamped lines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.errors import InvalidInputError
from app.domain.models import NoteLine
from app.ports.repositories import UnitOfWorkFactory
from app.services.meetings import require_meeting

MAX_LINES = 2000
MAX_LINE_CHARS = 5000


@dataclass(frozen=True)
class NoteLineDraft:
    id: UUID
    position: int
    text: str
    t_ms: int | None


def validate_lines(lines: Sequence[NoteLineDraft]) -> None:
    if len(lines) > MAX_LINES:
        raise InvalidInputError(f"At most {MAX_LINES} lines")
    if len({x.id for x in lines}) != len(lines):
        raise InvalidInputError("Line ids must be unique")
    if len({x.position for x in lines}) != len(lines):
        raise InvalidInputError("Line positions must be unique")
    for x in lines:
        if len(x.text) > MAX_LINE_CHARS:
            raise InvalidInputError(f"A line may hold at most {MAX_LINE_CHARS} characters")
        if x.position < 0 or (x.t_ms is not None and x.t_ms < 0):
            raise InvalidInputError("position and t_ms must be non-negative")


class NoteService:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def replace(self, *, user_id: UUID, meeting_id: UUID, lines: Sequence[NoteLineDraft]) -> list[NoteLine]:
        validate_lines(lines)
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            saved = await uow.notes.replace_all(meeting_id=meeting_id, lines=lines)
            await uow.commit()
            return saved
