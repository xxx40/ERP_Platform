"""Persist platform configuration publish and rollback audit records.

Revision ID: 20260729_0007
Revises: 20260729_0006
Create Date: 2026-07-29
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260729_0007"
down_revision: str | None = "20260729_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


config_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "platform_config_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("snapshot_version", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("config_json", config_type, nullable=False),
        sa.Column("actor_user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_platform_config_created",
        "platform_config_versions",
        ["created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_platform_config_created",
        table_name="platform_config_versions",
    )
    op.drop_table("platform_config_versions")
