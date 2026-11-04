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


# --- Folders (courses) ----------------------------------------------------------------------------------------


class Folder(_Model):
    """A user's grouping of meetings, typically one course. `vocabulary` boosts recognition of its jargon."""

    id: UUID
    user_id: UUID
    name: str
    icon: str | None = None  # short client-side icon name (e.g. a Material icon)
    vocabulary: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class FolderSummary(_Model):
    folder: Folder
    meeting_count: int


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
    folder_id: UUID | None = None  # the course/folder it is filed under; None means unfiled
    template: TemplateKind = TemplateKind.GENERAL
    status: MeetingStatus = MeetingStatus.DRAFT
    calendar_event_id: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)  # detected from the recording; empty until transcribed
    started_at: datetime | None = None
    # The assembled recording kept for playback after transcription (None once deleted, or when not kept).
    audio_path: str | None = None
    audio_expires_at: datetime | None = None
    audio_duration_ms: int | None = None
    created_at: datetime
    updated_at: datetime

    def has_audio(self, now: datetime) -> bool:
        """Whether a kept recording can still be played (it may linger in storage until the hourly purge)."""
        return self.audio_path is not None and self.audio_expires_at is not None and self.audio_expires_at > now


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
    languages: list[str] = Field(default_factory=list)  # BCP-47, most prominent first; empty when unknown
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


# --- Sharing --------------------------------------------------------------------------------------------------


class Share(_Model):
    """A public read-only link to one meeting's notes. At most one is active per meeting; creating again rotates."""

    id: UUID
    meeting_id: UUID
    user_id: UUID
    token: str
    include_transcript: bool = False
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


# --- Search ---------------------------------------------------------------------------------------------------


class SearchKind(StrEnum):
    NOTE = "note"  # a line the user typed
    TRANSCRIPT = "transcript"  # a transcript segment
    AI_NOTE = "ai_note"  # a block of the enhanced notes


class SearchHit(_Model):
    kind: SearchKind
    meeting_id: UUID
    meeting_title: str
    folder_id: UUID | None
    text: str
    start_ms: int | None = None  # where in the recording; None when the source has no timestamp
    end_ms: int | None = None
    speaker: int | None = None  # transcript hits only
    created_at: datetime  # the meeting's, so hits can be ordered by recency
    rank: float = 0.0


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
