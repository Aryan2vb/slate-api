"""folders (courses) with vocabulary, meetings.folder_id, full-text search indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04 03:00:00.000000

Search indexes use the 'simple' text-search configuration (no stemming or stop words) because content mixes
Hindi and English. The expressions must match app/adapters/db/tables.py exactly for queries to use them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FTS_INDEXES = (
    ("ix_note_lines_text_fts", "note_lines", "text"),
    ("ix_transcript_segments_text_fts", "transcript_segments", "text"),
    # to_tsvector(regconfig, jsonb) covers every string value of the enhanced-notes document.
    ("ix_enhanced_notes_content_fts", "enhanced_notes", "content"),
)


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("icon", sa.String(length=40), nullable=True),
        sa.Column("vocabulary", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_folders")),
    )
    op.create_index(op.f("ix_folders_user_id"), "folders", ["user_id"], unique=False)

    op.add_column("meetings", sa.Column("folder_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_meetings_folder_id_folders"), "meetings", "folders", ["folder_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_meetings_folder_created", "meetings", ["folder_id", "created_at"], unique=False)

    for name, table, column in _FTS_INDEXES:
        op.create_index(
            name, table, [sa.text(f"to_tsvector('simple'::regconfig, {column})")], unique=False, postgresql_using="gin"
        )


def downgrade() -> None:
    for name, table, _ in reversed(_FTS_INDEXES):
        op.drop_index(name, table_name=table, postgresql_using="gin")

    op.drop_index("ix_meetings_folder_created", table_name="meetings")
    op.drop_constraint(op.f("fk_meetings_folder_id_folders"), "meetings", type_="foreignkey")
    op.drop_column("meetings", "folder_id")

    op.drop_index(op.f("ix_folders_user_id"), table_name="folders")
    op.drop_table("folders")
