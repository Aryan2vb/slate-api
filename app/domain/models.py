"""Domain entities and value objects.

These are plain Pydantic models with no knowledge of HTTP, SQL or vendors. Adapters translate to and from them;
services and ports speak only these types.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


# --- Identity -------------------------------------------------------------------------------------------------


class AuthUser(_Model):
    id: UUID
    email: str | None = None


# --- Meetings and notes ---------------------------------------------------------------------------------------


class MeetingStatus(StrEnum):
    DRAFT = "draft"
    RECORDING = "recording"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class TemplateKind(StrEnum):
    GENERAL = "general"
    ONE_ON_ONE = "one_on_one"
    SALES = "sales"
    INTERVIEW = "interview"
    LECTURE = "lecture"


class Attendee(_Model):
    email: str | None = None
    name: str | None = None


class Meeting(_Model):
    id: UUID
    user_id: UUID
    title: str
    template: TemplateKind = TemplateKind.GENERAL
    status: MeetingStatus = MeetingStatus.DRAFT
    calendar_event_id: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class NoteLine(_Model):
    """One line the user typed. `t_ms` is the offset into the recording when it was written (None if no recording)."""

    id: UUID
    meeting_id: UUID
    position: int
    text: str
    t_ms: int | None = None
    updated_at: datetime


# --- Audio and transcript -------------------------------------------------------------------------------------


class ChunkStatus(StrEnum):
    PENDING = "pending"
    UPLOADED = "uploaded"
    DELETED = "deleted"


class AudioChunk(_Model):
    id: UUID
    meeting_id: UUID
    seq: int
    storage_path: str
    mime_type: str
    byte_size: int | None = None
    status: ChunkStatus = ChunkStatus.PENDING
    created_at: datetime


class TranscriptSegment(_Model):
    speaker: int
    start_ms: int
    end_ms: int
    text: str


class Transcript(_Model):
    segments: list[TranscriptSegment]
    language: str | None = None
    duration_ms: int | None = None
    provider: str


class Speaker(_Model):
    meeting_id: UUID
    label: int
    name: str | None = None
    attendee_email: str | None = None
    confirmed: bool = False


class SpeakerSuggestion(_Model):
    label: int
    name: str
    attendee_email: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_ms: list[int] = Field(default_factory=list)


# --- Enhanced notes -------------------------------------------------------------------------------------------


class Evidence(_Model):
    start_ms: int
    end_ms: int


class NoteBlock(_Model):
    source: Literal["user", "ai"]
    text: str
    evidence: list[Evidence] = Field(default_factory=list)


class Section(_Model):
    heading: str
    blocks: list[NoteBlock]


class ActionItem(_Model):
    owner: str | None = None
    task: str
    due: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class EnhancedNotes(_Model):
    title: str
    tldr: str
    sections: list[Section]
    action_items: list[ActionItem] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# --- Jobs -----------------------------------------------------------------------------------------------------


class JobKind(StrEnum):
    TRANSCRIBE = "transcribe"
    SPEAKERS = "speakers"
    ENHANCE = "enhance"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(_Model):
    id: UUID
    meeting_id: UUID
    user_id: UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
