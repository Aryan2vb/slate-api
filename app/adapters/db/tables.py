"""SQLAlchemy table definitions. The single source of truth for the schema; Alembic autogenerates from here."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnExpressionArgument,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def _meeting_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False)


# Search uses the 'simple' text-search configuration: content mixes Hindi and English, so English stemming and
# stop words would do more harm than good. Inlined (not a bind parameter) so queries match the expression indexes.
SIMPLE_TS_CONFIG = literal_column("'simple'::regconfig")


def simple_tsvector(column: ColumnExpressionArgument[Any]) -> ColumnElement[Any]:
    return func.to_tsvector(SIMPLE_TS_CONFIG, column)


def _fts_index(name: str, column: str) -> Index:
    """GIN index on to_tsvector('simple', column); queries built with simple_tsvector() can use it."""
    return Index(name, sa_text(f"to_tsvector('simple'::regconfig, {column})"), postgresql_using="gin")


class FolderRow(Base):
    __tablename__ = "folders"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(40))
    vocabulary: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class MeetingRow(Base):
    __tablename__ = "meetings"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL")
    )
    template: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    calendar_event_id: Mapped[str | None] = mapped_column(String(256))
    attendees: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    languages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("user_id", "calendar_event_id"),
        Index("ix_meetings_user_created", "user_id", "created_at"),
        Index("ix_meetings_folder_created", "folder_id", "created_at"),
    )


class NoteLineRow(Base):
    __tablename__ = "note_lines"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # client-generated
    meeting_id: Mapped[uuid.UUID] = _meeting_fk()
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    t_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_note_lines_meeting_position", "meeting_id", "position"),
        _fts_index("ix_note_lines_text_fts", "text"),
    )


class AudioChunkRow(Base):
    __tablename__ = "audio_chunks"
    id: Mapped[uuid.UUID] = _id()
    meeting_id: Mapped[uuid.UUID] = _meeting_fk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = _created()

    __table_args__ = (UniqueConstraint("meeting_id", "seq"),)


class TranscriptSegmentRow(Base):
    __tablename__ = "transcript_segments"
    id: Mapped[uuid.UUID] = _id()
    meeting_id: Mapped[uuid.UUID] = _meeting_fk()
    speaker: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_transcript_segments_meeting_start", "meeting_id", "start_ms"),
        _fts_index("ix_transcript_segments_text_fts", "text"),
    )


class SpeakerRow(Base):
    __tablename__ = "speakers"
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    attendee_email: Mapped[str | None] = mapped_column(String(320))
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class EnhancedNotesRow(Base):
    __tablename__ = "enhanced_notes"
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    # to_tsvector(regconfig, jsonb) indexes every string value of the document. Search uses it to find candidate
    # documents, then matches the individual blocks (sections[].blocks[].text) of those few.
    __table_args__ = (_fts_index("ix_enhanced_notes_content_fts", "content"),)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _id()
    meeting_id: Mapped[uuid.UUID] = _meeting_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("meeting_id", "kind"),
        Index("ix_jobs_runnable", "status", "run_after"),
    )
