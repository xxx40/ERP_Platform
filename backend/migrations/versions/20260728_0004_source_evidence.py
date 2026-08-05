"""Persist cited source fragments for authorized detail viewing.

Revision ID: 20260728_0004
Revises: 20260727_0003
Create Date: 2026-07-28
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260728_0004"
down_revision: str | None = "20260727_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


metadata_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "source_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("authority_level", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("source_updated_at", sa.String(128), nullable=True),
        sa.Column("metadata", metadata_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversations.session_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "source_id",
            name="uq_source_evidence_request_source",
        ),
    )
    op.create_index(
        "idx_source_evidence_request",
        "source_evidence",
        ["request_id", "source_id"],
    )
    op.create_index(
        "idx_source_evidence_session",
        "source_evidence",
        ["session_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_source_evidence_session", table_name="source_evidence")
    op.drop_index("idx_source_evidence_request", table_name="source_evidence")
    op.drop_table("source_evidence")
