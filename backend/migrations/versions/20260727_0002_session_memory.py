"""Add bounded task-memory and session ownership fields.

Revision ID: 20260727_0002
Revises: 20260720_0001
Create Date: 2026-07-27
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0002"
down_revision: str | None = "20260720_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("owner_user_id", sa.String(128)))
    op.add_column("conversations", sa.Column("tenant_id", sa.String(128)))
    op.add_column("conversations", sa.Column("org_code", sa.String(128)))
    op.add_column("conversations", sa.Column("last_order_number", sa.String(80)))


def downgrade() -> None:
    op.drop_column("conversations", "last_order_number")
    op.drop_column("conversations", "org_code")
    op.drop_column("conversations", "tenant_id")
    op.drop_column("conversations", "owner_user_id")
