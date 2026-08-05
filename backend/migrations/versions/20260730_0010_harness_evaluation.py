"""Persist Harness versions, verification and evaluation runs.

Revision ID: 20260730_0010
Revises: 20260730_0009
Create Date: 2026-07-30
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0010"
down_revision: str | None = "20260730_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.add_column(sa.Column("snapshot_version", sa.String(512)))
        batch_op.add_column(sa.Column("snapshot_hash", sa.String(64)))
        batch_op.add_column(sa.Column("skill_id", sa.String(128)))
        batch_op.add_column(sa.Column("operation_id", sa.String(128)))
        batch_op.add_column(sa.Column("prompt_version", sa.String(64)))

    op.create_table(
        "verification_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "request_id",
            sa.String(64),
            sa.ForeignKey("workflow_runs.request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("deterministic_passed", sa.Boolean(), nullable=False),
        sa.Column("semantic_status", sa.String(32), nullable=False),
        sa.Column("issues", json_type, nullable=False),
        sa.Column("repair_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_reason", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_verification_request",
        "verification_runs",
        ["request_id", "id"],
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("snapshot_version", sa.String(512)),
        sa.Column("dataset", sa.String(512), nullable=False),
        sa.Column("metrics", json_type, nullable=False),
        sa.Column("release_gate", json_type, nullable=False),
        sa.Column("result_path", sa.String(1024)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_evaluation_created",
        "evaluation_runs",
        ["created_at", "run_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_evaluation_created", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("idx_verification_request", table_name="verification_runs")
    op.drop_table("verification_runs")
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("operation_id")
        batch_op.drop_column("skill_id")
        batch_op.drop_column("snapshot_hash")
        batch_op.drop_column("snapshot_version")
