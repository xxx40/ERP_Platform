from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from app.repositories.models import (
    DataGovernanceAudit,
    DataSourceApprovalRequest,
    DataSourceConnection,
    SemanticModelRecord,
    SemanticModelVersion,
)


class DataGovernanceRepositoryMixin:
    """Persistence operations for governed data sources and semantic models."""

    async def record_data_governance_audit(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        identity,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Persist a governance event while defensively removing secret material."""

        await self.initialize()
        async with self.session_factory.begin() as session:
            session.add(
                DataGovernanceAudit(
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details=self._sanitize_audit_details(details or {}),
                    created_at=datetime.now(timezone.utc),
                )
            )

    async def list_data_governance_audit(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        statement = select(DataGovernanceAudit)
        if resource_type is not None:
            statement = statement.where(
                DataGovernanceAudit.resource_type == resource_type
            )
        if resource_id is not None:
            statement = statement.where(DataGovernanceAudit.resource_id == resource_id)
        statement = statement.order_by(DataGovernanceAudit.id.desc()).limit(limit)
        async with self.session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [
            {
                "id": row.id,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "user_id": row.user_id,
                "tenant_id": row.tenant_id,
                "org_code": row.org_code,
                "details": row.details,
                "created_at": self._as_utc(row.created_at),
            }
            for row in rows
        ]

    async def create_data_source(
        self,
        *,
        connector_id: str,
        identity,
        display_name: str,
        dialect: str,
        host_masked: str,
        database_name: str | None,
        secret_id: str,
        scope: str,
        safe_config: dict[str, Any],
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        row = DataSourceConnection(
            connector_id=connector_id,
            owner_user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            org_code=identity.org_code,
            display_name=display_name,
            dialect=dialect,
            host_masked=host_masked,
            database_name=database_name,
            secret_id=secret_id,
            scope=scope,
            status="draft",
            version=1,
            safe_config=safe_config,
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory.begin() as session:
            session.add(row)
            session.add(
                DataGovernanceAudit(
                    action="create",
                    resource_type="data_source",
                    resource_id=connector_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"scope": scope, "dialect": dialect},
                    created_at=now,
                )
            )
        return self._data_source_row(row)

    async def get_data_source(self, connector_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with self.session_factory() as session:
            row = await session.get(DataSourceConnection, connector_id)
        return self._data_source_row(row) if row is not None else None

    async def list_data_sources(
        self,
        identity,
        *,
        include_reviewable: bool = False,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        conditions = [
            DataSourceConnection.tenant_id == identity.tenant_id,
            DataSourceConnection.org_code == identity.org_code,
        ]
        if not include_reviewable:
            conditions.append(
                or_(
                    DataSourceConnection.owner_user_id == identity.user_id,
                    DataSourceConnection.scope.in_(["team", "tenant"]),
                )
            )
        statement = (
            select(DataSourceConnection)
            .where(*conditions)
            .order_by(DataSourceConnection.updated_at.desc())
        )
        async with self.session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [self._data_source_row(row) for row in rows]

    async def update_data_source_status(
        self,
        connector_id: str,
        status: str,
        identity,
        *,
        approved: bool = False,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            row = await session.get(DataSourceConnection, connector_id)
            if row is None:
                raise KeyError(connector_id)
            row.status = status
            row.updated_at = now
            row.version += 1
            if approved:
                row.approved_by = identity.user_id
                row.approved_at = now
            session.add(
                DataGovernanceAudit(
                    action=status,
                    resource_type="data_source",
                    resource_id=connector_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details=details or {},
                    created_at=now,
                )
            )
        return self._data_source_row(row)

    async def rotate_data_source_secret(
        self,
        connector_id: str,
        secret_id: str,
        identity,
        *,
        provider: str,
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            row = await session.get(DataSourceConnection, connector_id)
            if row is None:
                raise KeyError(connector_id)
            row.secret_id = secret_id
            row.version += 1
            row.updated_at = now
            session.add(
                DataGovernanceAudit(
                    action="secret_rotate",
                    resource_type="data_source",
                    resource_id=connector_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"provider": provider},
                    created_at=now,
                )
            )
        return self._data_source_row(row)

    async def submit_data_source(self, connector_id: str, identity) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        request_id = uuid4().hex
        async with self.session_factory.begin() as session:
            row = await session.get(DataSourceConnection, connector_id)
            if row is None:
                raise KeyError(connector_id)
            row.status = "submitted"
            row.updated_at = now
            session.add(
                DataSourceApprovalRequest(
                    request_id=request_id,
                    connector_id=connector_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    status="submitted",
                    submitted_by=identity.user_id,
                    submitted_at=now,
                )
            )
            session.add(
                DataGovernanceAudit(
                    action="submit",
                    resource_type="data_source",
                    resource_id=connector_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"approval_request_id": request_id},
                    created_at=now,
                )
            )
        return {
            "request_id": request_id,
            "connector_id": connector_id,
            "status": "submitted",
        }

    async def review_data_source(
        self,
        connector_id: str,
        identity,
        *,
        approved: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        status = "approved" if approved else "rejected"
        async with self.session_factory.begin() as session:
            row = await session.get(DataSourceConnection, connector_id)
            if row is None:
                raise KeyError(connector_id)
            request = await session.scalar(
                select(DataSourceApprovalRequest)
                .where(
                    DataSourceApprovalRequest.connector_id == connector_id,
                    DataSourceApprovalRequest.status == "submitted",
                )
                .order_by(DataSourceApprovalRequest.submitted_at.desc())
            )
            if request is None:
                raise ValueError("data source has no submitted approval request")
            request.status = status
            request.reviewed_by = identity.user_id
            request.review_reason = reason
            request.reviewed_at = now
            row.status = status
            row.updated_at = now
            if approved:
                row.approved_by = identity.user_id
                row.approved_at = now
            session.add(
                DataGovernanceAudit(
                    action=status,
                    resource_type="data_source",
                    resource_id=connector_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"reason": reason} if reason else {},
                    created_at=now,
                )
            )
        return self._data_source_row(row)

    async def create_semantic_model(
        self,
        *,
        model_id: str,
        connector_id: str,
        identity,
        name: str,
        description: str,
        domain: str,
        scope: str,
        logical_model: dict[str, Any],
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        row = SemanticModelRecord(
            model_id=model_id,
            connector_id=connector_id,
            owner_user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            org_code=identity.org_code,
            name=name,
            description=description,
            domain=domain,
            scope=scope,
            status="draft",
            current_version=1,
            created_at=now,
            updated_at=now,
        )
        version = SemanticModelVersion(
            model_id=model_id,
            version=1,
            logical_model=logical_model,
            validation_result={},
            status="draft",
            created_by=identity.user_id,
            created_at=now,
        )
        async with self.session_factory.begin() as session:
            session.add_all([row, version])
            session.add(
                DataGovernanceAudit(
                    action="create",
                    resource_type="semantic_model",
                    resource_id=model_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"version": 1, "connector_id": connector_id},
                    created_at=now,
                )
            )
        return self._semantic_model_row(row, version)

    async def get_semantic_model(self, model_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with self.session_factory() as session:
            row = await session.get(SemanticModelRecord, model_id)
            if row is None:
                return None
            version = await session.scalar(
                select(SemanticModelVersion).where(
                    SemanticModelVersion.model_id == model_id,
                    SemanticModelVersion.version == row.current_version,
                )
            )
        return self._semantic_model_row(row, version)

    async def list_semantic_models(
        self,
        identity,
        *,
        include_reviewable: bool = False,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        statement = (
            select(SemanticModelRecord, SemanticModelVersion)
            .join(
                SemanticModelVersion,
                and_(
                    SemanticModelVersion.model_id == SemanticModelRecord.model_id,
                    SemanticModelVersion.version == SemanticModelRecord.current_version,
                ),
            )
            .where(SemanticModelRecord.tenant_id == identity.tenant_id)
            .where(SemanticModelRecord.org_code == identity.org_code)
            .order_by(SemanticModelRecord.updated_at.desc())
        )
        if not include_reviewable:
            statement = statement.where(
                SemanticModelRecord.owner_user_id == identity.user_id
            )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [self._semantic_model_row(model, version) for model, version in rows]

    async def create_semantic_model_version(
        self,
        model_id: str,
        logical_model: dict[str, Any],
        identity,
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            row = await session.get(SemanticModelRecord, model_id)
            if row is None:
                raise KeyError(model_id)
            highest_version = await session.scalar(
                select(func.max(SemanticModelVersion.version)).where(
                    SemanticModelVersion.model_id == model_id
                )
            )
            next_version = int(highest_version or 0) + 1
            version = SemanticModelVersion(
                model_id=model_id,
                version=next_version,
                logical_model=logical_model,
                validation_result={},
                status="draft",
                created_by=identity.user_id,
                created_at=now,
            )
            row.current_version = next_version
            row.status = "draft"
            row.updated_at = now
            session.add(version)
            session.add(
                DataGovernanceAudit(
                    action="version_create",
                    resource_type="semantic_model",
                    resource_id=model_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"version": next_version},
                    created_at=now,
                )
            )
        return self._semantic_model_row(row, version)

    async def list_semantic_model_versions(
        self, model_id: str
    ) -> list[dict[str, Any]]:
        await self.initialize()
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(SemanticModelVersion)
                    .where(SemanticModelVersion.model_id == model_id)
                    .order_by(SemanticModelVersion.version.desc())
                )
            ).all()
        return [self._semantic_model_version_row(row) for row in rows]

    async def get_semantic_model_version(
        self, model_id: str, version: int
    ) -> dict[str, Any] | None:
        await self.initialize()
        async with self.session_factory() as session:
            row = await session.scalar(
                select(SemanticModelVersion).where(
                    SemanticModelVersion.model_id == model_id,
                    SemanticModelVersion.version == version,
                )
            )
        return self._semantic_model_version_row(row) if row is not None else None

    async def select_semantic_model_version(
        self,
        model_id: str,
        version_number: int,
        identity,
        *,
        status: str = "published",
        action: str = "rollback",
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            row = await session.get(SemanticModelRecord, model_id)
            if row is None:
                raise KeyError(model_id)
            version = await session.scalar(
                select(SemanticModelVersion).where(
                    SemanticModelVersion.model_id == model_id,
                    SemanticModelVersion.version == version_number,
                )
            )
            if version is None:
                raise KeyError(f"{model_id}:{version_number}")
            row.current_version = version_number
            row.status = status
            row.updated_at = now
            version.status = status
            if status == "published":
                version.published_at = now
            session.add(
                DataGovernanceAudit(
                    action=action,
                    resource_type="semantic_model",
                    resource_id=model_id,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    org_code=identity.org_code,
                    details={"version": version_number},
                    created_at=now,
                )
            )
        return self._semantic_model_row(row, version)

    async def update_semantic_model_validation(
        self,
        model_id: str,
        validation: dict[str, Any],
        *,
        status: str | None = None,
        published: bool = False,
        identity=None,
        action: str | None = None,
    ) -> dict[str, Any]:
        await self.initialize()
        now = datetime.now(timezone.utc)
        async with self.session_factory.begin() as session:
            row = await session.get(SemanticModelRecord, model_id)
            if row is None:
                raise KeyError(model_id)
            version = await session.scalar(
                select(SemanticModelVersion).where(
                    SemanticModelVersion.model_id == model_id,
                    SemanticModelVersion.version == row.current_version,
                )
            )
            version.validation_result = validation
            if status:
                version.status = status
                row.status = status
            if published:
                version.published_at = now
            row.updated_at = now
            if identity is not None and action:
                session.add(
                    DataGovernanceAudit(
                        action=action,
                        resource_type="semantic_model",
                        resource_id=model_id,
                        user_id=identity.user_id,
                        tenant_id=identity.tenant_id,
                        org_code=identity.org_code,
                        details={
                            "version": row.current_version,
                            "valid": bool(validation.get("valid")),
                        },
                        created_at=now,
                    )
                )
        return self._semantic_model_row(row, version)

    @staticmethod
    def _data_source_row(row: DataSourceConnection) -> dict[str, Any]:
        return {
            "connector_id": row.connector_id,
            "owner_user_id": row.owner_user_id,
            "tenant_id": row.tenant_id,
            "org_code": row.org_code,
            "display_name": row.display_name,
            "dialect": row.dialect,
            "host_masked": row.host_masked,
            "database_name": row.database_name,
            "secret_id": row.secret_id,
            "scope": row.scope,
            "status": row.status,
            "approved_by": row.approved_by,
            "approved_at": row.approved_at,
            "version": row.version,
            "safe_config": row.safe_config,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _semantic_model_row(
        row: SemanticModelRecord,
        version: SemanticModelVersion,
    ) -> dict[str, Any]:
        return {
            "model_id": row.model_id,
            "connector_id": row.connector_id,
            "owner_user_id": row.owner_user_id,
            "tenant_id": row.tenant_id,
            "org_code": row.org_code,
            "name": row.name,
            "description": row.description,
            "domain": row.domain,
            "scope": row.scope,
            "status": row.status,
            "current_version": row.current_version,
            "logical_model": version.logical_model,
            "validation_result": version.validation_result,
            "version_status": version.status,
            "published_at": version.published_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _semantic_model_version_row(row: SemanticModelVersion) -> dict[str, Any]:
        return {
            "model_id": row.model_id,
            "version": row.version,
            "logical_model": row.logical_model,
            "validation_result": row.validation_result,
            "status": row.status,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "published_at": row.published_at,
        }

    @classmethod
    def _sanitize_audit_details(cls, value: Any) -> Any:
        sensitive_fragments = {
            "secret",
            "password",
            "token",
            "authorization",
            "credential",
            "dsn",
            "api_key",
        }
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]"
                if any(fragment in str(key).lower() for fragment in sensitive_fragments)
                else cls._sanitize_audit_details(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize_audit_details(item) for item in value]
        return value
