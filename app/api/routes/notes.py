from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import ContainerDep, CurrentUser
from app.api.schemas import NotesOut, ReplaceNotesIn
from app.services.notes import NoteLineDraft, NoteService

router = APIRouter(prefix="/meetings/{meeting_id}/notes", tags=["notes"])


def note_service(container: ContainerDep) -> NoteService:
    return NoteService(container.uow)


Service = Annotated[NoteService, Depends(note_service)]


@router.put("")
async def replace_notes(meeting_id: UUID, body: ReplaceNotesIn, user: CurrentUser, svc: Service) -> NotesOut:
    drafts = [NoteLineDraft(id=x.id, position=x.position, text=x.text, t_ms=x.t_ms) for x in body.lines]
    return NotesOut.of(await svc.replace(user_id=user.id, meeting_id=meeting_id, lines=drafts))
