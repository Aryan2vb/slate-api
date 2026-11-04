"""share links (shares) and the recording kept for playback (meetings.audio_*)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04 12:00:00.000000

Both partial indexes must match app/adapters/db/tables.py: one unrevoked share per meeting, and the purge scan over
meetings that still hold kept audio.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("meeting_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("include_transcript", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["meeting_id"], ["meetings.id"], name=op.f("fk_shares_meeting_id_meetings"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shares")),
        sa.UniqueConstraint("token", name=op.f("uq_shares_token")),
    )
    op.create_index(
        "uq_shares_meeting_unrevoked",
        "shares",
        ["meeting_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.add_column("meetings", sa.Column("audio_path", sa.Text(), nullable=True))
    op.add_column("meetings", sa.Column("audio_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("meetings", sa.Column("audio_duration_ms", sa.Integer(), nullable=True))
    op.create_index(
        "ix_meetings_audio_expires_at",
        "meetings",
        ["audio_expires_at"],
        unique=False,
        postgresql_where=sa.text("audio_expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_meetings_audio_expires_at", table_name="meetings", postgresql_where=sa.text("audio_expires_at IS NOT NULL")
    )
    op.drop_column("meetings", "audio_duration_ms")
    op.drop_column("meetings", "audio_expires_at")
    op.drop_column("meetings", "audio_path")

    op.drop_index("uq_shares_meeting_unrevoked", table_name="shares", postgresql_where=sa.text("revoked_at IS NULL"))
    op.drop_table("shares")
