"""HTTP request/response DTOs. Kept apart from domain models so the wire contract can evolve independently."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import (
    Attendee,
    AudioChunk,
    ChunkStatus,
    EnhancedNotes,
    Job,
    JobKind,
    JobStatus,
    Meeting,
    MeetingStatus,
    NoteLine,
    TemplateKind,
)
from app.ports.providers import SignedUpload
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

    def attendee_models(self) -> list[Attendee]:
        return [Attendee(email=a.email, name=a.name) for a in self.attendees]


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
