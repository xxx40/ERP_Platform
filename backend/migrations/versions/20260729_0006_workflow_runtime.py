"""Persist modular workflow runtime execution records.

Revision ID: 20260729_0006
Revises: 20260728_0005
Create Date: 2026-07-29
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260729_0006"
down_revision: str | None = "20260728_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


arguments_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("workflow_version", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "idx_workflow_runs_session",
        "workflow_runs",
        ["session_id", "started_at"],
    )

    op.create_table(
        "workflow_node_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("node_kind", sa.String(32), nullable=False),
        sa.Column("handler", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["workflow_runs.request_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "node_id",
            name="uq_workflow_node_request_node",
        ),
    )
    op.create_index(
        "idx_workflow_node_runs_request",
        "workflow_node_runs",
        ["request_id", "id"],
    )

    op.create_table(
        "workflow_tool_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.String(32), nullable=False),
        sa.Column("connector_id", sa.String(128), nullable=True),
        sa.Column("arguments", arguments_type, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["workflow_runs.request_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id"),
    )
    op.create_index(
        "idx_workflow_tool_calls_request",
        "workflow_tool_calls",
        ["request_id", "id"],
    )

    op.create_table(
        "workflow_policy_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource", sa.String(256), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["workflow_runs.request_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_workflow_policy_request",
        "workflow_policy_decisions",
        ["request_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_workflow_policy_request",
        table_name="workflow_policy_decisions",
    )
    op.drop_table("workflow_policy_decisions")
    op.drop_index(
        "idx_workflow_tool_calls_request",
        table_name="workflow_tool_calls",
    )
    op.drop_table("workflow_tool_calls")
    op.drop_index(
        "idx_workflow_node_runs_request",
        table_name="workflow_node_runs",
    )
    op.drop_table("workflow_node_runs")
    op.drop_index("idx_workflow_runs_session", table_name="workflow_runs")
    op.drop_table("workflow_runs")
