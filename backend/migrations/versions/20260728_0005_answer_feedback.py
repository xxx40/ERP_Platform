"""Persist structured answer feedback for the improvement loop.

Revision ID: 20260728_0005
Revises: 20260728_0004
Create Date: 2026-07-28
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260728_0005"
down_revision: str | None = "20260728_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


reason_codes_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("rating", sa.String(32), nullable=False),
        sa.Column("reason_codes", reason_codes_type, nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["interactions.request_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversations.session_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_answer_feedback_request"),
    )
    op.create_index(
        "idx_answer_feedback_rating_created",
        "answer_feedback",
        ["rating", "created_at"],
    )
    op.create_index(
        "idx_answer_feedback_session",
        "answer_feedback",
        ["session_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_answer_feedback_session", table_name="answer_feedback")
    op.drop_index(
        "idx_answer_feedback_rating_created",
        table_name="answer_feedback",
    )
    op.drop_table("answer_feedback")
