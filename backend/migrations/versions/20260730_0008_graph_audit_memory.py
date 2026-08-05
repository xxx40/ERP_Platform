"""Add loop-safe graph node audit and structured conversation memory.

Revision ID: 20260730_0008
Revises: 20260729_0007
Create Date: 2026-07-30
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0008"
down_revision: str | None = "20260729_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


memory_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("memory_json", memory_type, nullable=True))

    with op.batch_alter_table("workflow_node_runs") as batch_op:
        batch_op.add_column(sa.Column("execution_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("graph_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("parent_node_id", sa.String(128), nullable=True))
        batch_op.add_column(
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.drop_constraint(
            "uq_workflow_node_request_node",
            type_="unique",
        )

    op.execute(
        "UPDATE workflow_node_runs "
        "SET execution_id = request_id || ':' || CAST(id AS VARCHAR), "
        "graph_id = (SELECT workflow_id FROM workflow_runs "
        "WHERE workflow_runs.request_id = workflow_node_runs.request_id)"
    )
    with op.batch_alter_table("workflow_node_runs") as batch_op:
        batch_op.alter_column("execution_id", nullable=False)
        batch_op.alter_column("graph_id", nullable=False)
        batch_op.create_unique_constraint(
            "uq_workflow_node_execution",
            ["execution_id"],
        )
    op.create_index(
        "idx_workflow_node_runs_graph_node",
        "workflow_node_runs",
        ["request_id", "graph_id", "node_id", "attempt"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_workflow_node_runs_graph_node",
        table_name="workflow_node_runs",
    )
    with op.batch_alter_table("workflow_node_runs") as batch_op:
        batch_op.drop_constraint("uq_workflow_node_execution", type_="unique")
        batch_op.create_unique_constraint(
            "uq_workflow_node_request_node",
            ["request_id", "node_id"],
        )
        batch_op.drop_column("attempt")
        batch_op.drop_column("parent_node_id")
        batch_op.drop_column("graph_id")
        batch_op.drop_column("execution_id")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("memory_json")
