"""Add governed data sources and semantic models.

Revision ID: 20260731_0012
Revises: 20260731_0011
Create Date: 2026-07-31
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260731_0012"
down_revision: str | None = "20260731_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "data_source_connections",
        sa.Column("connector_id", sa.String(128), primary_key=True),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("dialect", sa.String(32), nullable=False),
        sa.Column("host_masked", sa.String(256), nullable=False),
        sa.Column("database_name", sa.String(256)),
        sa.Column("secret_id", sa.String(256), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("safe_config", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_data_source_owner_scope", "data_source_connections", ["tenant_id", "org_code", "owner_user_id"])
    op.create_index("idx_data_source_status", "data_source_connections", ["status", "tenant_id"])
    op.create_table(
        "data_source_access_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("connector_id", sa.String(128), sa.ForeignKey("data_source_connections.connector_id", ondelete="CASCADE")),
        sa.Column("grantee_type", sa.String(32), nullable=False),
        sa.Column("grantee_id", sa.String(128), nullable=False),
        sa.Column("permissions", json_type, nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connector_id", "grantee_type", "grantee_id", name="uq_data_source_grant"),
    )
    op.create_table(
        "data_source_approval_requests",
        sa.Column("request_id", sa.String(64), primary_key=True),
        sa.Column("connector_id", sa.String(128), sa.ForeignKey("data_source_connections.connector_id", ondelete="CASCADE")),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("submitted_by", sa.String(128), nullable=False),
        sa.Column("reviewed_by", sa.String(128)),
        sa.Column("review_reason", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_data_source_approval_status", "data_source_approval_requests", ["status", "tenant_id"])
    op.create_table(
        "semantic_models",
        sa.Column("model_id", sa.String(160), primary_key=True),
        sa.Column("connector_id", sa.String(128), sa.ForeignKey("data_source_connections.connector_id", ondelete="CASCADE")),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_semantic_model_scope", "semantic_models", ["tenant_id", "org_code", "owner_user_id"])
    op.create_table(
        "semantic_model_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model_id", sa.String(160), sa.ForeignKey("semantic_models.model_id", ondelete="CASCADE")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("logical_model", json_type, nullable=False),
        sa.Column("validation_result", json_type, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("model_id", "version", name="uq_semantic_model_version"),
    )
    op.create_table(
        "data_governance_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(160), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("org_code", sa.String(128), nullable=False),
        sa.Column("details", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_data_governance_resource", "data_governance_audit", ["resource_type", "resource_id"])


def downgrade() -> None:
    op.drop_index("idx_data_governance_resource", table_name="data_governance_audit")
    op.drop_table("data_governance_audit")
    op.drop_table("semantic_model_versions")
    op.drop_index("idx_semantic_model_scope", table_name="semantic_models")
    op.drop_table("semantic_models")
    op.drop_index("idx_data_source_approval_status", table_name="data_source_approval_requests")
    op.drop_table("data_source_approval_requests")
    op.drop_table("data_source_access_grants")
    op.drop_index("idx_data_source_status", table_name="data_source_connections")
    op.drop_index("idx_data_source_owner_scope", table_name="data_source_connections")
    op.drop_table("data_source_connections")
