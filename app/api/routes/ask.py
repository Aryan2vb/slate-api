"""Ask Slate: a question across the user's meetings, answered from their notes with citations."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, CurrentUser, rate_limit
from app.services.ask import MAX_FOLLOWUPS, MAX_QUESTION_CHARS, AskAnswer, AskService

router = APIRouter(prefix="/ask", tags=["ask"])


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    folder_id: UUID | None = None  # one course
    meeting_id: UUID | None = None  # one meeting (narrower than folder_id)


class CitationOut(BaseModel):
    meeting_id: UUID
    meeting_title: str
    start_ms: int | None  # where in the recording; None when the source has no timestamp
    text: str  # the excerpt the answer relies on


class AskOut(BaseModel):
    answer: str
    citations: list[CitationOut]
    followups: list[str] = Field(max_length=MAX_FOLLOWUPS)

    @classmethod
    def of(cls, a: AskAnswer) -> AskOut:
        return cls(
            answer=a.answer,
            citations=[
                CitationOut(meeting_id=c.meeting_id, meeting_title=c.meeting_title, start_ms=c.start_ms, text=c.text)
                for c in a.citations
            ],
            followups=a.followups,
        )


def ask_service(container: ContainerDep) -> AskService:
    return AskService(container.uow, container.llm, max_input_tokens=container.settings.llm_max_input_tokens)


@router.post("", dependencies=[Depends(rate_limit("ask"))])
async def ask(body: AskIn, user: CurrentUser, svc: Annotated[AskService, Depends(ask_service)]) -> AskOut:
    answer = await svc.ask(
        user_id=user.id, question=body.question, folder_id=body.folder_id, meeting_id=body.meeting_id
    )
    return AskOut.of(answer)
