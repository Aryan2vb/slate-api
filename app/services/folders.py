"""Folders (courses): grouping meetings, and the course vocabulary that sharpens their transcription."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal
from uuid import UUID

import structlog

from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import Folder, FolderSummary
from app.domain.vocabulary import normalize_vocabulary, suggest_terms
from app.ports.repositories import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)

MAX_NAME_CHARS = 80
MAX_ICON_CHARS = 40
MAX_SUGGESTIONS = 20


class _Keep(Enum):
    KEEP = "keep"


KEEP: Final = _Keep.KEEP
"""Marks a field left out of a partial update (distinct from None, which clears an optional field)."""


@dataclass(frozen=True)
class VocabularySuggestion:
    term: str
    count: int  # how many times it appears in the folder's notes


def normalize_name(name: str) -> str:
    name = " ".join(name.split())
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise InvalidInputError(f"Folder name must be 1 to {MAX_NAME_CHARS} characters")
    return name


def normalize_icon(icon: str | None) -> str | None:
    icon = (icon or "").strip() or None
    if icon is not None and len(icon) > MAX_ICON_CHARS:
        raise InvalidInputError(f"Folder icon may hold at most {MAX_ICON_CHARS} characters")
    return icon


async def require_folder(uow: UnitOfWork, *, user_id: UUID, folder_id: UUID) -> Folder:
    """Ownership check shared by folder-scoped operations. Someone else's folder is indistinguishable from none."""
    folder = await uow.folders.get(user_id=user_id, folder_id=folder_id)
    if folder is None:
        raise NotFoundError("Folder not found")
    return folder


class FolderService:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def list(self, *, user_id: UUID) -> list[FolderSummary]:
        async with self._uow() as uow:
            return await uow.folders.list(user_id=user_id)

    async def get(self, *, user_id: UUID, folder_id: UUID) -> Folder:
        async with self._uow() as uow:
            return await require_folder(uow, user_id=user_id, folder_id=folder_id)

    async def create(self, *, user_id: UUID, name: str, icon: str | None) -> Folder:
        name, icon = normalize_name(name), normalize_icon(icon)
        async with self._uow() as uow:
            folder = await uow.folders.create(user_id=user_id, name=name, icon=icon)
            await uow.commit()
        log.info("folder.created", folder_id=str(folder.id))
        return folder

    async def update(
        self,
        *,
        user_id: UUID,
        folder_id: UUID,
        name: str | None = None,
        icon: str | Literal[_Keep.KEEP] | None = KEEP,
        vocabulary: Sequence[str] | None = None,
    ) -> Folder:
        """Partial update: `name`/`vocabulary` None and `icon` KEEP leave the field as it is."""
        new_name = normalize_name(name) if name is not None else None
        new_icon = normalize_icon(icon) if icon is not KEEP else KEEP
        new_vocabulary = normalize_vocabulary(vocabulary) if vocabulary is not None else None
        async with self._uow() as uow:
            current = await require_folder(uow, user_id=user_id, folder_id=folder_id)
            folder = await uow.folders.update(
                user_id=user_id,
                folder_id=folder_id,
                name=current.name if new_name is None else new_name,
                icon=current.icon if new_icon is KEEP else new_icon,
                vocabulary=current.vocabulary if new_vocabulary is None else new_vocabulary,
            )
            if folder is None:  # deleted between the read and the write
                raise NotFoundError("Folder not found")
            await uow.commit()
        log.info("folder.updated", folder_id=str(folder_id), vocabulary_terms=len(folder.vocabulary))
        return folder

    async def delete(self, *, user_id: UUID, folder_id: UUID) -> None:
        async with self._uow() as uow:
            if not await uow.folders.delete(user_id=user_id, folder_id=folder_id):
                raise NotFoundError("Folder not found")
            await uow.commit()
        log.info("folder.deleted", folder_id=str(folder_id))

    async def vocabulary_suggestions(
        self, *, user_id: UUID, folder_id: UUID, limit: int = MAX_SUGGESTIONS
    ) -> list[VocabularySuggestion]:
        """Jargon the user keeps typing in this folder's notes that is not yet in its vocabulary, most frequent
        first."""
        async with self._uow() as uow:
            folder = await require_folder(uow, user_id=user_id, folder_id=folder_id)
            lines = await uow.notes.list_for_folder(user_id=user_id, folder_id=folder_id)
        ranked = suggest_terms((n.text for n in lines), exclude=folder.vocabulary, limit=min(limit, MAX_SUGGESTIONS))
        return [VocabularySuggestion(term=t, count=n) for t, n in ranked]
