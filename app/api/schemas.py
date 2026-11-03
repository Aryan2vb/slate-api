"""HTTP request/response DTOs. Kept apart from domain models so the wire contract can evolve independently."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import (
    Attendee,
    AudioChunk,
    ChunkStatus,
    EnhancedNotes,
    Folder,
    FolderSummary,
    Job,
    JobKind,
    JobStatus,
    Meeting,
    MeetingStatus,
    NoteLine,
    SearchHit,
    SearchKind,
    TemplateKind,
)
from app.ports.providers import SignedUpload
from app.services.folders import VocabularySuggestion
from app.services.meetings import MeetingDetail, Stage


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Requests -------------------------------------------------------------------------------------------------


class AttendeeIn(_In):
    email: str | None = Field(default=None, max_length=320)
    name: str | None = Field(default=None, max_length=200)


class CreateMeetingIn(_In):
    title: str = Field(min_length=1, max_length=300)
    template: TemplateKind = TemplateKind.GENERAL
    calendar_event_id: str | None = Field(default=None, max_length=256)
    attendees: list[AttendeeIn] = Field(default_factory=list, max_length=200)
    folder_id: UUID | None = None

    def attendee_models(self) -> list[Attendee]:
        return [Attendee(email=a.email, name=a.name) for a in self.attendees]


class MoveMeetingIn(_In):
    folder_id: UUID | None  # required; null unfiles the meeting


class CreateFolderIn(_In):
    name: str = Field(min_length=1, max_length=80)
    icon: str | None = Field(default=None, max_length=40)


class UpdateFolderIn(_In):
    """Partial update: omitted fields are kept. `icon: null` clears the icon; name and vocabulary cannot be null."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    icon: str | None = Field(default=None, max_length=40)
    # Normalised (trimmed, deduped, <= 100 terms of <= 100 chars) by the service; these bounds only cap the payload.
    vocabulary: list[Annotated[str, Field(max_length=200)]] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _no_null_name_or_vocabulary(self) -> Self:
        for name in ("name", "vocabulary"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.model_fields_set}


class NoteLineIn(_In):
    id: UUID
    position: int = Field(ge=0)
    text: str
    t_ms: int | None = Field(default=None, ge=0)


class ReplaceNotesIn(_In):
    lines: list[NoteLineIn]


class StartChunkIn(_In):
    seq: int = Field(ge=0)
    mime_type: str = Field(max_length=100)
    byte_size: int | None = Field(default=None, gt=0)


# --- Responses ------------------------------------------------------------------------------------------------


class AttendeeOut(_Out):
    email: str | None
    name: str | None


class MeetingOut(_Out):
    id: UUID
    title: str
    template: TemplateKind
    status: MeetingStatus
    calendar_event_id: str | None
    attendees: list[AttendeeOut]
    languages: list[str]
    folder_id: UUID | None
    started_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, m: Meeting) -> MeetingOut:
        return cls.model_validate(m)


class MeetingListOut(_Out):
    items: list[MeetingOut]
    next_before: datetime | None


class NoteLineOut(_Out):
    id: UUID
    position: int
    text: str
    t_ms: int | None
    updated_at: datetime


class NotesOut(_Out):
    lines: list[NoteLineOut]

    @classmethod
    def of(cls, lines: list[NoteLine]) -> NotesOut:
        return cls(lines=[NoteLineOut.model_validate(x) for x in lines])


class SegmentOut(_Out):
    speaker: int
    start_ms: int
    end_ms: int
    text: str


class SpeakerOut(_Out):
    label: int
    name: str | None
    attendee_email: str | None
    confirmed: bool


class JobOut(_Out):
    id: UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    last_error: str | None
    updated_at: datetime


class MeetingDetailOut(_Out):
    meeting: MeetingOut
    stage: Stage
    notes: list[NoteLineOut]
    transcript: list[SegmentOut]
    speakers: list[SpeakerOut]
    enhanced: EnhancedNotes | None
    jobs: list[JobOut]

    @classmethod
    def of(cls, d: MeetingDetail) -> MeetingDetailOut:
        return cls(
            meeting=MeetingOut.of(d.meeting),
            stage=d.stage,
            notes=[NoteLineOut.model_validate(x) for x in d.notes],
            transcript=[SegmentOut.model_validate(x) for x in d.transcript],
            speakers=[SpeakerOut.model_validate(x) for x in d.speakers],
            enhanced=d.enhanced,
            jobs=[JobOut.model_validate(x) for x in d.jobs],
        )


class ChunkOut(_Out):
    id: UUID
    seq: int
    mime_type: str
    byte_size: int | None
    status: ChunkStatus

    @classmethod
    def of(cls, c: AudioChunk) -> ChunkOut:
        return cls.model_validate(c)


class StartChunkOut(_Out):
    chunk_id: UUID
    upload: SignedUpload


class JobsOut(_Out):
    jobs: list[JobOut]

    @classmethod
    def of(cls, jobs: list[Job]) -> JobsOut:
        return cls(jobs=[JobOut.model_validate(j) for j in jobs])


class FolderOut(_Out):
    id: UUID
    name: str
    icon: str | None
    vocabulary: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, f: Folder) -> FolderOut:
        return cls.model_validate(f)


class FolderSummaryOut(FolderOut):
    meeting_count: int

    @classmethod
    def of_summary(cls, s: FolderSummary) -> FolderSummaryOut:
        return cls.model_validate({**s.folder.model_dump(), "meeting_count": s.meeting_count})


class FoldersOut(_Out):
    items: list[FolderSummaryOut]


class VocabularySuggestionOut(_Out):
    term: str
    count: int


class VocabularySuggestionsOut(_Out):
    suggestions: list[VocabularySuggestionOut]

    @classmethod
    def of(cls, items: list[VocabularySuggestion]) -> VocabularySuggestionsOut:
        return cls(suggestions=[VocabularySuggestionOut.model_validate(x) for x in items])


class SearchResultOut(_Out):
    kind: SearchKind
    meeting_id: UUID
    meeting_title: str
    folder_id: UUID | None
    text: str  # a snippet around the match
    start_ms: int | None  # offset into the recording (note: when typed; ai_note: its first evidence span)
    end_ms: int | None
    speaker: int | None  # transcript results only
    created_at: datetime  # the meeting's


class SearchOut(_Out):
    items: list[SearchResultOut]

    @classmethod
    def of(cls, hits: list[SearchHit]) -> SearchOut:
        return cls(items=[SearchResultOut.model_validate(h) for h in hits])
