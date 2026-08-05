"""Add generic pending Agent tasks.

Revision ID: 20260731_0011
Revises: 20260730_0010
Create Date: 2026-07-31
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260731_0011"
down_revision: str | None = "20260730_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pending_agent_tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("conversations.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("original_question", sa.Text(), nullable=False),
        sa.Column("target_tool_id", sa.String(128), nullable=False),
        sa.Column("collected_arguments", json_type, nullable=False),
        sa.Column("missing_fields", json_type, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_pending_agent_tasks_session_status",
        "pending_agent_tasks",
        ["session_id", "status"],
    )
    op.create_index(
        "idx_pending_agent_tasks_owner",
        "pending_agent_tasks",
        ["user_id", "tenant_id", "org_code", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_pending_agent_tasks_owner", table_name="pending_agent_tasks"
    )
    op.drop_index(
        "idx_pending_agent_tasks_session_status",
        table_name="pending_agent_tasks",
    )
    op.drop_table("pending_agent_tasks")
